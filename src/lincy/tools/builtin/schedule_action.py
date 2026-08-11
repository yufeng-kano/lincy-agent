"""schedule_action tool: agent can schedule future wake-up turns.

The agent calls this to create one-time reminders that fire at a
specified time.  Each scheduled action becomes an InboundMessage with
``not_before`` sitting in the queue's pending/ directory.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, TYPE_CHECKING

from ...llm.schema import ToolDefinition, ToolParameter
from .batch_helpers import normalize_batch_items
from ...timezone_utils import get_tz, now as tz_now

if TYPE_CHECKING:
    from ...agent.queue import PersistentPriorityQueue

logger = logging.getLogger(__name__)

_SCHEDULED_ACTION_PRIORITY = 2

_SCHEDULED_TEMPLATE = (
    "[SCHEDULED]\n"
    "Reason: {reason}\n"
    "Scheduled at: {scheduled_at}\n\n"
    "Act on this reason. Use send_message to deliver messages."
)

_SCHEDULE_ADD_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
            "description": "Why this wake-up is needed.",
        },
        "trigger_spec": {
            "type": "string",
            "description": "Local ISO datetime, e.g. '2026-02-22T09:00'.",
        },
    },
    "required": ["reason", "trigger_spec"],
    "additionalProperties": False,
}

SCHEDULE_ACTION_DEFINITION = ToolDefinition(
    name="schedule_action",
    description=(
        "Schedule future wake-up turns. Use 'batch_add' to create one or more "
        "reminders in one call, 'list' to see pending scheduled actions, and "
        "'batch_remove' to cancel one or more pending actions in one call. "
        "Mutating actions are batch-only and should be called at most once per "
        "turn unless the previous call failed. The list action is read-only; do "
        "not repeat the same list call in consecutive tool rounds. System "
        "heartbeats cannot be removed."
    ),
    parameters={
        "action": ToolParameter(
            type="string",
            description="Action to perform: 'batch_add', 'list', or 'batch_remove'.",
            enum=["batch_add", "list", "batch_remove"],
        ),
        "adds": ToolParameter(
            type="array",
            description=(
                "Schedule items for action='batch_add' (max 12). "
                "Use a one-item array even for a single reminder."
            ),
            json_schema={
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": _SCHEDULE_ADD_ITEM_SCHEMA,
            },
        ),
        "pending_ids": ToolParameter(
            type="array",
            description=(
                "Pending filenames to remove for action='batch_remove' (max 12). "
                "Get these from the 'list' action."
            ),
            json_schema={
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": {"type": "string"},
            },
        ),
    },
    required=["action"],
)


def create_schedule_action(
    queue: PersistentPriorityQueue,
) -> Callable[..., str]:
    """Create a schedule_action function bound to a queue."""
    from ...agent.queue import _deserialize
    from ...agent.schema import InboundMessage

    tz = get_tz()

    def schedule_action(
        action: str,
        adds: list[dict[str, Any]] | str | None = None,
        pending_ids: list[str] | str | None = None,
        **kwargs: Any,
    ) -> str:
        if kwargs:
            extras = ", ".join(sorted(kwargs.keys()))
            return (
                "Error: Invalid schedule_action arguments: "
                f"unexpected keys: {extras}"
            )
        if action == "batch_add":
            if pending_ids is not None:
                return "Error: 'pending_ids' is only valid for batch_remove"
            return _handle_batch_add(adds)
        if action == "list":
            if adds is not None or pending_ids is not None:
                return "Error: 'list' does not accept adds or pending_ids"
            return _handle_list()
        if action == "batch_remove":
            if adds is not None:
                return "Error: 'adds' is only valid for batch_add"
            return _handle_batch_remove(pending_ids)
        return f"Error: unknown action '{action}'"

    def _handle_batch_add(adds: list[dict[str, Any]] | str | None) -> str:
        normalized = _normalize_adds(adds)
        if isinstance(normalized, str):
            return normalized

        validation_error = _validate_adds(normalized)
        if validation_error:
            return validation_error

        prepared: list[tuple[str, datetime, str, float, InboundMessage]] = []
        now = tz_now()
        for item in normalized:
            reason = str(item["reason"])
            trigger_spec = str(item["trigger_spec"])
            parsed = _prepare_add(reason, trigger_spec, now=now)
            if isinstance(parsed, str):
                return parsed
            prepared.append(parsed)

        for reason, _local_dt, display_time, _hours, msg in prepared:
            queue.put(msg)
            logger.info("Scheduled action: %s at %s", reason, display_time)

        lines = [f"OK: scheduled {len(prepared)} action(s)"]
        for reason, _local_dt, display_time, hours, _msg in prepared:
            lines.append(f"- {display_time} ({hours:.1f}h from now): {reason}")
        return "\n".join(lines)

    def _prepare_add(
        reason: str | None,
        trigger_spec: str | None,
        *,
        now: datetime,
    ) -> tuple[str, datetime, str, float, InboundMessage] | str:
        if not reason:
            return "Error: 'reason' is required for batch_add"
        if not trigger_spec:
            return "Error: 'trigger_spec' is required for batch_add"

        try:
            local_dt = datetime.fromisoformat(trigger_spec)
        except ValueError:
            return f"Error: invalid datetime format: {trigger_spec!r}"

        # Normalise to app timezone: naive assumed local, aware converted
        if local_dt.tzinfo is None:
            local_dt = local_dt.replace(tzinfo=tz)
        local_dt = local_dt.astimezone(tz)

        if local_dt <= now:
            return "Error: trigger_spec must be in the future"

        display_time = local_dt.strftime("%Y-%m-%d %H:%M")
        content = _SCHEDULED_TEMPLATE.format(
            reason=reason,
            scheduled_at=display_time,
        )
        msg = InboundMessage(
            channel="system",
            content=content,
            priority=_SCHEDULED_ACTION_PRIORITY,
            sender="system",
            metadata={"scheduled_reason": reason},
            timestamp=local_dt,
            not_before=local_dt,
        )
        delta = local_dt - now
        hours = delta.total_seconds() / 3600
        return reason, local_dt, display_time, hours, msg

    def _handle_list() -> str:
        items = queue.scan_pending(channel="system")
        if not items:
            return "No pending scheduled actions."

        lines = []
        for filepath, msg in items:
            nb = msg.not_before
            if nb is not None:
                nb_str = nb.strftime("%Y-%m-%d %H:%M")
            else:
                nb_str = "immediate"
            is_system = msg.metadata.get("system", False)
            tag = " [system]" if is_system else ""
            preview = msg.content[:80].replace("\n", " ")
            lines.append(f"- {filepath.name}{tag} (at {nb_str}): {preview}")
        return "\n".join(lines)

    def _handle_batch_remove(pending_ids: list[str] | str | None) -> str:
        normalized = _normalize_pending_ids(pending_ids)
        if isinstance(normalized, str):
            return normalized

        validation_error = _validate_pending_ids(normalized)
        if validation_error:
            return validation_error

        filepaths = []
        for pending_id in normalized:
            filepath = queue._pending_dir / pending_id
            if not filepath.exists():
                return f"Error: pending message not found: {pending_id}"

            # Check if it's a system heartbeat before mutating anything.
            try:
                data = json.loads(filepath.read_text())
                msg = _deserialize(data)
                if msg.metadata.get("system"):
                    return "Error: cannot remove system heartbeats"
            except Exception:
                return f"Error: failed to read pending message: {pending_id}"
            filepaths.append(filepath)

        removed: list[str] = []
        for filepath in filepaths:
            if queue.remove_pending(filepath):
                removed.append(filepath.name)

        if len(removed) != len(normalized):
            return "Error: one or more pending messages disappeared before removal"
        return f"OK: removed {len(removed)} pending action(s): {', '.join(removed)}"

    return schedule_action


def _normalize_adds(
    adds: list[dict[str, Any]] | str | None,
) -> list[dict[str, Any]] | str:
    """Normalize batch additions using the shared scaffold."""
    result = normalize_batch_items(adds, "adds", 12)
    if isinstance(result, str):
        return result.replace("'adds' must be an array", "'adds' must be an array for batch_add").replace(
            "each adds item", "each batch_add item"
        )
    return result


def _validate_adds(adds: list[dict[str, Any]]) -> str | None:
    allowed_keys = {"reason", "trigger_spec"}
    for index, item in enumerate(adds, start=1):
        extra = sorted(set(item) - allowed_keys)
        if extra:
            return f"Error: invalid batch_add item {index} keys: " + ", ".join(extra)
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return f"Error: batch_add item {index} requires non-empty 'reason'"
        trigger_spec = item.get("trigger_spec")
        if not isinstance(trigger_spec, str) or not trigger_spec.strip():
            return f"Error: batch_add item {index} requires non-empty 'trigger_spec'"
    return None


def _normalize_pending_ids(
    pending_ids: list[str] | str | None,
) -> list[str] | str:
    if isinstance(pending_ids, str):
        try:
            pending_ids = json.loads(pending_ids)
        except (json.JSONDecodeError, TypeError):
            return "Error: 'pending_ids' must be an array for batch_remove"
    if not isinstance(pending_ids, list):
        return "Error: 'pending_ids' must be an array for batch_remove"
    if not pending_ids:
        return "Error: 'pending_ids' must contain at least one item"
    if len(pending_ids) > 12:
        return "Error: 'pending_ids' supports at most 12 items"
    if not all(isinstance(item, str) for item in pending_ids):
        return "Error: each batch_remove pending_id must be a string"
    return pending_ids


def _validate_pending_ids(pending_ids: list[str]) -> str | None:
    seen: set[str] = set()
    for pending_id in pending_ids:
        if not pending_id.strip():
            return "Error: each batch_remove pending_id must be non-empty"
        if "/" in pending_id or "\\" in pending_id:
            return f"Error: invalid pending_id: {pending_id}"
        if pending_id in seen:
            return f"Error: duplicate pending_id '{pending_id}'"
        seen.add(pending_id)
    return None
