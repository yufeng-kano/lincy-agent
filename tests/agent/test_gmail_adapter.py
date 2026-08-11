"""Tests for agent.adapters.gmail helpers and adapter logic."""

import base64
from datetime import datetime, timezone

import pytest

from lincy.agent.adapters.gmail import (
    _collect_attachments,
    _extract_email,
    _extract_text_body,
    _is_automated_email,
    _matches_ignore_list,
    _parse_email_date,
    _strip_quoted_content,
    GmailAdapter,
)
from lincy.agent.contact_map import ContactMap
from lincy.agent.schema import InboundMessage, OutboundMessage
from lincy.agent.thread_registry import ThreadRegistry


# ------------------------------------------------------------------
# _extract_email
# ------------------------------------------------------------------

class TestExtractEmail:
    def test_bare_email(self):
        assert _extract_email("alice@example.com") == "alice@example.com"

    def test_name_angle_bracket(self):
        assert _extract_email("Alice <alice@example.com>") == "alice@example.com"

    def test_quoted_name(self):
        assert _extract_email('"Alice B" <alice@example.com>') == "alice@example.com"

    def test_lowercases(self):
        assert _extract_email("Alice@EXAMPLE.COM") == "alice@example.com"

    def test_empty(self):
        assert _extract_email("") == ""


# ------------------------------------------------------------------
# _extract_text_body
# ------------------------------------------------------------------

def _encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


class TestExtractTextBody:
    def test_single_part_text_plain(self):
        msg = {
            "payload": {
                "mimeType": "text/plain",
                "body": {"data": _encode("Hello world")},
            }
        }
        assert _extract_text_body(msg) == "Hello world"

    def test_multipart_finds_text_plain(self):
        msg = {
            "payload": {
                "mimeType": "multipart/alternative",
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"data": _encode("Plain text")},
                    },
                    {
                        "mimeType": "text/html",
                        "body": {"data": _encode("<b>HTML</b>")},
                    },
                ],
            }
        }
        assert _extract_text_body(msg) == "Plain text"

    def test_html_only_stripped_to_text(self):
        msg = {
            "payload": {
                "mimeType": "text/html",
                "body": {"data": _encode("<p>Hello <b>world</b></p>")},
            }
        }
        result = _extract_text_body(msg)
        assert "Hello" in result
        assert "world" in result
        assert "<" not in result

    def test_multipart_prefers_text_plain(self):
        msg = {
            "payload": {
                "mimeType": "multipart/alternative",
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"data": _encode("Plain version")},
                    },
                    {
                        "mimeType": "text/html",
                        "body": {"data": _encode("<b>HTML version</b>")},
                    },
                ],
            }
        }
        assert _extract_text_body(msg) == "Plain version"

    def test_nested_multipart(self):
        """Gmail replies often nest text/plain inside multipart/alternative."""
        msg = {
            "payload": {
                "mimeType": "multipart/mixed",
                "parts": [
                    {
                        "mimeType": "multipart/alternative",
                        "parts": [
                            {
                                "mimeType": "text/plain",
                                "body": {"data": _encode("Nested plain")},
                            },
                            {
                                "mimeType": "text/html",
                                "body": {"data": _encode("<b>Nested HTML</b>")},
                            },
                        ],
                    },
                ],
            }
        }
        assert _extract_text_body(msg) == "Nested plain"

    def test_multipart_html_fallback(self):
        msg = {
            "payload": {
                "mimeType": "multipart/alternative",
                "parts": [
                    {
                        "mimeType": "text/html",
                        "body": {"data": _encode("<p>Only HTML</p>")},
                    },
                ],
            }
        }
        result = _extract_text_body(msg)
        assert "Only HTML" in result
        assert "<" not in result

    def test_empty_payload(self):
        assert _extract_text_body({}) == ""
        assert _extract_text_body({"payload": {}}) == ""


# ------------------------------------------------------------------
# _strip_quoted_content
# ------------------------------------------------------------------

class TestStripQuotedContent:
    def test_strips_quoted_lines(self):
        text = "My reply\n> Previous message\n> More quoted"
        assert _strip_quoted_content(text) == "My reply"

    def test_strips_on_wrote(self):
        text = "My reply\n\nOn Mon, Jan 1, 2026, Alice wrote:"
        assert "On" not in _strip_quoted_content(text)

    def test_strips_signature(self):
        text = "My reply\n\n--\nAlice\nalice@example.com"
        result = _strip_quoted_content(text)
        assert "Alice" not in result
        assert result == "My reply"

    def test_preserves_clean_text(self):
        text = "Just a normal message."
        assert _strip_quoted_content(text) == text

    def test_empty_after_stripping(self):
        text = "> Only quoted"
        assert _strip_quoted_content(text) == ""


# ------------------------------------------------------------------
# _parse_email_date
# ------------------------------------------------------------------

class TestParseEmailDate:
    def test_standard_rfc2822(self):
        dt = _parse_email_date("Wed, 19 Feb 2026 09:30:00 +0800")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 2
        assert dt.day == 19

    def test_invalid_returns_none(self):
        assert _parse_email_date("not a date") is None
        assert _parse_email_date("") is None


# ------------------------------------------------------------------
# _is_automated_email
# ------------------------------------------------------------------

class TestIsAutomatedEmail:
    def test_list_unsubscribe(self):
        assert _is_automated_email({"list-unsubscribe": "<mailto:unsub@x.com>"})

    def test_precedence_bulk(self):
        assert _is_automated_email({"precedence": "bulk"})

    def test_precedence_list(self):
        assert _is_automated_email({"precedence": "list"})

    def test_auto_submitted(self):
        assert _is_automated_email({"auto-submitted": "auto-generated"})

    def test_auto_submitted_no_is_not_automated(self):
        assert not _is_automated_email({"auto-submitted": "no"})

    def test_normal_email(self):
        assert not _is_automated_email({"from": "alice@example.com"})


# ------------------------------------------------------------------
# _matches_ignore_list
# ------------------------------------------------------------------

class TestMatchesIgnoreList:
    def test_match_domain(self):
        assert _matches_ignore_list("noti@facebookmail.com", ["@facebookmail.com"])

    def test_match_prefix(self):
        assert _matches_ignore_list("noreply@example.com", ["noreply@"])

    def test_no_match(self):
        assert not _matches_ignore_list("alice@example.com", ["@facebookmail.com"])

    def test_case_insensitive(self):
        assert _matches_ignore_list("NoReply@Example.COM", ["noreply@"])

    def test_empty_list(self):
        assert not _matches_ignore_list("anyone@example.com", [])


# ------------------------------------------------------------------
# _collect_attachments
# ------------------------------------------------------------------

class TestCollectAttachments:
    def test_finds_attachment_part(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _encode("Hello")},
                },
                {
                    "mimeType": "image/jpeg",
                    "filename": "photo.jpg",
                    "headers": [],
                    "body": {"attachmentId": "att123", "size": 5000},
                },
            ],
        }
        result: list = []
        _collect_attachments(payload, result)
        assert len(result) == 1
        assert result[0]["filename"] == "photo.jpg"
        assert result[0]["attachment_id"] == "att123"
        assert result[0]["mime_type"] == "image/jpeg"
        assert result[0]["size"] == 5000

    def test_nested_attachment(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": _encode("Hi")}},
                    ],
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "report.pdf",
                    "headers": [
                        {"name": "Content-Disposition", "value": 'attachment; filename="report.pdf"'},
                    ],
                    "body": {"attachmentId": "att456", "size": 10000},
                },
            ],
        }
        result: list = []
        _collect_attachments(payload, result)
        assert len(result) == 1
        assert result[0]["filename"] == "report.pdf"

    def test_no_attachments(self):
        payload = {
            "mimeType": "text/plain",
            "body": {"data": _encode("Just text")},
        }
        result: list = []
        _collect_attachments(payload, result)
        assert len(result) == 0

    def test_multiple_attachments(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "image/png",
                    "filename": "a.png",
                    "headers": [],
                    "body": {"attachmentId": "att1", "size": 100},
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "b.pdf",
                    "headers": [],
                    "body": {"attachmentId": "att2", "size": 200},
                },
            ],
        }
        result: list = []
        _collect_attachments(payload, result)
        assert len(result) == 2


# ------------------------------------------------------------------
# GmailAdapter._process_message (via mock)
# ------------------------------------------------------------------

class _FakeGmailClient:
    """Stub for _GmailClient."""

    def __init__(self, messages=None, attachments=None):
        self._messages = messages or {}
        self._attachments: dict[str, bytes] = attachments or {}
        self.archived: list[str] = []

    def list_unread(self, query_extra=""):
        return [{"id": mid} for mid in self._messages]

    def get_message(self, msg_id):
        return self._messages[msg_id]

    def get_attachment(self, msg_id, attachment_id):
        return self._attachments.get(attachment_id, b"")

    def archive(self, msg_id):
        self.archived.append(msg_id)

    def send(self, **kwargs):
        return {"id": "sent_1", "threadId": kwargs.get("thread_id") or "new_thread"}

    def close(self):
        pass


def _make_gmail_message(
    msg_id: str,
    from_addr: str,
    subject: str,
    body: str,
    thread_id: str = "t1",
    extra_headers: dict[str, str] | None = None,
) -> dict:
    headers = [
        {"name": "From", "value": from_addr},
        {"name": "Subject", "value": subject},
    ]
    for k, v in (extra_headers or {}).items():
        headers.append({"name": k, "value": v})
    return {
        "id": msg_id,
        "threadId": thread_id,
        "payload": {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": _encode(body)},
        },
    }


class _FakeAgent:
    """Stub for AgentCore.enqueue()."""

    def __init__(self):
        self.enqueued: list[InboundMessage] = []

    def enqueue(self, msg):
        self.enqueued.append(msg)


@pytest.fixture()
def contact_map(tmp_path):
    return ContactMap(tmp_path / "cache")


@pytest.fixture()
def thread_registry(tmp_path):
    return ThreadRegistry(tmp_path / "cache")


def _make_adapter(contact_map, gmail_client, thread_registry=None, **kwargs):
    """Helper to create a GmailAdapter with fakes injected."""
    if thread_registry is None:
        from pathlib import Path
        import tempfile
        thread_registry = ThreadRegistry(Path(tempfile.mkdtemp()) / "cache")
    adapter = GmailAdapter(
        client_id="cid",
        client_secret="csec",
        refresh_token="rtok",
        contact_map=contact_map,
        thread_registry=thread_registry,
        **kwargs,
    )
    adapter._gmail = gmail_client
    adapter._agent = _FakeAgent()
    return adapter


class TestProcessMessage:

    def test_basic_message_enqueued(self, contact_map):
        fake = _FakeGmailClient({
            "m1": _make_gmail_message("m1", "alice@example.com", "Hi", "Hello!"),
        })
        adapter = _make_adapter(contact_map, fake)
        adapter._process_message("m1")

        assert len(adapter._agent.enqueued) == 1
        msg = adapter._agent.enqueued[0]
        assert msg.channel == "gmail"
        assert msg.sender == "alice@example.com"  # unknown, uses raw email
        assert "Hello!" in msg.content
        assert msg.metadata["reply_to"] == "alice@example.com"
        assert msg.metadata["thread_id"] == "t1"

    def test_subject_included_for_new_thread(self, contact_map):
        fake = _FakeGmailClient({
            "m1": _make_gmail_message("m1", "a@b.com", "Important topic", "Body"),
        })
        adapter = _make_adapter(contact_map, fake)
        adapter._process_message("m1")

        msg = adapter._agent.enqueued[0]
        assert "[Subject: Important topic]" in msg.content
        assert msg.metadata["subject"] == "Re: Important topic"

    def test_reply_includes_clean_subject(self, contact_map):
        fake = _FakeGmailClient({
            "m1": _make_gmail_message("m1", "a@b.com", "Re: Something", "Reply body"),
        })
        adapter = _make_adapter(contact_map, fake)
        adapter._process_message("m1")

        msg = adapter._agent.enqueued[0]
        assert "[Subject: Something]" in msg.content
        assert "Reply body" in msg.content
        assert "Re:" not in msg.content

    def test_contact_map_resolves_sender(self, contact_map):
        contact_map.update("gmail", "alice@example.com", "alice")
        fake = _FakeGmailClient({
            "m1": _make_gmail_message("m1", "alice@example.com", "Hi", "Hey"),
        })
        adapter = _make_adapter(contact_map, fake)
        adapter._process_message("m1")

        assert adapter._agent.enqueued[0].sender == "alice"

    def test_subject_only_uses_subject_as_content(self, contact_map):
        fake = _FakeGmailClient({
            "m1": _make_gmail_message("m1", "a@b.com", "Buy milk tomorrow", ""),
        })
        adapter = _make_adapter(contact_map, fake)
        adapter._process_message("m1")

        assert len(adapter._agent.enqueued) == 1
        msg = adapter._agent.enqueued[0]
        assert msg.content == "Buy milk tomorrow"

    def test_empty_subject_and_body_skipped(self, contact_map):
        fake = _FakeGmailClient({
            "m1": _make_gmail_message("m1", "a@b.com", "", ""),
        })
        adapter = _make_adapter(contact_map, fake)
        adapter._process_message("m1")

        assert len(adapter._agent.enqueued) == 0
        assert "m1" in fake.archived

    def test_archived_after_enqueue(self, contact_map):
        fake = _FakeGmailClient({
            "m1": _make_gmail_message("m1", "a@b.com", "Hi", "Body"),
        })
        adapter = _make_adapter(contact_map, fake)
        adapter._process_message("m1")

        assert "m1" in fake.archived

    def test_duplicate_skipped_via_check_inbox(self, contact_map):
        fake = _FakeGmailClient({
            "m1": _make_gmail_message("m1", "a@b.com", "Hi", "Body"),
        })
        adapter = _make_adapter(contact_map, fake)
        adapter._check_inbox()
        adapter._check_inbox()  # second poll returns same message

        assert len(adapter._agent.enqueued) == 1  # only processed once

    def test_automated_email_filtered_by_header(self, contact_map):
        fake = _FakeGmailClient({
            "m1": _make_gmail_message(
                "m1", "noti@facebookmail.com", "Story update", "Check it out",
                extra_headers={"List-Unsubscribe": "<mailto:unsub@fb.com>"},
            ),
        })
        adapter = _make_adapter(contact_map, fake)
        adapter._process_message("m1")

        assert len(adapter._agent.enqueued) == 0
        assert "m1" in fake.archived  # auto-archived

    def test_ignore_senders_filtered(self, contact_map):
        fake = _FakeGmailClient({
            "m1": _make_gmail_message("m1", "noti@facebookmail.com", "Hi", "Body"),
        })
        adapter = _make_adapter(
            contact_map, fake, ignore_senders=["@facebookmail.com"],
        )
        adapter._process_message("m1")

        assert len(adapter._agent.enqueued) == 0
        assert "m1" in fake.archived

    def test_email_date_used_as_timestamp(self, contact_map):
        fake = _FakeGmailClient({
            "m1": _make_gmail_message(
                "m1", "a@b.com", "Hi", "Body",
                extra_headers={"Date": "Wed, 15 Jan 2026 10:30:00 +0800"},
            ),
        })
        adapter = _make_adapter(contact_map, fake)
        adapter._process_message("m1")

        msg = adapter._agent.enqueued[0]
        assert msg.timestamp.year == 2026
        assert msg.timestamp.month == 1
        assert msg.timestamp.day == 15

    def test_attachment_downloaded_and_in_content(self, contact_map):
        msg_data = {
            "id": "m1",
            "threadId": "t1",
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [
                    {"name": "From", "value": "a@b.com"},
                    {"name": "Subject", "value": "See this"},
                ],
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"data": _encode("Check the photo")},
                    },
                    {
                        "mimeType": "image/jpeg",
                        "filename": "photo.jpg",
                        "headers": [],
                        "body": {"attachmentId": "att1", "size": 100},
                    },
                ],
            },
        }
        fake = _FakeGmailClient(
            messages={"m1": msg_data},
            attachments={"att1": b"\xff\xd8\xff\xe0fake-jpeg"},
        )
        adapter = _make_adapter(contact_map, fake)
        adapter._process_message("m1")

        msg = adapter._agent.enqueued[0]
        assert msg.content.count("[Attachments]") == 1
        assert "\n\n[Attachments]\n" in msg.content
        assert "photo.jpg" in msg.content
        assert "image/jpeg" in msg.content
        # Verify file was written
        assert adapter._tmp_dir.exists()

    def test_no_attachment_content_unchanged(self, contact_map):
        fake = _FakeGmailClient({
            "m1": _make_gmail_message("m1", "a@b.com", "Hi", "Just text"),
        })
        adapter = _make_adapter(contact_map, fake)
        adapter._process_message("m1")

        msg = adapter._agent.enqueued[0]
        assert "[Attachments]" not in msg.content

    def test_large_attachment_noted_not_downloaded(self, contact_map):
        msg_data = {
            "id": "m1",
            "threadId": "t1",
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [
                    {"name": "From", "value": "a@b.com"},
                    {"name": "Subject", "value": "Big file"},
                ],
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"data": _encode("Here")},
                    },
                    {
                        "mimeType": "application/zip",
                        "filename": "huge.zip",
                        "headers": [],
                        "body": {"attachmentId": "att1", "size": 30_000_000},
                    },
                ],
            },
        }
        fake = _FakeGmailClient(messages={"m1": msg_data})
        adapter = _make_adapter(contact_map, fake)
        adapter._process_message("m1")

        msg = adapter._agent.enqueued[0]
        assert "[Attachments]" in msg.content
        assert "too large" in msg.content
        assert "huge.zip" in msg.content


class TestSendStripsMarkdown:
    def test_send_converts_markdown_to_plaintext(self, contact_map, thread_registry):
        fake = _FakeGmailClient()
        sent_bodies: list[str] = []
        original_send = fake.send
        def capture_send(**kwargs):
            sent_bodies.append(kwargs.get("body", ""))
            return original_send(**kwargs)
        fake.send = capture_send

        adapter = GmailAdapter(
            client_id="cid", client_secret="csec", refresh_token="rtok",
            contact_map=contact_map,
            thread_registry=thread_registry,
        )
        adapter._gmail = fake

        adapter.send(OutboundMessage(
            channel="gmail",
            content="## Hello **world**",
            metadata={"reply_to": "a@b.com", "subject": "Re: Hi"},
        ))

        assert len(sent_bodies) == 1
        assert "**" not in sent_bodies[0]
        assert "##" not in sent_bodies[0]
        assert "Hello world" in sent_bodies[0]


# ------------------------------------------------------------------
# Thread registry integration
# ------------------------------------------------------------------

class TestThreadRegistryIntegration:
    def test_inbound_updates_registry(self, contact_map, thread_registry):
        fake = _FakeGmailClient({
            "m1": _make_gmail_message(
                "m1", "alice@example.com", "Hi", "Hello!",
                extra_headers={"Message-ID": "<msg1@mail.gmail.com>"},
            ),
        })
        adapter = _make_adapter(contact_map, fake, thread_registry)
        adapter._process_message("m1")

        entry = thread_registry.get("gmail", "alice@example.com")
        assert entry is not None
        assert entry["thread_id"] == "t1"
        assert entry["message_id"] == "<msg1@mail.gmail.com>"
        assert entry["subject"] == "Re: Hi"
        assert "last_activity" in entry

    def test_send_uses_registry_when_no_thread_context(
        self, contact_map, thread_registry,
    ):
        thread_registry.update("gmail", "alice@example.com", {
            "thread_id": "cached_thread",
            "message_id": "<cached@mail.gmail.com>",
            "subject": "Re: Dinner",
            "last_activity": datetime.now(timezone.utc).isoformat(),
        })
        fake = _FakeGmailClient()
        sent_kwargs: list[dict] = []
        original_send = fake.send
        def capture_send(**kwargs):
            sent_kwargs.append(kwargs)
            return original_send(**kwargs)
        fake.send = capture_send

        adapter = GmailAdapter(
            client_id="cid", client_secret="csec", refresh_token="rtok",
            contact_map=contact_map,
            thread_registry=thread_registry,
        )
        adapter._gmail = fake

        adapter.send(OutboundMessage(
            channel="gmail",
            content="Sounds good!",
            metadata={"reply_to": "alice@example.com"},
        ))

        assert len(sent_kwargs) >= 1
        # The actual Gmail send should use the cached thread
        assert sent_kwargs[0]["thread_id"] == "cached_thread"
        assert sent_kwargs[0]["in_reply_to"] == "<cached@mail.gmail.com>"
        assert sent_kwargs[0]["subject"] == "Re: Dinner"

    def test_send_ignores_registry_when_thread_id_present(
        self, contact_map, thread_registry,
    ):
        thread_registry.update("gmail", "alice@example.com", {
            "thread_id": "cached_thread",
            "message_id": "<cached@mail.gmail.com>",
            "subject": "Re: Old",
            "last_activity": datetime.now(timezone.utc).isoformat(),
        })
        fake = _FakeGmailClient()
        sent_kwargs: list[dict] = []
        original_send = fake.send
        def capture_send(**kwargs):
            sent_kwargs.append(kwargs)
            return original_send(**kwargs)
        fake.send = capture_send

        adapter = GmailAdapter(
            client_id="cid", client_secret="csec", refresh_token="rtok",
            contact_map=contact_map,
            thread_registry=thread_registry,
        )
        adapter._gmail = fake

        adapter.send(OutboundMessage(
            channel="gmail",
            content="Reply",
            metadata={
                "reply_to": "alice@example.com",
                "thread_id": "explicit_thread",
                "message_id": "<explicit@mail.gmail.com>",
                "subject": "Re: Current",
            },
        ))

        assert sent_kwargs[0]["thread_id"] == "explicit_thread"

    def test_stale_registry_entry_ignored(self, contact_map, thread_registry):
        # Entry from 30 days ago
        thread_registry.update("gmail", "alice@example.com", {
            "thread_id": "old_thread",
            "message_id": "<old@mail.gmail.com>",
            "subject": "Re: Ancient",
            "last_activity": "2026-01-01T00:00:00+00:00",
        })
        fake = _FakeGmailClient()
        sent_kwargs: list[dict] = []
        original_send = fake.send
        def capture_send(**kwargs):
            sent_kwargs.append(kwargs)
            return original_send(**kwargs)
        fake.send = capture_send

        adapter = GmailAdapter(
            client_id="cid", client_secret="csec", refresh_token="rtok",
            contact_map=contact_map,
            thread_registry=thread_registry,
            thread_max_age_days=7,
        )
        adapter._gmail = fake

        adapter.send(OutboundMessage(
            channel="gmail",
            content="Hello",
            metadata={"reply_to": "alice@example.com"},
        ))

        # Stale entry should not provide thread_id
        assert sent_kwargs[0]["thread_id"] is None

    def test_explicit_subject_skips_registry(self, contact_map, thread_registry):
        thread_registry.update("gmail", "alice@example.com", {
            "thread_id": "cached_thread",
            "message_id": "<cached@mail.gmail.com>",
            "subject": "Re: Old Topic",
            "last_activity": datetime.now(timezone.utc).isoformat(),
        })
        fake = _FakeGmailClient()
        sent_kwargs: list[dict] = []
        original_send = fake.send
        def capture_send(**kwargs):
            sent_kwargs.append(kwargs)
            return original_send(**kwargs)
        fake.send = capture_send

        adapter = GmailAdapter(
            client_id="cid", client_secret="csec", refresh_token="rtok",
            contact_map=contact_map,
            thread_registry=thread_registry,
        )
        adapter._gmail = fake

        adapter.send(OutboundMessage(
            channel="gmail",
            content="New topic!",
            metadata={
                "reply_to": "alice@example.com",
                "subject": "New Topic",
            },
        ))

        # Should NOT use registry; new thread
        assert sent_kwargs[0]["thread_id"] is None
        assert sent_kwargs[0]["subject"] == "New Topic"

    def test_post_send_updates_registry(self, contact_map, thread_registry):
        fake = _FakeGmailClient()
        adapter = GmailAdapter(
            client_id="cid", client_secret="csec", refresh_token="rtok",
            contact_map=contact_map,
            thread_registry=thread_registry,
        )
        adapter._gmail = fake

        adapter.send(OutboundMessage(
            channel="gmail",
            content="First email",
            metadata={
                "reply_to": "bob@example.com",
                "subject": "Hello",
            },
        ))

        entry = thread_registry.get("gmail", "bob@example.com")
        assert entry is not None
        assert entry["thread_id"] == "new_thread"
        assert entry["subject"] == "Hello"

    def test_thread_split_detected_on_send(self, contact_map, thread_registry):
        """When Gmail returns a different threadId, superseded is recorded."""
        thread_registry.update("gmail", "alice@example.com", {
            "thread_id": "thread_old",
            "message_id": "<old@mail.gmail.com>",
            "subject": "Re: Chat",
            "last_activity": datetime.now(timezone.utc).isoformat(),
        })
        fake = _FakeGmailClient()
        # Override send to return a DIFFERENT threadId (simulating split)
        fake.send = lambda **kw: {"id": "sent_1", "threadId": "thread_new"}

        adapter = _make_adapter(contact_map, fake, thread_registry)
        adapter.send(OutboundMessage(
            channel="gmail",
            content="Hi",
            metadata={
                "reply_to": "alice@example.com",
                "thread_id": "thread_old",
                "message_id": "<old@mail.gmail.com>",
                "subject": "Re: Chat",
            },
        ))

        entry = thread_registry.get("gmail", "alice@example.com")
        assert entry is not None
        assert entry["thread_id"] == "thread_new"
        assert entry["superseded"] == {"thread_old": "thread_new"}

    def test_inbound_from_superseded_thread_not_reverted(
        self, contact_map, thread_registry,
    ):
        """Inbound from old thread doesn't revert registry to old threadId."""
        thread_registry.update("gmail", "alice@example.com", {
            "thread_id": "thread_new",
            "message_id": "<new@mail.gmail.com>",
            "subject": "Re: Chat",
            "last_activity": datetime.now(timezone.utc).isoformat(),
            "superseded": {"thread_old": "thread_new"},
        })
        # Inbound arrives referencing the OLD thread
        fake = _FakeGmailClient({
            "m1": _make_gmail_message(
                "m1", "alice@example.com", "Re: Chat", "Hello!",
                thread_id="thread_old",
                extra_headers={"Message-ID": "<reply@mail.gmail.com>"},
            ),
        })
        adapter = _make_adapter(contact_map, fake, thread_registry)
        adapter._process_message("m1")

        # Registry should still point to thread_new
        entry = thread_registry.get("gmail", "alice@example.com")
        assert entry["thread_id"] == "thread_new"
        assert entry["superseded"] == {"thread_old": "thread_new"}
        # message_id should be updated to the new inbound
        assert entry["message_id"] == "<reply@mail.gmail.com>"

        # Metadata on enqueued message should also use thread_new
        enqueued = adapter._agent.enqueued
        assert len(enqueued) == 1
        assert enqueued[0].metadata["thread_id"] == "thread_new"

    def test_superseded_chain(self, contact_map, thread_registry):
        """A->B->C chain: inbound from A resolves to C."""
        thread_registry.update("gmail", "alice@example.com", {
            "thread_id": "thread_C",
            "message_id": "<c@mail.gmail.com>",
            "subject": "Re: Chat",
            "last_activity": datetime.now(timezone.utc).isoformat(),
            "superseded": {"thread_A": "thread_B", "thread_B": "thread_C"},
        })
        # Inbound from the oldest thread
        fake = _FakeGmailClient({
            "m1": _make_gmail_message(
                "m1", "alice@example.com", "Re: Chat", "Hey!",
                thread_id="thread_A",
                extra_headers={"Message-ID": "<a@mail.gmail.com>"},
            ),
        })
        adapter = _make_adapter(contact_map, fake, thread_registry)
        adapter._process_message("m1")

        entry = thread_registry.get("gmail", "alice@example.com")
        # thread_A -> thread_B -> thread_C via chain resolution
        assert entry["thread_id"] == "thread_C"
        # The superseded map is preserved
        assert entry["superseded"] == {
            "thread_A": "thread_B",
            "thread_B": "thread_C",
        }
