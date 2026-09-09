"""Planner agent for memory_edit v2 (instruction -> deterministic operations)."""

from __future__ import annotations

import json
import logging
from typing import Any

from ...llm.session import llm_session
from ...llm.base import LLMClient
from ...llm.schema import Message
from ...llm.json_extract import extract_json_object
from .schema import MemoryEditPlan, MemoryEditRequest

logger = logging.getLogger(__name__)

_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok", "error"]},
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [
                            "create_if_missing",
                            "append_entry",
                            "replace_block",
                            "toggle_checkbox",
                            "ensure_index_link",
                            "prune_checked_checkboxes",
                            "delete_file",
                            "overwrite",
                        ],
                    },
                    "payload_text": {"type": "string"},
                    "old_block": {"type": "string"},
                    "new_block": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                    "item_text": {"type": "string"},
                    "checked": {"type": "boolean"},
                    "link_path": {"type": "string"},
                    "link_title": {"type": "string"},
                    "apply_all_matches": {"type": "boolean"},
                },
                "required": ["kind"],
                "additionalProperties": False,
            },
        },
        "error_code": {"type": "string"},
        "error_detail": {"type": "string"},
    },
    "required": ["status"],
    "additionalProperties": False,
}

_PLAN_SCHEMA_JSON = json.dumps(_PLAN_SCHEMA, ensure_ascii=True, separators=(",", ":"))

_TEXT_JSON_FALLBACK_SYSTEM_PROMPT = (
    "Native structured outputs are unavailable for this model.\n"
    "Return ONLY one JSON object that matches this JSON Schema exactly.\n"
    "No markdown fences, no prose, no extra keys.\n"
    "JSON Schema:\n"
    f"{_PLAN_SCHEMA_JSON}"
)

_DEFAULT_PARSE_RETRY_PROMPT = (
    "Your previous output was invalid.\n"
    "Return ONLY a JSON object with keys: status, operations, error_code, error_detail.\n"
    "No markdown fences, no prose."
)


class MemoryEditPlanner:
    """Sub-agent planner that converts user intent into deterministic operations."""

    def __init__(
        self,
        client: LLMClient,
        system_prompt: str,
        *,
        supports_response_schema: bool = True,
        parse_retries: int = 1,
        parse_retry_prompt: str | None = None,
    ) -> None:
        self.client = client
        self.system_prompt = system_prompt
        self.supports_response_schema = supports_response_schema
        self.parse_retries = max(0, parse_retries)
        self.parse_retry_prompt = parse_retry_prompt or _DEFAULT_PARSE_RETRY_PROMPT
        self.last_raw_response: str | None = None

    @llm_session("memory_editor")
    def plan(
        self,
        *,
        request: MemoryEditRequest,
        as_of: str,
        turn_id: str,
        file_exists: bool,
        file_content: str,
        file_content_available: bool = True,
    ) -> MemoryEditPlan:
        """Generate deterministic operations for one instruction request."""
        # target_file first: stable prefix for API prompt caching on appends
        payload = {
            "target_file": {
                "exists": file_exists,
                "content_available": file_content_available,
                "content": file_content,
            },
            "as_of": as_of,
            "turn_id": turn_id,
            "request": request.model_dump(mode="json"),
        }
        user_prompt = "MEMORY_EDIT_PLAN_INPUT_JSON\n" + json.dumps(
            payload, ensure_ascii=False, indent=2
        )

        base_messages = [Message(role="system", content=self.system_prompt)]
        if not self.supports_response_schema:
            # Keep fallback instructions in a stable system slot so retries only
            # add the parse-repair message, not a changing schema wrapper.
            base_messages.append(
                Message(role="system", content=_TEXT_JSON_FALLBACK_SYSTEM_PROMPT)
            )
        base_messages.append(Message(role="user", content=user_prompt))
        review_messages = base_messages

        try:
            for attempt in range(self.parse_retries + 1):
                raw = self._chat(review_messages)
                self.last_raw_response = raw
                is_final = attempt >= self.parse_retries
                parsed = self._parse_response(raw, final_attempt=is_final)
                if parsed is not None:
                    return parsed
                if attempt < self.parse_retries:
                    review_messages = [
                        *base_messages,
                        Message(role="user", content=self.parse_retry_prompt),
                    ]
        except Exception as e:
            logger.warning("memory_editor planner failed: %s", e)
            return MemoryEditPlan(
                status="error",
                error_code="planner_exception",
                error_detail=str(e),
            )

        return MemoryEditPlan(
            status="error",
            error_code="plan_parse_failed",
            error_detail="planner output was not valid JSON/schema",
        )

    def _chat(self, messages: list[Message]) -> str:
        """Prefer native structured outputs, else fall back to text JSON."""
        if self.supports_response_schema:
            return self.client.chat(messages, response_schema=_PLAN_SCHEMA)
        return self.client.chat(messages)

    def _parse_response(
        self,
        raw: str,
        *,
        final_attempt: bool,
    ) -> MemoryEditPlan | None:
        """Parse and validate planner response."""
        data = extract_json_object(raw)
        if data is None:
            logger.warning(
                "Failed to parse memory planner response (len=%d): %.500s",
                len(raw), raw.strip(),
            )
            return None
        try:
            return MemoryEditPlan.model_validate(data)
        except ValueError:
            logger.warning(
                "Invalid memory planner schema: %.500s",
                json.dumps(data, ensure_ascii=False),
            )
            return None
