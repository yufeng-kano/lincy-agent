"""Schemas for memory editor v2 (instruction -> planned operations)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


OperationKind = Literal[
    "create_if_missing",
    "append_entry",
    "replace_block",
    "toggle_checkbox",
    "ensure_index_link",
    "prune_checked_checkboxes",
    "delete_file",
    "overwrite",
]


ApplyStatus = Literal["applied", "noop", "already_applied"]


class MemoryEditRequest(BaseModel):
    """Single instruction-style memory edit request from brain."""

    request_id: str
    target_path: str
    instruction: str


class MemoryEditOperation(BaseModel):
    """One deterministic operation planned by memory_editor sub-agent."""

    kind: OperationKind
    payload_text: str | None = None
    old_block: str | None = None
    new_block: str | None = None
    replace_all: bool = False
    item_text: str | None = None
    checked: bool | None = None
    link_path: str | None = None
    link_title: str | None = None
    apply_all_matches: bool = True

    @model_validator(mode="after")
    def validate_kind_fields(self) -> "MemoryEditOperation":
        if self.kind in {"create_if_missing", "append_entry"}:
            if self.payload_text is None:
                raise ValueError("payload_text is required for create_if_missing/append_entry")

        if self.kind == "replace_block":
            if self.old_block is None or self.new_block is None:
                raise ValueError("old_block and new_block are required for replace_block")
            if self.old_block == "":
                raise ValueError("old_block must be non-empty for replace_block")

        if self.kind == "toggle_checkbox":
            if self.item_text is None or self.checked is None:
                raise ValueError("item_text and checked are required for toggle_checkbox")

        if self.kind == "ensure_index_link":
            if self.link_path is None or self.link_title is None:
                raise ValueError("link_path and link_title are required for ensure_index_link")

        if self.kind == "overwrite":
            if self.payload_text is None:
                raise ValueError("payload_text is required for overwrite")

        return self


class MemoryEditPlan(BaseModel):
    """Planner output for one memory edit request."""

    status: Literal["ok", "error"]
    operations: list[MemoryEditOperation] = Field(default_factory=list)
    index_description: str | None = None
    error_code: str | None = None
    error_detail: str | None = None

    @model_validator(mode="after")
    def validate_status_payload(self) -> "MemoryEditPlan":
        if self.status == "ok" and not self.operations:
            raise ValueError("operations must be non-empty when status=ok")
        if self.status == "error" and not self.error_code:
            raise ValueError("error_code is required when status=error")
        return self


class MemoryEditBatch(BaseModel):
    """Batch request for memory_edit tool."""

    as_of: str
    turn_id: str
    requests: list[MemoryEditRequest] = Field(min_length=1, max_length=12)


class AppliedItem(BaseModel):
    """Applied/noop/already_applied status for one request."""

    request_id: str
    status: ApplyStatus
    path: str


class ErrorItem(BaseModel):
    """Error details for one request."""

    request_id: str
    code: str
    detail: str


class WarningItem(BaseModel):
    """Non-blocking warning about file state."""

    path: str
    code: str
    detail: str


class MemoryEditResult(BaseModel):
    """Result payload for memory_edit tool."""

    status: Literal["ok", "failed"]
    turn_id: str
    applied: list[AppliedItem]
    errors: list[ErrorItem]
    warnings: list[WarningItem] = Field(default_factory=list)
