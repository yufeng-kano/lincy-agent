"""Gmail channel adapter: polls agent's inbox, sends replies via Gmail API."""

from __future__ import annotations

import base64
import email.utils
import logging
import os
import re
import tempfile
import threading
import time
import mimetypes
from datetime import datetime

from ...timezone_utils import now as tz_now
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ..contact_map import ContactMap
from ..schema import InboundMessage, OutboundMessage
from ..thread_registry import ThreadRegistry
from .formatting import format_attachment_lines, markdown_to_plaintext

if TYPE_CHECKING:
    from ..core import AgentCore

logger = logging.getLogger(__name__)

_GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25 MB

# Patterns stripped from email body before enqueueing
_QUOTE_RE = re.compile(r"^>.*$", re.MULTILINE)
_ON_WROTE_RE = re.compile(r"^On .+ wrote:\s*$", re.MULTILINE)
_SIGNATURE_RE = re.compile(r"^--\s*$.*", re.DOTALL | re.MULTILINE)


# ------------------------------------------------------------------
# Thin Gmail REST client (httpx + OAuth2 refresh)
# ------------------------------------------------------------------

class _GmailClient:
    """Minimal Gmail API wrapper.  Uses httpx for HTTP and standard-library
    ``email`` for MIME construction.  OAuth2 token refresh is a single POST.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._token_expiry: float = 0
        self._http = httpx.Client(timeout=30)

    # -- auth ---------------------------------------------------------

    def _ensure_token(self) -> str:
        if self._access_token and time.time() < self._token_expiry - 60:
            return self._access_token
        resp = self._http.post(
            _TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        token: str = data["access_token"]
        self._access_token = token
        self._token_expiry = time.time() + data.get("expires_in", 3600)
        return token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._ensure_token()}"}

    # -- API calls ----------------------------------------------------

    def list_unread(self, query_extra: str = "") -> list[dict[str, Any]]:
        """List unread messages in INBOX (up to 10)."""
        q = "is:unread in:inbox"
        if query_extra:
            q = f"{q} {query_extra}"
        resp = self._http.get(
            f"{_GMAIL_API}/users/me/messages",
            headers=self._headers(),
            params={"q": q, "maxResults": "10"},
        )
        resp.raise_for_status()
        return resp.json().get("messages", [])

    def get_message(self, msg_id: str) -> dict[str, Any]:
        """Get full message by ID."""
        resp = self._http.get(
            f"{_GMAIL_API}/users/me/messages/{msg_id}",
            headers=self._headers(),
            params={"format": "full"},
        )
        resp.raise_for_status()
        return resp.json()

    def archive(self, msg_id: str) -> None:
        """Remove INBOX and UNREAD labels (archive)."""
        resp = self._http.post(
            f"{_GMAIL_API}/users/me/messages/{msg_id}/modify",
            headers=self._headers(),
            json={"removeLabelIds": ["INBOX", "UNREAD"]},
        )
        resp.raise_for_status()

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
        attachments: list[str] | None = None,
    ) -> dict[str, Any]:
        """Send an email, optionally with file attachments.

        Returns the Gmail API response (contains ``id`` and ``threadId``).
        """
        if attachments:
            mime = MIMEMultipart()
            mime.attach(MIMEText(body, "plain", "utf-8"))
            for filepath in attachments:
                p = Path(filepath)
                ctype, _ = mimetypes.guess_type(str(p))
                maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
                part = MIMEBase(maintype, subtype)
                part.set_payload(p.read_bytes())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition", "attachment", filename=p.name,
                )
                mime.attach(part)
        else:
            mime = MIMEText(body, "plain", "utf-8")
        mime["To"] = to
        mime["Subject"] = subject
        if in_reply_to:
            mime["In-Reply-To"] = in_reply_to
            mime["References"] = in_reply_to
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
        payload: dict[str, Any] = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id
        resp = self._http.post(
            f"{_GMAIL_API}/users/me/messages/send",
            headers=self._headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    def get_attachment(self, msg_id: str, attachment_id: str) -> bytes:
        """Download attachment by ID. Returns raw bytes."""
        resp = self._http.get(
            f"{_GMAIL_API}/users/me/messages/{msg_id}/attachments/{attachment_id}",
            headers=self._headers(),
        )
        resp.raise_for_status()
        data: str = resp.json().get("data", "")
        return base64.urlsafe_b64decode(data)

    def close(self) -> None:
        self._http.close()


# ------------------------------------------------------------------
# Email parsing helpers
# ------------------------------------------------------------------

def _extract_email(from_header: str) -> str:
    """Extract bare email address from a From header value."""
    _, addr = email.utils.parseaddr(from_header)
    return addr.lower()


def _parse_email_date(date_header: str) -> datetime | None:
    """Parse an RFC 2822 Date header into a timezone-aware datetime."""
    try:
        return email.utils.parsedate_to_datetime(date_header)
    except (ValueError, TypeError):
        return None


def _decode_body_data(data: str) -> str:
    """Decode base64url-encoded body data."""
    if not data:
        return ""
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def _strip_html_tags(html: str) -> str:
    """Crude HTML-to-text: strip tags and decode common entities."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    for entity, char in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                          ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")]:
        text = text.replace(entity, char)
    return text.strip()


def _collect_text_parts(
    payload: dict[str, Any],
    result: dict[str, str],
) -> None:
    """Recursively collect text/plain and text/html from MIME parts."""
    mime = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data", "")

    if mime == "text/plain" and "plain" not in result:
        decoded = _decode_body_data(data)
        if decoded:
            result["plain"] = decoded
    elif mime == "text/html" and "html" not in result:
        decoded = _decode_body_data(data)
        if decoded:
            result["html"] = decoded

    for part in payload.get("parts", []):
        _collect_text_parts(part, result)


def _extract_text_body(msg: dict[str, Any]) -> str:
    """Extract text content from a Gmail message payload.

    Recursively searches MIME parts. Prefers text/plain; falls back
    to text/html with tag stripping.
    """
    result: dict[str, str] = {}
    _collect_text_parts(msg.get("payload", {}), result)

    if "plain" in result:
        return result["plain"]
    if "html" in result:
        return _strip_html_tags(result["html"])
    return ""


def _strip_quoted_content(text: str) -> str:
    """Strip quoted replies and email signatures from body text."""
    text = _ON_WROTE_RE.sub("", text)
    text = _QUOTE_RE.sub("", text)
    text = _SIGNATURE_RE.sub("", text)
    return text.strip()


def _collect_attachments(
    payload: dict[str, Any],
    result: list[dict[str, Any]],
) -> None:
    """Recursively find attachment parts (parts with filename + attachmentId)."""
    body = payload.get("body", {})
    attachment_id = body.get("attachmentId")
    if attachment_id:
        # Extract filename from part headers
        filename = ""
        for h in payload.get("headers", []):
            if h["name"].lower() in ("filename", "content-disposition"):
                # Content-Disposition may contain filename=
                val = h["value"]
                if "filename=" in val.lower():
                    # Parse filename="xxx" or filename=xxx
                    match = re.search(r'filename="?([^";\n]+)"?', val, re.IGNORECASE)
                    if match:
                        filename = match.group(1).strip()
                elif h["name"].lower() == "filename":
                    filename = val
        if not filename:
            filename = payload.get("filename", "attachment")
        result.append({
            "filename": filename,
            "attachment_id": attachment_id,
            "mime_type": payload.get("mimeType", "application/octet-stream"),
            "size": body.get("size", 0),
        })

    for part in payload.get("parts", []):
        _collect_attachments(part, result)


def _is_automated_email(headers: dict[str, str]) -> bool:
    """Detect automated/notification emails via standard headers."""
    if "list-unsubscribe" in headers:
        return True
    precedence = headers.get("precedence", "").lower()
    if precedence in ("bulk", "list", "junk"):
        return True
    auto_submitted = headers.get("auto-submitted", "").lower()
    if auto_submitted and auto_submitted != "no":
        return True
    return False


def _matches_ignore_list(from_addr: str, ignore_senders: list[str]) -> bool:
    """Check if sender matches any pattern in the ignore list."""
    addr = from_addr.lower()
    for pattern in ignore_senders:
        if pattern.lower() in addr:
            return True
    return False


# ------------------------------------------------------------------
# GmailAdapter
# ------------------------------------------------------------------

class GmailAdapter:
    """Gmail channel adapter.

    Polls the agent's own Gmail inbox for unread messages and creates
    ``InboundMessage`` items for the AgentCore queue.  Responses are
    sent as email replies via Gmail API.  Processed emails are
    automatically archived.
    """

    channel_name = "gmail"
    priority = 1  # same as LINE

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        contact_map: ContactMap,
        thread_registry: ThreadRegistry,
        thread_max_age_days: int = 7,
        poll_interval: int = 45,
        max_age_minutes: int | None = None,
        ignore_senders: list[str] | None = None,
    ) -> None:
        self._gmail = _GmailClient(client_id, client_secret, refresh_token)
        self._contact_map = contact_map
        self._thread_registry = thread_registry
        self._thread_max_age_days = thread_max_age_days
        self._poll_interval = poll_interval
        self._max_age_minutes = max_age_minutes
        self._ignore_senders = ignore_senders or []

        # Temp directory for downloaded attachments
        self._tmp_dir = Path(tempfile.gettempdir()) / f"lincy_gmail_{os.getpid()}"
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

        self._agent: AgentCore | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Track message IDs already enqueued this session to avoid duplicates
        self._processed_ids: set[str] = set()
        self._processed_ids_max = 10_000

    @property
    def attachments_dir(self) -> str:
        """Temp directory for downloaded attachments (for allowed_paths)."""
        return str(self._tmp_dir)

    # -- ChannelAdapter protocol --------------------------------------

    def start(self, agent: AgentCore) -> None:
        self._agent = agent
        self._thread = threading.Thread(
            target=self._poll_loop, name="gmail-poll", daemon=True,
        )
        self._thread.start()

    def send(self, message: OutboundMessage) -> None:
        reply_to = message.metadata.get("reply_to")
        if not reply_to:
            logger.warning("Gmail send: no reply_to in metadata, skipping")
            return
        subject = message.metadata.get("subject")
        thread_id = message.metadata.get("thread_id")
        # message_id = RFC 2822 Message-ID of the inbound email.
        # Used as In-Reply-To header so the *recipient* sees a threaded
        # reply.  threadId alone only controls the sender's own mailbox.
        # See send_message.py _resolve_route for why Gmail preserves this.
        in_reply_to = message.metadata.get("message_id")

        # No thread context and no explicit subject -> try continuing
        # the most recent thread with this contact via registry.
        if thread_id is None and subject is None:
            cached = self._thread_registry.get("gmail", reply_to)
            if cached and not self._is_stale(cached):
                thread_id = cached.get("thread_id")
                in_reply_to = cached.get("message_id")
                subject = cached.get("subject", "")
                logger.info(
                    "Gmail send: continuing cached thread %s for %s",
                    thread_id, reply_to,
                )

        subject = subject or ""
        body = markdown_to_plaintext(message.content)
        try:
            result = self._gmail.send(
                to=reply_to,
                subject=subject,
                body=body,
                thread_id=thread_id,
                in_reply_to=in_reply_to,
                attachments=message.attachments or None,
            )
            logger.info("Gmail reply sent to %s", reply_to)
            # Update thread registry with the sent message's context
            self._update_registry_after_send(
                reply_to, result, subject, in_reply_to,
                requested_thread_id=thread_id,
            )
        except Exception as exc:
            logger.error("Gmail send failed to %s: %s", reply_to, exc)

    def _is_stale(self, entry: dict[str, Any]) -> bool:
        """Check if a thread registry entry is too old to use."""
        last = entry.get("last_activity")
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
            age = tz_now() - last_dt
            return age.total_seconds() > self._thread_max_age_days * 86400
        except (ValueError, TypeError):
            return True

    def _update_registry_after_send(
        self,
        reply_to: str,
        result: dict[str, Any],
        subject: str,
        fallback_message_id: str | None,
        requested_thread_id: str | None = None,
    ) -> None:
        """Update thread registry after a successful send."""
        new_thread_id = result.get("threadId")
        if not new_thread_id:
            return
        # Fetch sent message to get its RFC 2822 Message-ID header
        sent_message_id = fallback_message_id or ""
        try:
            sent_msg = self._gmail.get_message(result["id"])
            for h in sent_msg.get("payload", {}).get("headers", []):
                if h["name"].lower() == "message-id":
                    sent_message_id = h["value"]
                    break
        except Exception:
            logger.debug(
                "Could not fetch sent message headers for registry update"
            )
        # Carry forward superseded map; detect thread split
        cached = self._thread_registry.get("gmail", reply_to)
        superseded: dict[str, str] = {}
        if cached:
            superseded = dict(cached.get("superseded", {}))
        if (
            requested_thread_id
            and new_thread_id != requested_thread_id
        ):
            superseded[requested_thread_id] = new_thread_id
            logger.info(
                "Gmail thread split: %s -> %s for %s",
                requested_thread_id, new_thread_id, reply_to,
            )
        data: dict[str, Any] = {
            "thread_id": new_thread_id,
            "message_id": sent_message_id,
            "subject": subject,
            "last_activity": tz_now().isoformat(),
        }
        if superseded:
            data["superseded"] = superseded
        self._thread_registry.update("gmail", reply_to, data)

    def on_turn_start(self, channel: str) -> None:
        pass

    def on_turn_complete(self) -> None:
        pass

    def stop(self) -> None:
        self._stop_event.set()
        self._gmail.close()

    # -- Polling loop -------------------------------------------------

    def _build_query_extra(self) -> str:
        """Build additional Gmail query filter (e.g. ``after:`` for max_age)."""
        if self._max_age_minutes is None:
            return ""
        cutoff = int(time.time()) - self._max_age_minutes * 60
        return f"after:{cutoff}"

    def _poll_loop(self) -> None:
        assert self._agent is not None
        while not self._stop_event.is_set():
            try:
                self._check_inbox()
            except Exception:
                logger.exception("Gmail poll error")
            self._stop_event.wait(self._poll_interval)

    def _check_inbox(self) -> None:
        query_extra = self._build_query_extra()
        unread = self._gmail.list_unread(query_extra)
        for stub in unread:
            msg_id = stub["id"]
            if msg_id in self._processed_ids:
                continue
            try:
                self._process_message(msg_id)
            except Exception:
                logger.exception("Failed to process Gmail message %s", msg_id)

    def _process_message(self, msg_id: str) -> None:
        """Fetch, parse, enqueue, and archive a single email."""
        assert self._agent is not None
        full = self._gmail.get_message(msg_id)
        self._processed_ids.add(msg_id)
        if len(self._processed_ids) > self._processed_ids_max:
            self._processed_ids = set(list(self._processed_ids)[-self._processed_ids_max:])

        # Parse headers (lowercase keys for easy lookup)
        headers = {
            h["name"].lower(): h["value"]
            for h in full.get("payload", {}).get("headers", [])
        }
        from_addr = _extract_email(headers.get("from", ""))
        subject = headers.get("subject", "")
        thread_id = full.get("threadId")

        # Auto-forwarded emails: reply to the forwarder, not the
        # original sender, to avoid leaking messages to third parties.
        forwarded_for = headers.get("x-forwarded-for", "")
        if forwarded_for:
            reply_addr = _extract_email(forwarded_for)
            logger.info(
                "Auto-forwarded email detected: from=%s, forwarded_for=%s",
                from_addr, reply_addr,
            )
        else:
            reply_addr = from_addr

        # Filter: automated/notification emails
        if _is_automated_email(headers):
            logger.info("Skipping automated email from %s: %s", from_addr, subject)
            self._gmail.archive(msg_id)
            return
        if _matches_ignore_list(from_addr, self._ignore_senders):
            logger.info("Skipping ignored sender %s: %s", from_addr, subject)
            self._gmail.archive(msg_id)
            return

        # Extract and clean body
        body = _extract_text_body(full)
        body = _strip_quoted_content(body)
        if not body and not subject:
            logger.info("Skipping empty email from %s", from_addr)
            self._gmail.archive(msg_id)
            return

        # Parse email Date header for accurate timestamp
        email_date = _parse_email_date(headers.get("date", ""))
        timestamp = email_date or tz_now()

        # Resolve sender via contact map (Layer 1)
        sender = self._contact_map.resolve("gmail", reply_addr) or reply_addr

        # Download attachments (synchronous — completes before LLM sees message)
        attachment_metas: list[dict[str, Any]] = []
        _collect_attachments(full.get("payload", {}), attachment_metas)
        attachments: list[dict[str, Any]] = []
        for att in attachment_metas:
            record: dict[str, Any] = {
                "filename": att["filename"],
                "content_type": att["mime_type"],
                "local_path": None,
                "download_note": None,
            }
            if att["size"] > _MAX_ATTACHMENT_BYTES:
                record["download_note"] = "too large, not downloaded"
                attachments.append(record)
                continue
            try:
                raw_bytes = self._gmail.get_attachment(msg_id, att["attachment_id"])
                msg_dir = self._tmp_dir / f"msg_{msg_id}"
                msg_dir.mkdir(parents=True, exist_ok=True)
                file_path = msg_dir / att["filename"]
                file_path.write_bytes(raw_bytes)
                record["local_path"] = str(file_path)
            except Exception:
                logger.exception("Failed to download attachment %s", att["filename"])
                record["download_note"] = "download failed"
            attachments.append(record)
        attachment_lines = format_attachment_lines(attachments)

        # Build content: always include subject for topic context
        # Strip leading "Re: " prefixes for cleanliness
        clean_subject = re.sub(r"^(?:Re:\s*)+", "", subject, flags=re.IGNORECASE).strip()
        if not body:
            content = clean_subject or "(empty)"
        elif clean_subject:
            content = f"[Subject: {clean_subject}]\n{body}"
        else:
            content = body

        if attachment_lines:
            content += "\n\n[Attachments]\n" + "\n".join(attachment_lines)

        # Reply metadata for send()
        reply_subject = subject
        if subject and not subject.lower().startswith("re:"):
            reply_subject = f"Re: {subject}"

        # Update thread registry with inbound thread context.
        # If the inbound threadId was superseded (e.g. Gmail split at
        # ~100 messages), resolve to the current thread so the registry
        # isn't reverted to the old, full thread.
        effective_thread_id = thread_id
        cached = self._thread_registry.get("gmail", reply_addr)
        superseded: dict[str, str] = {}
        if cached:
            superseded = dict(cached.get("superseded", {}))
            # Follow the chain (A->B->C) in case of multiple splits
            tid = thread_id
            for _ in range(10):  # guard against cycles
                if tid and tid in superseded:
                    tid = superseded[tid]
                else:
                    break
            if tid != thread_id:
                effective_thread_id = tid
                logger.debug(
                    "Inbound threadId %s superseded -> %s for %s",
                    thread_id, effective_thread_id, reply_addr,
                )

        reg_data: dict[str, Any] = {
            "thread_id": effective_thread_id,
            "message_id": headers.get("message-id", ""),
            "subject": reply_subject,
            "last_activity": tz_now().isoformat(),
        }
        if superseded:
            reg_data["superseded"] = superseded
        self._thread_registry.update("gmail", reply_addr, reg_data)

        msg = InboundMessage(
            channel="gmail",
            content=content,
            priority=self.priority,
            sender=sender,
            timestamp=timestamp,
            metadata={
                "reply_to": reply_addr,
                "subject": reply_subject,
                "thread_id": effective_thread_id,
                "message_id": headers.get("message-id", ""),
                "gmail_msg_id": msg_id,
            },
        )
        self._agent.enqueue(msg)
        self._gmail.archive(msg_id)
