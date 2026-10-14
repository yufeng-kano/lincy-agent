"""macOS app tool implementation."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from ....llm.schema import ToolDefinition, ToolParameter
from .runtime import (
    _APPLE_MAIL_DEFAULT_SCAN_LIMIT,
    _APPLE_MAIL_GET_CONTENT_MAX_CHARS,
    _APPLE_MAIL_MAX_SCAN_LIMIT,
    _APPLE_MAIL_SCOPES,
    _APPLE_MAIL_TRASH_MAX_MESSAGES,
    _error,
    _json_output,
    _parse_mail_range_datetime,
    _parse_tool_iso_datetime,
)
from .mail_store import MAIL_ROOT, SCOPE_ORDER, MailStore, parse_message_ref, public_fields

MAIL_TOOL_DEFINITION = ToolDefinition(
    name="mail_tool",
    description=(
        "Access the user's unified macOS Mail.app data. "
        "'catalog' summarizes built-in unified scopes, "
        "'search' scans a bounded number of messages in a scope, "
        "'get' fetches one message by message_ref, "
        "'export_attachment' saves attachments from one message, "
        "'trash' moves explicit message_refs to Trash after an optional dry run."
    ),
    parameters={
        "action": ToolParameter(
            type="string",
            description="Action to perform.",
            enum=["catalog", "search", "get", "export_attachment", "trash"],
        ),
        "scope": ToolParameter(
            type="string",
            description=(
                "Unified Mail.app scope. Defaults to inbox. "
                "'all' scans inbox, sent, drafts, junk, trash, and outbox using scan_limit."
            ),
            enum=["inbox", "sent", "drafts", "trash", "junk", "outbox", "all"],
        ),
        "message_ref": ToolParameter(
            type="string",
            description="Opaque message reference returned by search/get, e.g. 'mailmsg:12345'.",
        ),
        "message_refs": ToolParameter(
            type="array",
            description="Opaque message references returned by search/get. Required for trash.",
            items={"type": "string"},
        ),
        "attachment_ids": ToolParameter(
            type="array",
            description=(
                "Optional attachment ids to export. "
                "When omitted, export_attachment saves all attachments on the message."
            ),
            items={"type": "string"},
        ),
        "query": ToolParameter(
            type="string",
            description=(
                "Case-insensitive query matched against sender and subject. "
                "Set search_body=true to also inspect message body text."
            ),
        ),
        "search_body": ToolParameter(
            type="boolean",
            description="Whether search should inspect message body text. Defaults false for speed.",
        ),
        "date_after": ToolParameter(
            type="string",
            description=(
                "Lower bound in local time. Accepts YYYY-MM-DD or local ISO datetime. "
                "Incoming mail uses received date; sent/drafts/outbox use sent date."
            ),
        ),
        "date_before": ToolParameter(
            type="string",
            description=(
                "Upper bound in local time. Accepts YYYY-MM-DD or local ISO datetime. "
                "Date-only values include the whole local day."
            ),
        ),
        "unread": ToolParameter(
            type="boolean",
            description="Optional unread filter.",
        ),
        "flagged": ToolParameter(
            type="boolean",
            description="Optional flagged filter.",
        ),
        "has_attachments": ToolParameter(
            type="boolean",
            description="Optional attachment presence filter.",
        ),
        "scan_limit": ToolParameter(
            type="integer",
            description=(
                "Maximum messages to inspect, newest first, within the scope and date range. "
                f"Defaults to {_APPLE_MAIL_DEFAULT_SCAN_LIMIT}; max {_APPLE_MAIL_MAX_SCAN_LIMIT}."
            ),
        ),
        "limit": ToolParameter(
            type="integer",
            description="Maximum matched results to return.",
        ),
        "offset": ToolParameter(
            type="integer",
            description="Zero-based result offset for paging within the scanned window.",
        ),
        "destination_dir": ToolParameter(
            type="string",
            description="Attachment export directory. Must be within allowed paths.",
        ),
        "dry_run": ToolParameter(
            type="boolean",
            description="For trash, preview messages without moving them. Defaults true.",
        ),
    },
    required=["action"],
)

# Trash stays in Mail.app so the move syncs to the server. Each target is
# addressed directly (account / mailbox path / message id, where the AppleScript
# message id is the Envelope Index ROWID) and its Message-ID is checked against
# the index before anything moves, so a stale ref cannot trash another message.
_MAIL_TRASH_JXA = """
const app = Application("Mail");
const payload = readPayload();
// The full-path form is tried first because neither form has been verified
// against nested IMAP folders on the host; Mail may expose a nested mailbox
// only through its parents. Returns null when neither form resolves.
const mailboxOf = (target, path) => {
  const root = target.local ? app : app.accounts.byId(target.account);
  const direct = root.mailboxes.byName(path);
  if (safe(() => direct.exists(), false)) {
    return direct;
  }
  let box = root;
  for (const name of path.split("/")) {
    box = box.mailboxes.byName(name);
    if (!safe(() => box.exists(), false)) {
      return null;
    }
  }
  return box;
};
const resolved = [];
const problems = [];
for (const target of payload.targets) {
  const source = mailboxOf(target, target.mailbox_path);
  const trash = mailboxOf(target, target.trash_path);
  if (source === null || trash === null) {
    const path = source === null ? target.mailbox_path : target.trash_path;
    problems.push(`${target.message_ref}: Mail.app mailbox not found: ${path}`);
    continue;
  }
  const message = source.messages.byId(target.id);
  const matches = target.message_id === null
    ? safe(() => message.exists(), false)
    : safe(() => message.messageId(), null) === target.message_id;
  if (!matches) {
    problems.push(`${target.message_ref}: Mail.app message does not match the index; search again`);
    continue;
  }
  resolved.push({ target, message, trash });
}
if (problems.length > 0) {
  return { ok: false, error: problems.join("; ") };
}
if (!payload.dry_run) {
  for (const { message, trash } of resolved) {
    app.move(message, { to: trash });
  }
}
return { ok: true, verified_count: resolved.length };
"""


class MacOSAppBridge:
    _mail_root: Path = MAIL_ROOT

    def mail_catalog(self) -> dict[str, Any]:
        """Summarize unified Mail.app scopes from the Envelope Index."""
        return MailStore(self._mail_root).catalog()

    def mail_search(
        self,
        *,
        scope: str | None,
        query: str | None,
        search_body: bool,
        date_after: str | None,
        date_before: str | None,
        unread: bool | None,
        flagged: bool | None,
        has_attachments: bool | None,
        scan_limit: int | None,
        limit: int | None,
        offset: int | None,
    ) -> dict[str, Any]:
        """Search the newest scan_limit messages of a scope within the date range."""
        after = _parse_mail_range_datetime(date_after, field_name="date_after")
        before = _parse_mail_range_datetime(date_before, field_name="date_before")
        scope = scope or "inbox"
        scopes = list(SCOPE_ORDER) if scope == "all" else [scope]
        limit = max(1, min(limit or self._max_search_results, self._max_search_results))
        offset = max(0, offset or 0)
        scan_limit = max(1, min(scan_limit or _APPLE_MAIL_DEFAULT_SCAN_LIMIT, _APPLE_MAIL_MAX_SCAN_LIMIT))
        needle = (query or "").lower()
        store = MailStore(self._mail_root)
        with closing(store.connect()) as connection:
            window, scan_truncated = store.search_window(
                connection,
                scopes,
                after=datetime.fromisoformat(after).timestamp() if after else None,
                before=datetime.fromisoformat(before).timestamp() if before else None,
                scan_limit=scan_limit,
            )
        matches = []
        body_unavailable = 0
        for row in window:
            if unread is not None and row["read"] == unread:
                continue
            if flagged is not None and row["flagged"] != flagged:
                continue
            if has_attachments is not None and (row["attachment_count"] > 0) != has_attachments:
                continue
            if needle and needle not in f"{row['sender'] or ''}\n{row['subject'] or ''}".lower():
                if not search_body:
                    continue
                path = store.message_file(row["_url"], row["id"])
                text = store.body_text(store.read_emlx(path)) if path else None
                if text is None:
                    body_unavailable += 1
                    continue
                if needle not in text.lower():
                    continue
            matches.append(public_fields(row))
        page = matches[offset:offset + limit]
        warnings = []
        if scan_truncated:
            warnings.append(f"scan_limit {scan_limit} reached; narrow the date range or increase scan_limit")
        if body_unavailable:
            warnings.append(
                f"could not search the body of {body_unavailable} message(s) not downloaded locally; "
                "matched_count may miss them"
            )
        result = {
            "ok": True,
            "scope": scope,
            "scanned_scopes": scopes,
            "scanned_count": len(window),
            "scan_limit": scan_limit,
            "scan_truncated": scan_truncated,
            "matched_count": len(matches),
            # A skipped undownloaded body may have matched.
            "matched_count_exact": body_unavailable == 0,
            "offset": offset,
            "limit": limit,
            "result_truncated": len(matches) > offset + limit,
            "results": page,
            "count": len(page),
            "warning": "; ".join(warnings) or None,
        }
        if search_body:
            result["body_unavailable_count"] = body_unavailable
        return result

    def mail_get(
        self,
        *,
        message_ref: str,
        scope: str | None,
    ) -> dict[str, Any]:
        """Fetch one message by ref, with body text from its .emlx file."""
        store = MailStore(self._mail_root)
        rowid = parse_message_ref(message_ref)
        with closing(store.connect()) as connection:
            row = store.message(connection, rowid) if rowid is not None else None
        if row is None or (scope not in (None, "all") and scope not in row["scopes"]):
            return {"ok": False, "error": f"message not found: {message_ref}"}
        return {
            "ok": True,
            "message": store.message_details(
                row, include_content=True, max_chars=_APPLE_MAIL_GET_CONTENT_MAX_CHARS
            ),
        }

    def mail_export_attachment(
        self,
        *,
        message_ref: str,
        attachment_ids: list[str] | None,
        destination_dir: str | None,
    ) -> dict[str, Any]:
        """Copy a message's attachments out of Mail's store."""
        export_dir = self._prepare_mail_export_dir(destination_dir)
        store = MailStore(self._mail_root)
        rowid = parse_message_ref(message_ref)
        with closing(store.connect()) as connection:
            row = store.message(connection, rowid) if rowid is not None else None
        if row is None:
            return {"ok": False, "error": f"message not found: {message_ref}"}
        exported, files = store.export_attachments(row, attachment_ids or [], export_dir)
        return {
            "ok": True,
            "message": store.message_details(
                row, include_content=False, max_chars=_APPLE_MAIL_GET_CONTENT_MAX_CHARS
            ),
            "attachments": exported,
            "requested_attachment_count": len(attachment_ids or []),
            "exported_count": len(exported),
            "destination_dir": str(export_dir),
            "files": files,
            "count": len(files),
        }

    def mail_trash(
        self,
        *,
        message_refs: list[str],
        dry_run: bool,
    ) -> dict[str, Any]:
        """Move explicit message refs to their account's Trash in Mail.app."""
        store = MailStore(self._mail_root)
        rows = []
        missing = []
        problems = []
        with closing(store.connect()) as connection:
            for message_ref in message_refs:
                rowid = parse_message_ref(message_ref)
                row = store.message(connection, rowid) if rowid is not None else None
                if row is None:
                    missing.append(message_ref)
                elif "trash" in row["scopes"]:
                    problems.append(f"{message_ref}: already in Trash")
                elif row["_trash_mailbox"] is None:
                    problems.append(f"{message_ref}: no Trash mailbox found for its account")
                else:
                    rows.append(row)
        if missing:
            return {"ok": False, "error": f"messages not found: {', '.join(missing)}"}
        if problems:
            return {"ok": False, "error": "; ".join(problems)}
        result = self._run_jxa_json(
            _MAIL_TRASH_JXA,
            payload={
                "dry_run": dry_run,
                "targets": [
                    {
                        "message_ref": row["message_ref"],
                        "id": row["id"],
                        "local": row["_local"],
                        "account": row["_account"],
                        "mailbox_path": row["_mailbox_path"],
                        "trash_path": row["_trash_mailbox"],
                        "message_id": row["message_id"],
                    }
                    for row in rows
                ],
            },
            operation="mail_trash",
            log_details={"message_refs": message_refs, "dry_run": dry_run},
        )
        if not result.get("ok"):
            return result
        messages = [
            store.message_details(row, include_content=False, max_chars=_APPLE_MAIL_GET_CONTENT_MAX_CHARS)
            for row in rows
        ]
        return {
            "ok": True,
            "dry_run": dry_run,
            "messages": messages,
            "count": len(messages),
            "missing": [],
            "error": None,
        }


def create_mail_tool(bridge: MacOSAppBridge) -> Callable[..., str]:
    """Create mail_tool bound to the bridge."""

    def mail_tool(
        action: str,
        scope: str | None = None,
        message_ref: str | None = None,
        message_refs: list[str] | None = None,
        attachment_ids: list[str] | None = None,
        query: str | None = None,
        search_body: bool = False,
        date_after: str | None = None,
        date_before: str | None = None,
        unread: bool | None = None,
        flagged: bool | None = None,
        has_attachments: bool | None = None,
        scan_limit: int | None = None,
        limit: int | None = None,
        offset: int | None = None,
        destination_dir: str | None = None,
        dry_run: bool = True,
    ) -> str:

            if scope is not None and scope not in _APPLE_MAIL_SCOPES:
                return _error(f"unknown scope '{scope}'")
            if action == "catalog":
                return _json_output(bridge.mail_catalog())
            if action == "search":
                after_iso = _parse_mail_range_datetime(
                    date_after, field_name="date_after"
                )
                before_iso = _parse_mail_range_datetime(
                    date_before, field_name="date_before"
                )
                after_dt = _parse_tool_iso_datetime(after_iso) if after_iso else None
                before_dt = _parse_tool_iso_datetime(before_iso) if before_iso else None
                if (
                    after_dt is not None
                    and before_dt is not None
                    and before_dt < after_dt
                ):
                    return _error("'date_before' must be after or equal to 'date_after'")
                return _json_output(
                    bridge.mail_search(
                        scope=scope,
                        query=query,
                        search_body=search_body,
                        date_after=date_after,
                        date_before=date_before,
                        unread=unread,
                        flagged=flagged,
                        has_attachments=has_attachments,
                        scan_limit=scan_limit,
                        limit=limit,
                        offset=offset,
                    )
                )
            if action == "get":
                if not message_ref:
                    return _error("'message_ref' is required for get")
                return _json_output(
                    bridge.mail_get(message_ref=message_ref, scope=scope)
                )
            if action == "export_attachment":
                if not message_ref:
                    return _error("'message_ref' is required for export_attachment")
                return _json_output(
                    bridge.mail_export_attachment(
                        message_ref=message_ref,
                        attachment_ids=attachment_ids,
                        destination_dir=destination_dir,
                    )
                )
            if action == "trash":
                refs = list(message_refs or [])
                if message_ref:
                    refs.append(message_ref)
                refs = list(dict.fromkeys(refs))
                if not refs:
                    return _error("'message_refs' is required for trash")
                if len(refs) > _APPLE_MAIL_TRASH_MAX_MESSAGES:
                    return _error(
                        f"trash accepts at most {_APPLE_MAIL_TRASH_MAX_MESSAGES} messages"
                    )
                return _json_output(
                    bridge.mail_trash(message_refs=refs, dry_run=dry_run)
                )
            return _error(f"unknown action '{action}'")


    return mail_tool
