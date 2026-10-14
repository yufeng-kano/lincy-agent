"""Tests for mail_tool reading a synthetic Mail.app store (Envelope Index + emlx)."""

from datetime import datetime, timezone
import json
from pathlib import Path
import plistlib
import sqlite3

import pytest

from lincy.tools.builtin.macos_apps import MacOSAppBridge, create_mail_tool
from lincy.tools.builtin.macos_apps.mail_store import MailStore, mail_permission_problems, parse_message_ref

ACCOUNT = "ACCT-UUID"
LOCAL = "LOCAL-UUID"
STORE = "STORE-UUID"

# Only the tables and columns mail_store reads.
_SCHEMA = """
CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT NOT NULL, source INTEGER);
CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT NOT NULL);
CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT NOT NULL, comment TEXT NOT NULL);
CREATE TABLE message_global_data (ROWID INTEGER PRIMARY KEY, message_id_header TEXT);
CREATE TABLE messages (
  ROWID INTEGER PRIMARY KEY, global_message_id INTEGER NOT NULL, sender INTEGER,
  subject_prefix TEXT, subject INTEGER NOT NULL, date_sent INTEGER, date_received INTEGER,
  mailbox INTEGER NOT NULL, read INTEGER NOT NULL DEFAULT 0, flagged INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0, size INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE labels (message_id INTEGER, mailbox_id INTEGER, PRIMARY KEY(message_id, mailbox_id)) WITHOUT ROWID;
CREATE TABLE attachments (ROWID INTEGER PRIMARY KEY, message INTEGER NOT NULL, attachment_id TEXT, name TEXT);
"""

_MAILBOXES = {
    1: (f"imap://{ACCOUNT}/%5BGmail%5D/All%20Mail", ("[Gmail]", "All Mail")),
    2: (f"imap://{ACCOUNT}/INBOX", ("INBOX",)),
    3: (f"imap://{ACCOUNT}/%5BGmail%5D/Sent%20Mail", ("[Gmail]", "Sent Mail")),
    4: (f"imap://{ACCOUNT}/%5BGmail%5D/Trash", ("[Gmail]", "Trash")),
    5: (f"imap://{ACCOUNT}/%5BGmail%5D/Spam", ("[Gmail]", "Spam")),
    6: (f"local://{LOCAL}/Outbox", ("Outbox",)),
    7: (f"imap://{ACCOUNT}/Work", ("Work",)),
}


def _utc(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


def _emlx(raw: str) -> bytes:
    body = raw.replace("\n", "\r\n").encode()
    return f"{len(body)}\n".encode() + body + plistlib.dumps({"flags": 0})


_FULL = """From: Alice <alice@example.com>
Reply-To: billing@example.com
Subject: Invoice May
Message-ID: <m1001@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="b1"

--b1
Content-Type: text/plain; charset=utf-8

hello body keyword
--b1
Content-Type: text/plain; name="note.txt"
Content-Disposition: attachment; filename="note.txt"
Content-Transfer-Encoding: base64

YXR0YWNoZWQgdGV4dA==
--b1--
"""

_HTML = """From: Me <me@example.com>
Subject: Sent report
Message-ID: <m1002@example.com>
MIME-Version: 1.0
Content-Type: text/html; charset=utf-8

<html><head><style>p {color: red}</style></head><body><p>Quarterly</p><p>numbers</p></body></html>
"""

_PARTIAL_WITH_STORED_ATTACHMENT = """From: Bob <bob@example.com>
Subject: Report attached
Message-ID: <m5@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="b2"

--b2
Content-Type: text/plain; charset=utf-8

see report
--b2
Content-Type: application/pdf; name="report.pdf"
Content-Disposition: attachment; filename="report.pdf"
Content-Transfer-Encoding: base64
X-Apple-Content-Length: 4096

--b2--
"""

_PARTIAL_WITHOUT_BODY = """From: Carol <carol@example.com>
Subject: Large message
Message-ID: <m2001@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="b3"

--b3
Content-Type: text/plain; charset=utf-8
X-Apple-Content-Length: 120

--b3
Content-Type: application/zip; name="data.zip"
Content-Disposition: attachment; filename="data.zip"
Content-Transfer-Encoding: base64
X-Apple-Content-Length: 9000

--b3--
"""


def _message_dir(root: Path, mailbox: tuple[str, ...], rowid: int) -> Path:
    path = root / ACCOUNT
    for component in mailbox:
        path /= f"{component}.mbox"
    bucket = list(reversed(str(rowid // 1000))) if rowid >= 1000 else []
    return path.joinpath(STORE, "Data", *bucket, "Messages")


@pytest.fixture()
def mail_root(tmp_path: Path) -> Path:
    root = tmp_path / "V10"
    (root / "MailData").mkdir(parents=True)
    (root / ACCOUNT).mkdir()
    (root / ACCOUNT / ".mboxCache.plist").write_bytes(plistlib.dumps({
        "mboxes": {
            "INBOX": {"IMAPMailboxAttributes": 64, "MailboxPathComponent": "INBOX"},
            "Work": {"IMAPMailboxAttributes": 64, "MailboxPathComponent": "Work"},
            "[Gmail]": {
                "IMAPMailboxAttributes": 34,
                "MailboxPathComponent": "[Gmail]",
                "IMAPMailboxChildren": {
                    "All Mail": {"IMAPMailboxAttributes": 1088, "MailboxPathComponent": "All Mail"},
                    "Sent Mail": {"IMAPMailboxAttributes": 32832, "MailboxPathComponent": "Sent Mail"},
                    "Trash": {"IMAPMailboxAttributes": 65600, "MailboxPathComponent": "Trash"},
                    "Spam": {"IMAPMailboxAttributes": 16448, "MailboxPathComponent": "Spam"},
                },
            },
        }
    }))
    connection = sqlite3.connect(root / "MailData" / "Envelope Index")
    connection.executescript(_SCHEMA)
    connection.executemany("INSERT INTO mailboxes VALUES (?, ?, NULL)", [(k, v[0]) for k, v in _MAILBOXES.items()])
    messages = [
        # rowid, mailbox, labels, sender, subject, received, sent, read, flagged, attachments
        (1001, 1, [2], ("alice@example.com", "Alice"), "Invoice May",
         "2026-05-06T11:00:00", "2026-05-06T10:59:00", 0, 1, ["2"]),
        (1002, 1, [3], ("me@example.com", "Me"), "Sent report",
         "2026-05-07T09:00:00", "2026-05-05T10:00:00", 1, 0, []),
        (1003, 4, [], ("spam@example.com", ""), "Old trash",
         "2026-05-01T00:00:00", "2026-05-01T00:00:00", 1, 0, []),
        (5, 1, [2], ("bob@example.com", "Bob"), "Report attached",
         "2026-05-06T16:30:00", "2026-05-06T16:29:00", 1, 0, ["2"]),
        (2001, 1, [2], ("carol@example.com", "Carol"), "Large message",
         "2026-05-04T08:00:00", "2026-05-04T08:00:00", 1, 0, ["2"]),
        (1004, 1, [7], ("dan@example.com", "Dan"), "Archived only",
         "2026-05-08T00:00:00", "2026-05-08T00:00:00", 1, 0, []),
    ]
    for index, (rowid, mailbox, labels, sender, subject, received, sent, read, flagged, attachments) in enumerate(
        messages, start=1
    ):
        connection.execute("INSERT INTO addresses VALUES (?, ?, ?)", (index, *sender))
        connection.execute("INSERT INTO subjects VALUES (?, ?)", (index, subject))
        connection.execute("INSERT INTO message_global_data VALUES (?, ?)", (index, f"<m{rowid}@example.com>"))
        connection.execute(
            "INSERT INTO messages VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, ?, 0, 100)",
            (rowid, index, index, index, _utc(sent), _utc(received), mailbox, read, flagged),
        )
        connection.executemany("INSERT INTO labels VALUES (?, ?)", [(rowid, label) for label in labels])
        connection.executemany(
            "INSERT INTO attachments (message, attachment_id, name) VALUES (?, ?, 'x')",
            [(rowid, attachment_id) for attachment_id in attachments],
        )
    connection.commit()
    connection.close()

    all_mail = ("[Gmail]", "All Mail")
    files = {
        (all_mail, 1001, "1001.emlx"): _FULL,
        (all_mail, 1002, "1002.emlx"): _HTML,
        (all_mail, 5, "5.partial.emlx"): _PARTIAL_WITH_STORED_ATTACHMENT,
        (all_mail, 2001, "2001.partial.emlx"): _PARTIAL_WITHOUT_BODY,
    }
    for (mailbox, rowid, name), raw in files.items():
        folder = _message_dir(root, mailbox, rowid)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(_emlx(raw))
    stored = _message_dir(root, all_mail, 5).parent / "Attachments" / "5" / "2"
    stored.mkdir(parents=True)
    (stored / "report.pdf").write_bytes(b"%PDF-stored")
    return root


@pytest.fixture()
def bridge(tmp_path: Path, mail_root: Path) -> MacOSAppBridge:
    bridge = MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=5,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
    )
    bridge._mail_root = mail_root
    return bridge


def _search(bridge: MacOSAppBridge, **kwargs) -> dict:
    params = dict(
        scope=None, query=None, search_body=False, date_after=None, date_before=None,
        unread=None, flagged=None, has_attachments=None, scan_limit=None, limit=None, offset=None,
    )
    params.update(kwargs)
    return bridge.mail_search(**params)


def test_catalog_maps_gmail_labels_and_special_use_mailboxes(bridge: MacOSAppBridge):
    result = bridge.mail_catalog()

    scopes = {row["scope"]: row for row in result["scopes"]}
    assert result["count"] == 6
    assert (scopes["inbox"]["message_count"], scopes["inbox"]["unread_count"]) == (3, 1)
    assert scopes["inbox"]["name"] == "INBOX"
    assert scopes["sent"]["message_count"] == 1
    assert scopes["sent"]["mailboxes"] == ["[Gmail]/Sent Mail"]
    assert scopes["trash"]["message_count"] == 1
    assert scopes["junk"]["message_count"] == 0
    assert scopes["outbox"]["mailboxes"] == ["Outbox"]
    assert scopes["drafts"] == {
        "scope": "drafts", "name": "drafts", "mailboxes": [], "message_count": 0, "unread_count": 0,
    }


def test_search_inbox_returns_newest_first_with_local_times(bridge: MacOSAppBridge):
    result = _search(bridge)

    assert [row["message_ref"] for row in result["results"]] == ["mailmsg:5", "mailmsg:1001", "mailmsg:2001"]
    first = result["results"][0]
    assert first["scope"] == "inbox"
    assert first["sender"] == "Bob <bob@example.com>"
    assert first["message_id"] == "m5@example.com"
    assert first["date"] == "2026-05-07T00:30:00+08:00"
    assert first["date_kind"] == "received"
    assert first["attachment_count"] == 1
    assert result["scanned_count"] == 3
    assert result["scan_truncated"] is False
    assert result["matched_count_exact"] is True


def test_search_date_only_bounds_cover_the_whole_local_day(bridge: MacOSAppBridge):
    result = _search(bridge, date_after="2026-05-06", date_before="2026-05-06")

    # 11:00Z is 19:00 local on 05-06; 16:30Z is already 00:30 local on 05-07.
    assert [row["message_ref"] for row in result["results"]] == ["mailmsg:1001"]
    assert result["results"][0]["date_received"] == "2026-05-06T19:00:00+08:00"


def test_search_all_uses_sent_date_for_sent_and_skips_archived(bridge: MacOSAppBridge):
    result = _search(bridge, scope="all")

    refs = [row["message_ref"] for row in result["results"]]
    assert "mailmsg:1004" not in refs
    assert result["scanned_scopes"] == ["inbox", "sent", "drafts", "junk", "trash", "outbox"]
    sent = next(row for row in result["results"] if row["message_ref"] == "mailmsg:1002")
    assert (sent["scope"], sent["date_kind"], sent["date"]) == ("sent", "sent", "2026-05-05T18:00:00+08:00")
    assert refs[-1] == "mailmsg:1003"


def test_search_filters_and_scan_limit(bridge: MacOSAppBridge):
    assert [r["id"] for r in _search(bridge, unread=True)["results"]] == [1001]
    assert [r["id"] for r in _search(bridge, flagged=True)["results"]] == [1001]
    assert _search(bridge, has_attachments=False)["count"] == 0
    assert [r["id"] for r in _search(bridge, query="BOB")["results"]] == [5]

    truncated = _search(bridge, scan_limit=1)
    assert truncated["scanned_count"] == 1
    assert truncated["scan_truncated"] is True
    assert "scan_limit 1 reached" in truncated["warning"]


def test_search_body_reads_emlx_and_counts_undownloaded_bodies(bridge: MacOSAppBridge):
    assert _search(bridge, query="keyword")["count"] == 0

    result = _search(bridge, query="keyword", search_body=True)
    assert [r["id"] for r in result["results"]] == [1001]
    assert result["body_unavailable_count"] == 1
    assert result["matched_count_exact"] is False
    assert "body of 1 message(s) not downloaded locally" in result["warning"]

    html = _search(bridge, scope="sent", query="quarterly", search_body=True)
    assert [r["id"] for r in html["results"]] == [1002]
    assert html["body_unavailable_count"] == 0
    assert html["matched_count_exact"] is True
    assert html["warning"] is None


def test_get_returns_body_reply_to_and_attachments(bridge: MacOSAppBridge):
    message = bridge.mail_get(message_ref="mailmsg:1001", scope=None)["message"]

    assert message["content"] == "hello body keyword"
    assert message["content_available"] is True
    assert message["reply_to"] == "billing@example.com"
    assert message["scopes"] == ["inbox"]
    assert message["junk"] is False
    assert message["attachments"] == [
        {"id": "2", "name": "note.txt", "mime_type": "text/plain", "file_size": 13, "downloaded": True}
    ]

    html = bridge.mail_get(message_ref="mailmsg:1002", scope="sent")["message"]
    assert html["content"] == "Quarterly\n\nnumbers"


def test_get_reports_missing_body_and_scope_mismatch(bridge: MacOSAppBridge):
    message = bridge.mail_get(message_ref="mailmsg:2001", scope=None)["message"]
    assert message["content"] is None
    assert message["content_available"] is False
    assert "not downloaded" in message["content_error"]
    assert message["attachments"][0]["downloaded"] is False
    assert message["attachments"][0]["file_size"] == 9000

    assert bridge.mail_get(message_ref="mailmsg:1001", scope="sent") == {
        "ok": False, "error": "message not found: mailmsg:1001",
    }
    assert bridge.mail_get(message_ref="msg:1001", scope=None)["ok"] is False


def test_export_decodes_inline_and_copies_stored_attachments(bridge: MacOSAppBridge, tmp_path: Path):
    destination = tmp_path / "out"

    full = bridge.mail_export_attachment(message_ref="mailmsg:1001", attachment_ids=None, destination_dir=str(destination))
    partial = bridge.mail_export_attachment(message_ref="mailmsg:5", attachment_ids=["2"], destination_dir=str(destination))
    again = bridge.mail_export_attachment(message_ref="mailmsg:5", attachment_ids=None, destination_dir=str(destination))

    assert (destination / "note.txt").read_bytes() == b"attached text"
    assert full["exported_count"] == 1
    assert partial["files"] == [str(destination / "report.pdf")]
    assert partial["attachments"][0]["file_size"] == len(b"%PDF-stored")
    assert (destination / "report.pdf").read_bytes() == b"%PDF-stored"
    assert again["files"] == [str(destination / "report (2).pdf")]


def test_export_errors_on_unknown_or_undownloaded_attachments(bridge: MacOSAppBridge, tmp_path: Path):
    with pytest.raises(RuntimeError, match="unknown attachment ids: 9"):
        bridge.mail_export_attachment(message_ref="mailmsg:1001", attachment_ids=["9"], destination_dir=str(tmp_path))
    with pytest.raises(RuntimeError, match="not downloaded locally: 2"):
        bridge.mail_export_attachment(message_ref="mailmsg:2001", attachment_ids=None, destination_dir=str(tmp_path))


_FORWARDED = """From: Dan <dan@example.com>
Subject: Fwd: Inner
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="b4"

--b4
Content-Type: text/plain; charset=utf-8

see forwarded
--b4
Content-Type: message/rfc822
Content-Disposition: attachment; filename="Inner.eml"

From: Eve <eve@example.com>
Subject: Inner
Content-Type: text/plain; charset=utf-8

inner body
--b4
Content-Type: message/rfc822

From: Postmaster <postmaster@example.com>
Subject: Delivery report
Content-Type: text/plain; charset=utf-8

report text
--b4--
"""

_EMPTY_PLAIN_ALTERNATIVE = """From: Fay <fay@example.com>
Subject: Newsletter
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="b5"

--b5
Content-Type: text/plain; charset=utf-8

\x20\x20
--b5
Content-Type: text/html; charset=utf-8

<html><body><p>Real body</p></body></html>
--b5--
"""


def test_attached_message_is_one_exportable_eml(tmp_path: Path):
    path = tmp_path / "Messages" / "1.emlx"
    path.parent.mkdir()
    path.write_bytes(_emlx(_FORWARDED))
    store = MailStore(tmp_path)
    store.message_file = lambda url, rowid: path  # type: ignore[method-assign]

    attachments = store.attachments(store.read_emlx(path), path, 1)
    # The inline report (part 3) is still opened up and is not an attachment.
    assert [(item["id"], item["name"], item["mime_type"]) for item in attachments] == [
        ("2", "Inner.eml", "message/rfc822")
    ]
    assert attachments[0]["file_size"] > 0

    exported, files = store.export_attachments(
        {"_url": "", "id": 1, "message_ref": "mailmsg:1"}, ["2"], tmp_path / "out"
    )
    assert [Path(f).name for f in files] == ["Inner.eml"]
    inner = MailStore.read_emlx(path).get_payload(1).get_payload(0)
    assert Path(files[0]).read_bytes() == inner.as_bytes()
    assert "Subject: Inner" in Path(files[0]).read_text()


def test_body_text_falls_back_to_html_when_plain_alternative_is_blank(tmp_path: Path):
    path = tmp_path / "1.emlx"
    path.write_bytes(_emlx(_EMPTY_PLAIN_ALTERNATIVE))

    assert MailStore.body_text(MailStore.read_emlx(path)) == "Real body"


def test_trash_addresses_message_directly_and_verifies_message_id(bridge: MacOSAppBridge):
    captured = {}

    def fake_run_jxa_json(body: str, *, payload=None, **kwargs):
        captured["body"] = body
        captured["payload"] = payload
        return {"ok": True, "verified_count": 1}

    bridge._run_jxa_json = fake_run_jxa_json  # type: ignore[method-assign]
    result = bridge.mail_trash(message_refs=["mailmsg:1001"], dry_run=True)

    assert captured["payload"] == {
        "dry_run": True,
        "targets": [{
            "message_ref": "mailmsg:1001",
            "id": 1001,
            "local": False,
            "account": ACCOUNT,
            "mailbox_path": "[Gmail]/All Mail",
            "trash_path": "[Gmail]/Trash",
            "message_id": "m1001@example.com",
        }],
    }
    assert "messageId()" in captured["body"]
    assert "app.move(" in captured["body"]
    assert result["dry_run"] is True
    assert result["messages"][0]["message_ref"] == "mailmsg:1001"


def test_trash_reports_unresolved_mailbox_from_mail(bridge: MacOSAppBridge):
    captured = {}
    problem = {"ok": False, "error": "mailmsg:1001: Mail.app mailbox not found: [Gmail]/Trash"}

    def fake_run_jxa_json(body: str, *, payload=None, **kwargs):
        captured["body"] = body
        return problem

    bridge._run_jxa_json = fake_run_jxa_json  # type: ignore[method-assign]

    assert bridge.mail_trash(message_refs=["mailmsg:1001"], dry_run=False) == problem
    # Full path first, then a walk through the parents; both mailboxes are
    # resolved before anything moves.
    assert "safe(() => direct.exists(), false)" in captured["body"]
    assert 'path.split("/")' in captured["body"]
    assert "mailboxOf(target, target.trash_path)" in captured["body"]


def test_trash_rejects_missing_and_already_trashed_before_mail_runs(bridge: MacOSAppBridge):
    def fail(*args, **kwargs):
        raise AssertionError("JXA must not run")

    bridge._run_jxa_json = fail  # type: ignore[method-assign]

    assert bridge.mail_trash(message_refs=["mailmsg:1001", "mailmsg:999"], dry_run=False) == {
        "ok": False, "error": "messages not found: mailmsg:999",
    }
    assert bridge.mail_trash(message_refs=["mailmsg:1003"], dry_run=True) == {
        "ok": False, "error": "mailmsg:1003: already in Trash",
    }


def test_tool_reports_unreadable_index(tmp_path: Path):
    bridge = MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=5,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
    )
    bridge._mail_root = tmp_path / "missing"
    tool = create_mail_tool(bridge)

    with pytest.raises(RuntimeError, match="Full Disk Access"):
        tool(action="catalog")
    problems = mail_permission_problems(tmp_path / "missing")
    assert len(problems) == 1 and "Full Disk Access" in problems[0]


def test_permission_check_passes_for_readable_index(mail_root: Path):
    assert mail_permission_problems(mail_root) == []


def test_tool_output_is_json(bridge: MacOSAppBridge):
    payload = json.loads(create_mail_tool(bridge)(action="search", scope="inbox", limit=1))
    assert payload["count"] == 1
    assert payload["result_truncated"] is True


def test_parse_message_ref_accepts_bare_numeric_id(bridge: MacOSAppBridge):
    assert bridge.mail_get(message_ref="1001", scope=None)["message"]["message_ref"] == "mailmsg:1001"
    assert parse_message_ref("mailmsg:1001") == 1001
    assert parse_message_ref(" 1001 ") == 1001
    assert parse_message_ref("mailmsg:abc") is None
    assert parse_message_ref("msg:1001") is None
