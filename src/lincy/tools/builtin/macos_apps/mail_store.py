"""Read Mail.app data straight from its on-disk store.

Mail has no public framework API and every Apple Event costs about a second,
so reads go to Mail's own files instead: the SQLite ``Envelope Index`` for
metadata and the ``.emlx`` files for bodies and attachments. The process needs
Full Disk Access to read ``~/Library/Mail``.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import email
from email.message import EmailMessage
import email.policy
from html.parser import HTMLParser
import mimetypes
import plistlib
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .apple_events import automation_problem
from .runtime import _datetime_to_app_iso

MAIL_ROOT = Path.home() / "Library" / "Mail" / "V10"
SCOPE_ORDER = ("inbox", "sent", "drafts", "junk", "trash", "outbox")
# Outgoing scopes are dated by when the user sent them, the rest by arrival.
_SENT_DATE_SCOPES = frozenset({"sent", "drafts", "outbox"})
# Mail caches the IMAP SPECIAL-USE flags (RFC 6154) per mailbox in each
# account's .mboxCache.plist; these are the bits Mail uses to pick the
# account's sent/drafts/junk/trash mailboxes.
_SPECIAL_USE_SCOPES = {4096: "drafts", 16384: "junk", 32768: "sent", 65536: "trash"}
_NOT_DOWNLOADED = "message body is not downloaded locally; open the message in Mail.app to download it"

_MESSAGE_COLUMNS = """
    m.ROWID, mb.url, COALESCE(m.subject_prefix, '') || COALESCE(s.subject, ''),
    a.address, a.comment, g.message_id_header, m.date_received, m.date_sent,
    m.read, m.flagged, m.deleted, m.size,
    (SELECT COUNT(*) FROM attachments t WHERE t.message = m.ROWID)
"""
_MESSAGE_JOINS = """
    JOIN mailboxes mb ON mb.ROWID = m.mailbox
    LEFT JOIN subjects s ON s.ROWID = m.subject
    LEFT JOIN addresses a ON a.ROWID = m.sender
    LEFT JOIN message_global_data g ON g.ROWID = m.global_message_id
"""


def mail_permission_problems(root: Path = MAIL_ROOT) -> list[str]:
    """Return blocking problems for reading Mail data and for the Mail JXA trash call."""
    problems = []
    try:
        MailStore(root).connect().close()
    except RuntimeError as exc:
        problems.append(str(exc))
    automation = automation_problem("com.apple.mail", "Mail")
    if automation:
        problems.append(automation)
    return problems


def parse_message_ref(message_ref: str) -> int | None:
    """Return the Envelope Index ROWID behind a 'mailmsg:<id>' ref.

    A bare numeric id is accepted too, as the old JXA backend did, so ids the
    agent stored from earlier output keep working.
    """
    match = re.fullmatch(r"(?:mailmsg:)?(\d+)", message_ref.strip())
    return int(match.group(1)) if match else None


class _HTMLText(HTMLParser):
    """Collect the visible text of an HTML body, one line per block element."""

    _SKIP = frozenset({"head", "script", "style"})
    _BLOCK = frozenset({
        "br", "p", "div", "tr", "li", "table", "blockquote", "pre",
        "h1", "h2", "h3", "h4", "h5", "h6",
    })

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def _html_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html)
    parser.close()
    lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _decoded_text(part: EmailMessage) -> str:
    payload = part.get_payload(decode=True) or b""
    try:
        return payload.decode(part.get_content_charset() or "utf-8", "replace")
    except LookupError:
        # Real mail carries bogus charset labels; UTF-8 is the best guess left.
        return payload.decode("utf-8", "replace")


def _is_attached_message(part: EmailMessage) -> bool:
    return part.get_content_type() == "message/rfc822" and (
        part.get_filename() is not None or part.get_content_disposition() == "attachment"
    )


def _part_bytes(part: EmailMessage) -> bytes:
    # A message/rfc822 payload parses into a message object, which
    # get_payload(decode=True) does not serialize.
    if part.get_content_type() == "message/rfc822":
        return part.get_payload(0).as_bytes()
    return part.get_payload(decode=True) or b""


def _leaf_parts(message: EmailMessage) -> list[tuple[str, EmailMessage]]:
    """Return non-multipart parts keyed by IMAP part number ('1', '1.2', ...).

    Mail names attachment folders and Envelope Index attachment ids by these
    numbers. An inline message/rfc822 part P is opened up like IMAP does: its
    body parts are numbered P.1, P.2, ... An attached message stays one leaf
    so it can be exported whole as .eml.
    """
    leaves: list[tuple[str, EmailMessage]] = []

    def walk(part: EmailMessage, prefix: str) -> None:
        if part.get_content_maintype() != "multipart":
            leaves.append((f"{prefix}1", part))
            return
        for index, child in enumerate(part.iter_parts(), start=1):
            part_id = f"{prefix}{index}"
            if child.get_content_maintype() == "multipart":
                walk(child, f"{part_id}.")
            elif child.get_content_type() == "message/rfc822" and not _is_attached_message(child):
                walk(child.get_payload(0), f"{part_id}.")
            else:
                leaves.append((part_id, child))

    walk(message, "")
    return leaves


# Mail lists PGP signatures as attachments but not S/MIME ones.
_SMIME_SIGNATURES = frozenset({"application/pkcs7-signature", "application/x-pkcs7-signature"})


def _is_body_text(part: EmailMessage) -> bool:
    return (
        part.get_content_maintype() == "text"
        and part.get_content_disposition() != "attachment"
        and part.get_filename() is None
    )


def _is_attachment(part: EmailMessage) -> bool:
    # Matches the Envelope Index attachments table, checked against a real
    # mailbox: alternative text bodies (text/x-amp-html, ...) and message/*
    # reports are not attachments, but inline text/calendar invites are.
    # A forwarded message with a filename or attachment disposition is one.
    if part.get_content_type() == "text/calendar" or _is_attached_message(part):
        return True
    return not (
        _is_body_text(part)
        or part.get_content_maintype() == "message"
        or part.get_content_type() in _SMIME_SIGNATURES
    )


def _not_downloaded(part: EmailMessage) -> bool:
    # Partial .emlx files keep the MIME headers of parts Mail has not fetched
    # but drop their bodies; X-Apple-Content-Length records the real size.
    return not part.get_payload() and int(part.get("X-Apple-Content-Length") or 0) > 0


def public_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Drop the internal '_' keys that locate a row on disk and in Mail.app."""
    return {key: value for key, value in row.items() if not key.startswith("_")}


def _unix_to_iso(value: int | None) -> str | None:
    if value is None:
        return None
    return _datetime_to_app_iso(datetime.fromtimestamp(value, tz=timezone.utc))


def _unique_path(directory: Path, name: str) -> Path:
    target = directory / name
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 2
    while target.exists():
        target = directory / f"{stem} ({counter}){suffix}"
        counter += 1
    return target


class MailStore:
    """Read-only view over one Mail data directory (``~/Library/Mail/V10``)."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._index = root / "MailData" / "Envelope Index"

    def connect(self) -> sqlite3.Connection:
        """Open the Envelope Index read-only; Mail keeps writing it in WAL mode."""
        # A fresh connection per call sees Mail's latest committed WAL frames.
        uri = f"{self._index.as_uri()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
            connection.execute("SELECT 1 FROM messages LIMIT 1").fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"Cannot read the Mail.app index at {self._index} ({exc}). "
                "Make sure Mail.app has an account set up, and grant Full Disk Access "
                "to the app that runs lincy (e.g. Terminal) in System Settings > "
                "Privacy & Security > Full Disk Access, then restart lincy."
            ) from exc
        return connection

    # -- mailbox mapping -------------------------------------------------

    def _special_use(self, account: str) -> dict[tuple[str, ...], int]:
        cache = self._root / account / ".mboxCache.plist"
        if not cache.exists():
            return {}
        attributes: dict[tuple[str, ...], int] = {}

        def walk(children: dict[str, Any], prefix: tuple[str, ...]) -> None:
            for key, node in children.items():
                path = (*prefix, node.get("MailboxPathComponent", key))
                attributes[path] = node.get("IMAPMailboxAttributes", 0)
                walk(node.get("IMAPMailboxChildren", {}), path)

        walk(plistlib.loads(cache.read_bytes()).get("mboxes", {}), ())
        return attributes

    def scope_mailboxes(self, connection: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
        """Map each unified scope to the Envelope Index mailboxes Mail puts in it."""
        scopes: dict[str, list[dict[str, Any]]] = {scope: [] for scope in SCOPE_ORDER}
        special_use: dict[str, dict[tuple[str, ...], int]] = {}
        for rowid, url in connection.execute("SELECT ROWID, url FROM mailboxes"):
            parts = urlsplit(url)
            path = tuple(unquote(parts.path).strip("/").split("/"))
            if parts.scheme == "local" and path == ("Outbox",):
                scope = "outbox"
            elif len(path) == 1 and path[0].upper() == "INBOX":
                scope = "inbox"
            else:
                if parts.netloc not in special_use:
                    special_use[parts.netloc] = self._special_use(parts.netloc)
                bits = special_use[parts.netloc].get(path, 0)
                scope = next(
                    (name for bit, name in _SPECIAL_USE_SCOPES.items() if bits & bit), None
                )
            if scope:
                scopes[scope].append({"id": rowid, "account": parts.netloc, "path": "/".join(path)})
        return scopes

    # -- message files -----------------------------------------------------

    def message_file(self, mailbox_url: str, rowid: int) -> Path | None:
        """Locate <ROWID>.emlx or <ROWID>.partial.emlx for a message.

        Layout: <account>/<a>.mbox/<b>.mbox/<store uuid>/Data/<digits>/Messages/,
        where <digits> are the digits of ROWID // 1000 reversed, one per folder.
        """
        parts = urlsplit(mailbox_url)
        mailbox_dir = self._root / parts.netloc
        for component in unquote(parts.path).strip("/").split("/"):
            mailbox_dir /= f"{component}.mbox"
        bucket = "".join(f"{digit}/" for digit in reversed(str(rowid // 1000))) if rowid >= 1000 else ""
        return next(mailbox_dir.glob(f"*/Data/{bucket}Messages/{rowid}.*emlx"), None)

    @staticmethod
    def read_emlx(path: Path) -> EmailMessage:
        """Parse an .emlx file: a byte-count line, the RFC 822 message, a plist."""
        data = path.read_bytes()
        count, _, rest = data.partition(b"\n")
        return email.message_from_bytes(rest[: int(count)], policy=email.policy.default)

    @staticmethod
    def body_text(message: EmailMessage) -> str | None:
        """Return readable body text, or None when Mail has not downloaded it."""
        plain: list[str] = []
        html: list[str] = []
        missing = False
        for _, part in _leaf_parts(message):
            if part.get_content_type() not in ("text/plain", "text/html") or not _is_body_text(part):
                continue
            if _not_downloaded(part):
                missing = True
            elif part.get_content_subtype() == "plain":
                plain.append(_decoded_text(part))
            elif part.get_content_subtype() == "html":
                html.append(_html_text(_decoded_text(part)))
        # Some senders put an empty text/plain next to the real HTML body.
        texts = plain if any(text.strip() for text in plain) else html
        if not texts and missing:
            return None
        return "\n\n".join(texts).strip()

    @staticmethod
    def attachments(message: EmailMessage, message_path: Path, rowid: int) -> list[dict[str, Any]]:
        """List attachments; partial messages keep them under Attachments/<ROWID>/<part>/."""
        rows = []
        for part_id, part in _leaf_parts(message):
            if not _is_attachment(part):
                continue
            folder = message_path.parent.parent / "Attachments" / str(rowid) / part_id
            stored = next((path for path in folder.glob("*") if path.is_file()), None)
            name = part.get_filename() or (
                f"attachment-{part_id}{mimetypes.guess_extension(part.get_content_type()) or ''}"
            )
            if stored is not None:
                size = stored.stat().st_size
            elif _not_downloaded(part):
                size = int(part["X-Apple-Content-Length"])
            else:
                size = len(_part_bytes(part))
            rows.append({
                "id": part_id,
                "name": name,
                "mime_type": part.get_content_type(),
                "file_size": size,
                "downloaded": stored is not None or not _not_downloaded(part),
                "_part": part,
                "_stored": stored,
            })
        return rows

    # -- queries -----------------------------------------------------------

    def catalog(self) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            mailboxes = self.scope_mailboxes(connection)
            scopes = []
            for scope in SCOPE_ORDER:
                ids = [mailbox["id"] for mailbox in mailboxes[scope]]
                marks = ",".join("?" * len(ids))
                total, unread = connection.execute(
                    f"""
                    SELECT COUNT(*), COALESCE(SUM(read = 0), 0) FROM messages
                    WHERE deleted = 0 AND (mailbox IN ({marks})
                      OR ROWID IN (SELECT message_id FROM labels WHERE mailbox_id IN ({marks})))
                    """,
                    ids + ids,
                ).fetchone()
                names = list(dict.fromkeys(mailbox["path"].rsplit("/", 1)[-1] for mailbox in mailboxes[scope]))
                scopes.append({
                    "scope": scope,
                    "name": ", ".join(names) or scope,
                    "mailboxes": [mailbox["path"] for mailbox in mailboxes[scope]],
                    "message_count": total,
                    "unread_count": unread,
                })
        return {"ok": True, "scopes": scopes, "count": len(scopes)}

    def search_window(
        self,
        connection: sqlite3.Connection,
        scopes: list[str],
        *,
        after: float | None,
        before: float | None,
        scan_limit: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return the newest scan_limit messages in scopes and date range.

        A message in several scopes (Gmail labels) is reported once, under the
        first scope in SCOPE_ORDER. The bool says whether more messages exist.
        """
        mailboxes = self.scope_mailboxes(connection)
        members = []
        params: list[Any] = []
        for scope in scopes:
            ids = [mailbox["id"] for mailbox in mailboxes[scope]]
            if not ids:
                continue
            marks = ",".join("?" * len(ids))
            rank = SCOPE_ORDER.index(scope)
            members.append(f"SELECT ROWID, {rank} FROM messages WHERE mailbox IN ({marks})")
            members.append(f"SELECT message_id, {rank} FROM labels WHERE mailbox_id IN ({marks})")
            params += ids + ids
        if not members:
            return [], False
        sent_ranks = ",".join(str(SCOPE_ORDER.index(scope)) for scope in _SENT_DATE_SCOPES)
        conditions = ["m.deleted = 0"]
        if after is not None:
            conditions.append("sort_date >= ?")
            params.append(after)
        if before is not None:
            conditions.append("sort_date <= ?")
            params.append(before)
        params.append(scan_limit + 1)
        rows = connection.execute(
            f"""
            WITH member(message, rank) AS ({" UNION ALL ".join(members)}),
            best AS (SELECT message, MIN(rank) AS rank FROM member GROUP BY message)
            SELECT best.rank,
              CASE WHEN best.rank IN ({sent_ranks})
                THEN COALESCE(m.date_sent, m.date_received)
                ELSE COALESCE(m.date_received, m.date_sent) END AS sort_date,
              {_MESSAGE_COLUMNS}
            FROM best JOIN messages m ON m.ROWID = best.message
            {_MESSAGE_JOINS}
            WHERE {" AND ".join(conditions)}
            ORDER BY sort_date DESC, m.ROWID DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        window = [self._row(row[2:], SCOPE_ORDER[row[0]]) for row in rows[:scan_limit]]
        return window, len(rows) > scan_limit

    def message(self, connection: sqlite3.Connection, rowid: int) -> dict[str, Any] | None:
        """Return one message row with every scope it belongs to, or None."""
        row = connection.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages m {_MESSAGE_JOINS} WHERE m.ROWID = ?",
            (rowid,),
        ).fetchone()
        if row is None:
            return None
        mailbox_ids = {
            mailbox_id
            for (mailbox_id,) in connection.execute(
                "SELECT mailbox FROM messages WHERE ROWID = ? "
                "UNION SELECT mailbox_id FROM labels WHERE message_id = ?",
                (rowid, rowid),
            )
        }
        mailboxes = self.scope_mailboxes(connection)
        scopes = [
            scope for scope in SCOPE_ORDER
            if any(mailbox["id"] in mailbox_ids for mailbox in mailboxes[scope])
        ]
        result = self._row(row, scopes[0] if scopes else None)
        result["scopes"] = scopes
        result["_trash_mailbox"] = next(
            (mailbox["path"] for mailbox in mailboxes["trash"] if mailbox["account"] == result["_account"]),
            None,
        )
        return result

    @staticmethod
    def _row(row: tuple[Any, ...], scope: str | None) -> dict[str, Any]:
        (rowid, url, subject, address, comment, message_id, received, sent,
         read, flagged, deleted, size, attachment_count) = row
        if scope in _SENT_DATE_SCOPES:
            date, kind = (sent, "sent") if sent is not None else (received, "received")
        else:
            date, kind = (received, "received") if received is not None else (sent, "sent")
        parts = urlsplit(url)
        return {
            "message_ref": f"mailmsg:{rowid}",
            "id": rowid,
            "scope": scope,
            "subject": subject,
            # Close to Mail's AppleScript sender; Mail quotes some display
            # names from the raw header, which the index does not keep.
            "sender": f"{comment} <{address}>" if comment else address,
            # AppleScript reports Message-ID without the angle brackets.
            "message_id": message_id.strip("<>") if message_id else None,
            "date": _unix_to_iso(date),
            "date_kind": kind,
            "date_received": _unix_to_iso(received),
            "date_sent": _unix_to_iso(sent),
            "read": bool(read),
            "flagged": bool(flagged),
            "deleted": bool(deleted),
            "message_size": size,
            "attachment_count": attachment_count,
            "_url": url,
            "_account": parts.netloc,
            # "On My Mac" mailboxes hang off the application, not an account.
            "_local": parts.scheme == "local",
            "_mailbox_path": unquote(parts.path).strip("/"),
        }

    def message_details(self, row: dict[str, Any], *, include_content: bool, max_chars: int) -> dict[str, Any]:
        """Add emlx-derived fields (reply_to, attachments, content) to a row."""
        path = self.message_file(row["_url"], row["id"])
        details = public_fields(row)
        details["junk"] = "junk" in row.get("scopes", [row["scope"]])
        details["content_available"] = False
        details["content_error"] = None
        if path is None:
            details.update(reply_to=None, attachments=[], content=None, content_chars=None,
                           content_truncated=False, content_error=_NOT_DOWNLOADED)
            return details
        message = self.read_emlx(path)
        attachments = self.attachments(message, path, row["id"])
        details["reply_to"] = str(message["Reply-To"]) if message["Reply-To"] else None
        details["attachments"] = [public_fields(item) for item in attachments]
        details["attachment_count"] = len(attachments)
        details.update(content=None, content_chars=None, content_truncated=False)
        if include_content:
            text = self.body_text(message)
            if text is None:
                details["content_error"] = _NOT_DOWNLOADED
            else:
                details.update(
                    content=text[:max_chars],
                    content_chars=len(text),
                    content_truncated=len(text) > max_chars,
                    content_available=True,
                )
        return details

    def export_attachments(
        self, row: dict[str, Any], attachment_ids: list[str], destination: Path,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Copy attachments to destination; returns (exported rows, written files)."""
        path = self.message_file(row["_url"], row["id"])
        if path is None:
            raise RuntimeError(f"{row['message_ref']}: {_NOT_DOWNLOADED}")
        attachments = self.attachments(self.read_emlx(path), path, row["id"])
        selected = [item for item in attachments if not attachment_ids or item["id"] in attachment_ids]
        unknown = sorted(set(attachment_ids) - {item["id"] for item in attachments})
        if unknown:
            raise RuntimeError(f"unknown attachment ids: {', '.join(unknown)}")
        missing = [item["id"] for item in selected if not item["downloaded"]]
        if missing:
            raise RuntimeError(
                f"attachments not downloaded locally: {', '.join(missing)}; "
                "open the message in Mail.app to download them"
            )
        destination.mkdir(parents=True, exist_ok=True)
        exported, files = [], []
        for item in selected:
            name = item["name"].replace("/", "_").replace(":", "_").lstrip(".") or f"attachment-{item['id']}"
            target = _unique_path(destination, name)
            if item["_stored"] is not None:
                shutil.copyfile(item["_stored"], target)
            else:
                target.write_bytes(_part_bytes(item["_part"]))
            exported.append(public_fields(item))
            files.append(str(target))
        return exported, files
