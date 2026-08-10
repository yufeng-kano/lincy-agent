"""macOS app tool implementation."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from .runtime import *

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
                "Maximum messages to inspect before stopping. "
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

MAIL_JXA_HELPERS = """\
    function mailboxForScope(app, scope) {
      switch (scope) {
    case "inbox":
      return app.inbox;
    case "sent":
      return app.sentMailbox;
    case "drafts":
      return app.draftsMailbox;
    case "trash":
      return app.trashMailbox;
    case "junk":
      return app.junkMailbox;
    case "outbox":
      return app.outbox;
    default:
      return null;
      }
    }
    function resolveScopeNames(scope) {
      if (!scope || scope === "inbox") {
    return ["inbox"];
      }
      if (scope === "all") {
    return ["inbox", "sent", "drafts", "junk", "trash", "outbox"];
      }
      return [scope];
    }
    function messageDates(message, scope) {
      const dateReceived = safe(() => message.dateReceived(), null);
      const dateSent = safe(() => message.dateSent(), null);
      if (scope === "sent" || scope === "drafts" || scope === "outbox") {
    return { date: dateSent || dateReceived, kind: dateSent ? "sent" : "received" };
      }
      return { date: dateReceived || dateSent, kind: dateReceived ? "received" : "sent" };
    }
    function attachmentRows(message) {
      const attachments = safe(() => message.mailAttachments(), []) || [];
      return attachments.map((attachment) => ({
    id: String(safe(() => attachment.id(), "")),
    name: safe(() => attachment.name(), null),
    mime_type: safe(() => attachment.mimeType(), null),
    file_size: safe(() => attachment.fileSize(), null),
    downloaded: safe(() => attachment.downloaded(), null),
      }));
    }
    function mailRow(message, scope, includeContent, contentMaxChars) {
      const id = safe(() => message.id(), null);
      const dates = messageDates(message, scope);
      const attachments = attachmentRows(message);
      let content = null;
      let contentChars = null;
      let contentTruncated = false;
      if (includeContent) {
    content = safe(() => message.content(), "") || "";
    contentChars = content.length;
    if (content.length > contentMaxChars) {
      content = content.slice(0, contentMaxChars);
      contentTruncated = true;
    }
      }
      return {
    message_ref: id === null ? null : `mailmsg:${id}`,
    id,
    scope,
    subject: safe(() => message.subject(), null),
    sender: safe(() => message.sender(), null),
    reply_to: safe(() => message.replyTo(), null),
    message_id: safe(() => message.messageId(), null),
    date: iso(dates.date),
    date_kind: dates.kind,
    date_received: iso(safe(() => message.dateReceived(), null)),
    date_sent: iso(safe(() => message.dateSent(), null)),
    read: !!safe(() => message.readStatus(), false),
    flagged: !!safe(() => message.flaggedStatus(), false),
    junk: !!safe(() => message.junkMailStatus(), false),
    deleted: !!safe(() => message.deletedStatus(), false),
    message_size: safe(() => message.messageSize(), null),
    attachment_count: attachments.length,
    attachments,
    content,
    content_chars: contentChars,
    content_truncated: contentTruncated,
      };
    }
    function mailSearchRow(message, scope, attachmentCount) {
      const id = safe(() => message.id(), null);
      const dates = messageDates(message, scope);
      return {
    message_ref: id === null ? null : `mailmsg:${id}`,
    id,
    scope,
    subject: safe(() => message.subject(), null),
    sender: safe(() => message.sender(), null),
    message_id: safe(() => message.messageId(), null),
    date: iso(dates.date),
    date_kind: dates.kind,
    date_received: iso(safe(() => message.dateReceived(), null)),
    date_sent: iso(safe(() => message.dateSent(), null)),
    read: !!safe(() => message.readStatus(), false),
    flagged: !!safe(() => message.flaggedStatus(), false),
    attachment_count: attachmentCount,
      };
    }
    function parseMessageRef(messageRef) {
      const text = String(messageRef || "");
      const match = text.match(/^mailmsg:(\\d+)$/) || text.match(/^(\\d+)$/);
      return match ? Number(match[1]) : null;
    }
    function findMessageByRef(app, messageRef, scopeNames) {
      const id = parseMessageRef(messageRef);
      if (id === null) {
    return null;
      }
      for (const scope of scopeNames) {
    const mailbox = mailboxForScope(app, scope);
    if (!mailbox || !safe(() => mailbox.exists(), false)) {
      continue;
    }
    const message = mailbox.messages.byId(id);
    if (safe(() => message.exists(), false)) {
      return { message, scope };
    }
      }
      return null;
    }
"""

class MacOSAppBridge:

    def _run_mail_jxa_json(self, body: str, **kwargs: Any) -> dict[str, Any]:
        """Run Mail JXA with its app-specific helper prelude."""
        return self._run_jxa_json(body, helpers=MAIL_JXA_HELPERS, **kwargs)

    def mail_catalog(self) -> dict[str, Any]:
        """Summarize unified Mail.app scopes."""
        return self._run_mail_jxa_json(
            """
const app = Application("Mail");
const scopeNames = ["inbox", "sent", "drafts", "junk", "trash", "outbox"];
const scopes = [];
for (const scope of scopeNames) {
  const mailbox = mailboxForScope(app, scope);
  if (!mailbox || !safe(() => mailbox.exists(), false)) {
    continue;
  }
  scopes.push({
    scope,
    name: safe(() => mailbox.name(), scope),
    message_count: safe(() => mailbox.messages.length, 0),
    unread_count: safe(() => mailbox.unreadCount(), null),
  });
}
return { ok: true, scopes, count: scopes.length };
""",
            operation="mail_catalog",
        )

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
        """Search a bounded window of unified Mail.app messages."""
        date_after = _parse_mail_range_datetime(date_after, field_name="date_after")
        date_before = _parse_mail_range_datetime(date_before, field_name="date_before")
        result = self._run_mail_jxa_json(
            """
const app = Application("Mail");
const payload = readPayload();
const scopeList = resolveScopeNames(payload.scope);
const query = lower(payload.query || "");
const searchBody = !!payload.search_body;
const limit = clampLimit(payload.limit, payload.max_result_limit);
const offset = Math.max(0, Number(payload.offset || 0));
const scanLimit = clampScanLimit(payload.scan_limit, payload.default_scan_limit, payload.max_scan_limit);
const dateAfter = payload.date_after ? new Date(payload.date_after) : null;
const dateBefore = payload.date_before ? new Date(payload.date_before) : null;
const hasFilters = !!query
  || !!dateAfter
  || !!dateBefore
  || payload.unread !== null && payload.unread !== undefined
  || payload.flagged !== null && payload.flagged !== undefined
  || payload.has_attachments !== null && payload.has_attachments !== undefined;
let inspected = 0;
let scanTruncated = false;
let resultWindowFilled = false;
const matches = [];
scopeLoop:
for (const scopeName of scopeList) {
  const mailbox = mailboxForScope(app, scopeName);
  if (!mailbox || !safe(() => mailbox.exists(), false)) {
    continue;
  }
  const messages = mailbox.messages;
  const total = safe(() => messages.length, 0);
  for (let index = 0; index < total; index += 1) {
    if (inspected >= scanLimit) {
      scanTruncated = true;
      break scopeLoop;
    }
    const message = messages.at(index);
    inspected += 1;
    const dates = messageDates(message, scopeName);
    if (dateAfter && (!dates.date || dates.date < dateAfter)) {
      continue;
    }
    if (dateBefore && (!dates.date || dates.date > dateBefore)) {
      continue;
    }
    const isRead = !!safe(() => message.readStatus(), false);
    if (payload.unread !== null && payload.unread !== undefined && isRead === payload.unread) {
      continue;
    }
    const isFlagged = !!safe(() => message.flaggedStatus(), false);
    if (payload.flagged !== null && payload.flagged !== undefined && isFlagged !== payload.flagged) {
      continue;
    }
    const attachmentCount = payload.has_attachments !== null && payload.has_attachments !== undefined
      ? safe(() => message.mailAttachments().length, 0)
      : null;
    if (
      payload.has_attachments !== null
      && payload.has_attachments !== undefined
      && (attachmentCount > 0) !== payload.has_attachments
    ) {
      continue;
    }
    const sender = safe(() => message.sender(), "") || "";
    const subject = safe(() => message.subject(), "") || "";
    const quickHaystack = lower(`${sender}\\n${subject}`);
    if (query && !quickHaystack.includes(query)) {
      if (!searchBody) {
        continue;
      }
      const body = safe(() => message.content(), "") || "";
      if (!lower(body).includes(query)) {
        continue;
      }
    }
    matches.push(mailSearchRow(message, scopeName, attachmentCount));
    if (!hasFilters && matches.length >= offset + limit) {
      resultWindowFilled = true;
      break scopeLoop;
    }
  }
}
matches.sort((a, b) => compareIsoDesc(a.date, b.date));
const page = matches.slice(offset, offset + limit);
return {
  ok: true,
  scope: payload.scope || "inbox",
  scanned_scopes: scopeList,
  scanned_count: inspected,
  scan_limit: scanLimit,
  scan_truncated: scanTruncated,
  matched_count: matches.length,
  matched_count_exact: !resultWindowFilled,
  offset,
  limit,
  result_truncated: resultWindowFilled || matches.length > offset + limit,
  results: page,
  count: page.length,
  warning: scanTruncated
    ? `scan_limit ${scanLimit} reached; narrow the date range or increase scan_limit`
    : null,
};
""",
            payload={
                "scope": scope,
                "query": query,
                "search_body": search_body,
                "date_after": date_after,
                "date_before": date_before,
                "unread": unread,
                "flagged": flagged,
                "has_attachments": has_attachments,
                "scan_limit": scan_limit,
                "default_scan_limit": _APPLE_MAIL_DEFAULT_SCAN_LIMIT,
                "max_scan_limit": _APPLE_MAIL_MAX_SCAN_LIMIT,
                "max_result_limit": self._max_search_results,
                "content_max_chars": _APPLE_MAIL_GET_CONTENT_MAX_CHARS,
                "limit": limit,
                "offset": offset,
            },
            operation="mail_search",
            log_details={
                "scope": scope,
                "query": query,
                "search_body": search_body,
                "date_after": date_after,
                "date_before": date_before,
                "scan_limit": scan_limit,
                "limit": limit,
            },
        )
        return _localize_mail_datetime_fields(result)

    def mail_get(
        self,
        *,
        message_ref: str,
        scope: str | None,
    ) -> dict[str, Any]:
        """Fetch one Mail.app message by opaque ref."""
        result = self._run_mail_jxa_json(
            """
const app = Application("Mail");
const payload = readPayload();
const found = findMessageByRef(app, payload.message_ref, resolveScopeNames(payload.scope || "all"));
if (!found) {
  return { ok: false, error: `message not found: ${payload.message_ref}` };
}
return {
  ok: true,
  message: mailRow(found.message, found.scope, true, payload.content_max_chars),
};
""",
            payload={
                "message_ref": message_ref,
                "scope": scope,
                "content_max_chars": _APPLE_MAIL_GET_CONTENT_MAX_CHARS,
            },
            operation="mail_get",
            log_details={"message_ref": message_ref, "scope": scope},
        )
        return _localize_mail_datetime_fields(result)

    def mail_export_attachment(
        self,
        *,
        message_ref: str,
        attachment_ids: list[str] | None,
        destination_dir: str | None,
    ) -> dict[str, Any]:
        """Export Mail.app attachments to files."""
        export_dir = self._prepare_mail_export_dir(destination_dir)
        before = {path.name for path in export_dir.iterdir()} if export_dir.exists() else set()
        export_dir.mkdir(parents=True, exist_ok=True)
        result = self._run_mail_jxa_json(
            """
const app = Application("Mail");
const payload = readPayload();
const found = findMessageByRef(app, payload.message_ref, resolveScopeNames("all"));
if (!found) {
  return { ok: false, error: `message not found: ${payload.message_ref}` };
}
const destination = Path(payload.destination_dir);
const selectedIds = new Set((payload.attachment_ids || []).map((id) => String(id)));
const exported = [];
for (const attachment of attachmentRows(found.message, true)) {
  if (selectedIds.size > 0 && !selectedIds.has(String(attachment.id))) {
    continue;
  }
  const target = found.message.mailAttachments.byId(attachment.id);
  app.save(target, { in: destination });
  exported.push(attachment);
}
return {
  ok: true,
  message: mailRow(found.message, found.scope, false, payload.content_max_chars),
  attachments: exported,
  requested_attachment_count: selectedIds.size,
  exported_count: exported.length,
};
""",
            payload={
                "message_ref": message_ref,
                "attachment_ids": attachment_ids or [],
                "destination_dir": str(export_dir),
                "content_max_chars": _APPLE_MAIL_GET_CONTENT_MAX_CHARS,
            },
            operation="mail_export_attachment",
            log_details={
                "message_ref": message_ref,
                "attachment_ids": attachment_ids or [],
                "destination_dir": str(export_dir),
            },
        )
        if not result.get("ok"):
            return result
        files = sorted(
            str(path)
            for path in export_dir.iterdir()
            if path.is_file() and path.name not in before
        )
        result["destination_dir"] = str(export_dir)
        result["files"] = files
        result["count"] = len(files)
        return _localize_mail_datetime_fields(result)

    def mail_trash(
        self,
        *,
        message_refs: list[str],
        dry_run: bool,
    ) -> dict[str, Any]:
        """Move explicit Mail.app message refs to Trash."""
        result = self._run_mail_jxa_json(
            """
const app = Application("Mail");
const payload = readPayload();
const trash = app.trashMailbox;
const messages = [];
const missing = [];
for (const messageRef of payload.message_refs || []) {
  const found = findMessageByRef(app, messageRef, resolveScopeNames("all"));
  if (!found) {
    missing.push(messageRef);
    continue;
  }
  const row = mailRow(found.message, found.scope, false, payload.content_max_chars);
  messages.push(row);
  if (!payload.dry_run) {
    found.message.mailbox.set(trash);
  }
}
return {
  ok: missing.length === 0,
  dry_run: !!payload.dry_run,
  messages,
  count: messages.length,
  missing,
  error: missing.length > 0 ? `messages not found: ${missing.join(", ")}` : null,
};
""",
            payload={
                "message_refs": message_refs,
                "dry_run": dry_run,
                "content_max_chars": _APPLE_MAIL_GET_CONTENT_MAX_CHARS,
            },
            operation="mail_trash",
            log_details={"message_refs": message_refs, "dry_run": dry_run},
        )
        return _localize_mail_datetime_fields(result)



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
