"""Tests for Discord adapter behavior without a real gateway."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lincy.agent.adapters.discord import DiscordAdapter
from lincy.agent.contact_map import ContactMap
from lincy.agent.discord_history import DiscordHistoryStore
from lincy.agent.schema import OutboundMessage
from lincy.core.schema import DiscordChannelConfig


class _FakeAgent:
    def __init__(self):
        self.enqueued = []
        self.turn_context = None

    def enqueue(self, msg):
        self.enqueued.append(msg)


@dataclass
class _FakeGuild:
    id: int
    name: str


@dataclass
class _FakeUser:
    id: int
    name: str
    display_name: str | None = None
    global_name: str | None = None

    async def create_dm(self):
        return _FakeSendChannel(self.id, "dm")


class _FakeAttachment:
    def __init__(self, filename="img.png", content_type="image/png", data=b"img", url="https://x/y"):
        self.filename = filename
        self.content_type = content_type
        self.size = len(data)
        self.url = url
        self._data = data

    async def read(self):
        return self._data


class _FakeReference:
    def __init__(self, message_id=None, resolved=None):
        self.message_id = message_id
        self.resolved = resolved


class _FakeMessage:
    def __init__(
        self,
        *,
        message_id: int,
        channel,
        author,
        content: str,
        mentions=None,
        attachments=None,
        created_at=None,
        embeds=None,
        stickers=None,
        reference=None,
    ):
        self.id = message_id
        self.channel = channel
        self.author = author
        self.content = content
        self.mentions = mentions or []
        self.attachments = attachments or []
        self.created_at = created_at or datetime.now(timezone.utc)
        self.embeds = embeds or []
        self.stickers = stickers or []
        self.reference = reference


class _FakeInboundChannel:
    def __init__(self, channel_id: int, name: str, guild: _FakeGuild | None):
        self.id = channel_id
        self.name = name
        self.guild = guild


class _FakeSendChannel:
    def __init__(self, channel_id: int, name: str, guild: _FakeGuild | None = None):
        self.id = channel_id
        self.name = name
        self.guild = guild
        self.sent = []
        self._next_message_id = 1000

    async def send(self, content, **kwargs):
        self.sent.append((content, kwargs))
        msg_id = self._next_message_id
        self._next_message_id += 1
        return SimpleNamespace(
            id=msg_id,
            content=content,
            author=_FakeUser(id=999, name="agent", display_name="agent"),
            created_at=datetime.now(timezone.utc),
            channel=self,
        )

    def get_partial_message(self, message_id: int):
        return SimpleNamespace(id=message_id)

    async def trigger_typing(self):
        return None


def _make_adapter(tmp_path, **cfg_overrides):
    cfg = DiscordChannelConfig(**cfg_overrides)
    contact_map = ContactMap(tmp_path / "cache")
    history = DiscordHistoryStore(tmp_path / "cache")
    adapter = DiscordAdapter(
        token="token",
        contact_map=contact_map,
        thread_registry=None,
        config=cfg,
        history_store=history,
    )
    return adapter, contact_map, history


@pytest.mark.asyncio
class TestDiscordAdapterIngest:
    async def test_dm_respects_listen_dms_false(self, tmp_path):
        adapter, _, history = _make_adapter(tmp_path, listen_dms=False)
        adapter._agent = _FakeAgent()
        adapter._loop = asyncio.get_running_loop()
        adapter._self_user_id = "999"

        ch = _FakeInboundChannel(1, "dm", None)
        msg = _FakeMessage(
            message_id=10,
            channel=ch,
            author=_FakeUser(id=1, name="alice", display_name="Alice"),
            content="hi",
        )
        await adapter._handle_message(msg)

        assert history.read_events("1") == []
        assert adapter._agent.enqueued == []

    async def test_guild_mention_auto_registers_and_flushes_review(self, tmp_path):
        adapter, _, history = _make_adapter(tmp_path)
        adapter._agent = _FakeAgent()
        adapter._loop = asyncio.get_running_loop()
        adapter._self_user_id = "999"

        guild = _FakeGuild(7, "MyGuild")
        ch = _FakeInboundChannel(123, "general", guild)
        author = _FakeUser(id=1, name="alice", display_name="Alice")
        self_user = _FakeUser(id=999, name="agent")
        msg = _FakeMessage(
            message_id=10,
            channel=ch,
            author=author,
            content="@agent help",
            mentions=[self_user],
        )
        await adapter._handle_message(msg)

        entry = history.get_channel_entry("123")
        assert entry is not None
        assert entry["filter"] == "all"
        assert entry["alias"] == "#general @ MyGuild"
        # Mention review is debounce-triggered; flush directly for deterministic test.
        adapter._flush_mention_review("123")
        assert len(adapter._agent.enqueued) == 1
        inbound = adapter._agent.enqueued[0]
        assert inbound.channel == "discord"
        assert inbound.sender == "#general @ MyGuild"
        assert inbound.metadata["source"] == "guild_mention_review"
        assert "[Message]" in inbound.content
        assert "@agent help" in inbound.content

    async def test_hard_allowlist_blocks_unlisted_channel(self, tmp_path):
        adapter, _, history = _make_adapter(
            tmp_path,
            listen_channels=[{"channel_id": "777", "filter": "mention_only"}],
        )
        adapter._agent = _FakeAgent()
        adapter._loop = asyncio.get_running_loop()
        adapter._self_user_id = "999"
        adapter._hard_allowlist_filters = {"777": "mention_only"}

        guild = _FakeGuild(7, "MyGuild")
        ch = _FakeInboundChannel(123, "general", guild)
        self_user = _FakeUser(id=999, name="agent")
        msg = _FakeMessage(
            message_id=10,
            channel=ch,
            author=_FakeUser(id=1, name="alice", display_name="Alice"),
            content="@agent help",
            mentions=[self_user],
        )
        await adapter._handle_message(msg)
        assert history.get_channel_entry("123") is None

    async def test_dm_typing_resets_timer(self, tmp_path):
        adapter, _, _ = _make_adapter(tmp_path)
        adapter._agent = _FakeAgent()
        adapter._loop = asyncio.get_running_loop()
        adapter._self_user_id = "999"

        ch = _FakeInboundChannel(1, "dm", None)
        msg = _FakeMessage(
            message_id=10,
            channel=ch,
            author=_FakeUser(id=1, name="alice", display_name="Alice"),
            content="hi",
        )
        await adapter._handle_message(msg)
        old_handle = adapter._timers["dm:1"]
        adapter._handle_typing("1", "1")
        new_handle = adapter._timers["dm:1"]
        assert new_handle is not old_handle
        old_handle.cancel()
        new_handle.cancel()
        adapter._timers.clear()

    async def test_dm_burst_extends_debounce_delay(self, tmp_path):
        adapter, _, _ = _make_adapter(
            tmp_path,
            debounce_seconds=5,
            max_wait_seconds=30,
            dm_debounce_seconds=5,
            dm_max_wait_seconds=30,
            dm_typing_quiet_seconds=5,
        )
        adapter._agent = _FakeAgent()
        adapter._loop = asyncio.get_running_loop()
        adapter._self_user_id = "999"

        ch = _FakeInboundChannel(1, "dm", None)
        author = _FakeUser(id=1, name="alice", display_name="Alice")
        await adapter._handle_message(
            _FakeMessage(message_id=10, channel=ch, author=author, content="hi")
        )
        h1 = adapter._timers["dm:1"]
        d1 = h1.when() - adapter._loop.time()

        await adapter._handle_message(
            _FakeMessage(message_id=11, channel=ch, author=author, content="and")
        )
        h2 = adapter._timers["dm:1"]
        d2 = h2.when() - adapter._loop.time()

        assert d2 > d1 + 0.5
        h1.cancel()
        h2.cancel()
        adapter._timers.clear()

    async def test_dm_debounce_respects_max_wait_cap(self, tmp_path):
        adapter, _, _ = _make_adapter(
            tmp_path,
            debounce_seconds=5,
            max_wait_seconds=30,
            dm_debounce_seconds=5,
            dm_max_wait_seconds=6,
            dm_typing_quiet_seconds=5,
        )
        adapter._agent = _FakeAgent()
        adapter._loop = asyncio.get_running_loop()
        adapter._self_user_id = "999"

        ch = _FakeInboundChannel(1, "dm", None)
        author = _FakeUser(id=1, name="alice", display_name="Alice")
        await adapter._handle_message(
            _FakeMessage(message_id=10, channel=ch, author=author, content="hi")
        )
        buf = adapter._dm_buffers["1"]
        buf.first_seen_monotonic = adapter._loop.time() - 5.7
        adapter._reset_timer("dm:1", adapter._flush_dm_buffer, "1")
        handle = adapter._timers["dm:1"]
        delay = handle.when() - adapter._loop.time()
        assert 0 <= delay <= 0.5
        handle.cancel()
        adapter._timers.clear()

    async def test_dm_image_only_gets_extra_wait_without_typing(self, tmp_path):
        adapter, _, _ = _make_adapter(
            tmp_path,
            dm_debounce_seconds=5,
            dm_max_wait_seconds=30,
            dm_typing_quiet_seconds=5,
        )
        adapter._agent = _FakeAgent()
        adapter._loop = asyncio.get_running_loop()

        buf = adapter._dm_buffers["1"] = adapter._dm_buffers.get("1") or None  # type: ignore[assignment]
        if buf is None:
            from lincy.agent.adapters.discord import _DebounceBuffer  # local import for test
            buf = _DebounceBuffer(first_seen_monotonic=adapter._loop.time())
            adapter._dm_buffers["1"] = buf
        buf.messages.append(
            {
                "raw_content": "",
                "attachments": [{"filename": "img.png"}],
            }
        )
        buf.last_message_monotonic = adapter._loop.time()

        delay = adapter._compute_dm_flush_delay(buf)
        assert delay >= 7.5
        assert delay <= 8.5

    async def test_non_image_attachment_downloads_and_surfaces_local_path(self, tmp_path):
        adapter, _, history = _make_adapter(tmp_path)
        adapter._agent = _FakeAgent()
        adapter._loop = asyncio.get_running_loop()
        adapter._self_user_id = "999"

        ch = _FakeInboundChannel(1, "dm", None)
        msg = _FakeMessage(
            message_id=10,
            channel=ch,
            author=_FakeUser(id=1, name="alice", display_name="Alice"),
            content="",
            attachments=[_FakeAttachment(filename="note.txt", content_type="text/plain", data=b"hello")],
        )

        await adapter._handle_message(msg)
        adapter._flush_dm_buffer("1")

        assert len(adapter._agent.enqueued) == 1
        inbound = adapter._agent.enqueued[0]
        assert "[Message]" in inbound.content
        assert "[Attachments]" in inbound.content
        assert "- note.txt (text/plain) -> " in inbound.content

        events = history.read_events("1")
        local_path = events[0]["attachments"][0]["local_path"]
        assert local_path is not None
        assert Path(local_path).read_bytes() == b"hello"

    async def test_attachment_url_is_included_when_download_fails(self, tmp_path):
        adapter, _, _ = _make_adapter(tmp_path)
        adapter._agent = _FakeAgent()
        adapter._loop = asyncio.get_running_loop()
        adapter._self_user_id = "999"
        adapter._download_attachment = AsyncMock(return_value=None)  # type: ignore[method-assign]

        ch = _FakeInboundChannel(1, "dm", None)
        msg = _FakeMessage(
            message_id=10,
            channel=ch,
            author=_FakeUser(id=1, name="alice", display_name="Alice"),
            content="",
            attachments=[_FakeAttachment(filename="note.txt", content_type="text/plain", data=b"hello")],
        )

        await adapter._handle_message(msg)
        adapter._flush_dm_buffer("1")

        assert len(adapter._agent.enqueued) == 1
        inbound = adapter._agent.enqueued[0]
        assert "url: https://x/y" in inbound.content

    async def test_image_attachment_still_downloads_when_auto_read_disabled(self, tmp_path):
        adapter, _, history = _make_adapter(tmp_path, auto_read_images=False)
        adapter._agent = _FakeAgent()
        adapter._loop = asyncio.get_running_loop()
        adapter._self_user_id = "999"

        ch = _FakeInboundChannel(1, "dm", None)
        msg = _FakeMessage(
            message_id=10,
            channel=ch,
            author=_FakeUser(id=1, name="alice", display_name="Alice"),
            content="",
            attachments=[_FakeAttachment(filename="img.png", content_type="image/png", data=b"png")],
        )

        await adapter._handle_message(msg)
        adapter._flush_dm_buffer("1")

        assert len(adapter._agent.enqueued) == 1
        inbound = adapter._agent.enqueued[0]
        assert "- img.png (image/png) -> " in inbound.content
        assert "image_summary:" not in inbound.content

        events = history.read_events("1")
        assert events[0]["attachments"][0]["local_path"] is not None

    async def test_large_attachment_is_not_downloaded_but_keeps_url(self, tmp_path):
        adapter, _, _ = _make_adapter(tmp_path, auto_download_attachment_max_mb=1)
        adapter._agent = _FakeAgent()
        adapter._loop = asyncio.get_running_loop()
        adapter._self_user_id = "999"

        ch = _FakeInboundChannel(1, "dm", None)
        msg = _FakeMessage(
            message_id=10,
            channel=ch,
            author=_FakeUser(id=1, name="alice", display_name="Alice"),
            content="",
            attachments=[_FakeAttachment(filename="large.bin", content_type="application/octet-stream", data=b"x" * (2 * 1024 * 1024))],
        )

        await adapter._handle_message(msg)
        adapter._flush_dm_buffer("1")

        assert len(adapter._agent.enqueued) == 1
        inbound = adapter._agent.enqueued[0]
        assert "- large.bin (application/octet-stream) [too large, not downloaded]" in inbound.content
        assert "url: https://x/y" in inbound.content

    async def test_dm_flush_includes_reply_preview_context(self, tmp_path):
        adapter, _, _ = _make_adapter(tmp_path)
        adapter._agent = _FakeAgent()
        adapter._loop = asyncio.get_running_loop()
        adapter._self_user_id = "999"

        ch = _FakeInboundChannel(1, "dm", None)
        ref_author = _FakeUser(id=2, name="lincy", display_name="Lincy")
        resolved_ref = _FakeMessage(
            message_id=9,
            channel=ch,
            author=ref_author,
            content="上一句內容",
        )
        msg = _FakeMessage(
            message_id=10,
            channel=ch,
            author=_FakeUser(id=1, name="alice", display_name="Alice"),
            content="這句是回覆",
            reference=_FakeReference(message_id=9, resolved=resolved_ref),
        )

        await adapter._handle_message(msg)
        adapter._flush_dm_buffer("1")

        assert len(adapter._agent.enqueued) == 1
        inbound = adapter._agent.enqueued[0]
        assert "[Message]" in inbound.content
        assert "這句是回覆" in inbound.content
        assert "[Reply To]" in inbound.content
        assert "message_id: 9" in inbound.content
        assert "author: Lincy" in inbound.content
        assert "preview: 上一句內容" in inbound.content
        assert inbound.metadata["reply_to_message_id"] == "9"

    async def test_dm_flush_sender_uses_one_hop_alias_mapping(self, tmp_path):
        adapter, contact_map, _ = _make_adapter(tmp_path)
        adapter._agent = _FakeAgent()
        adapter._loop = asyncio.get_running_loop()
        adapter._self_user_id = "999"

        contact_map.update("discord", "1", "idksilver")
        contact_map.update("discord", "idksilver", "毓峰")

        ch = _FakeInboundChannel(1, "dm", None)
        msg = _FakeMessage(
            message_id=10,
            channel=ch,
            author=_FakeUser(id=1, name="idksilver", display_name="idksilver"),
            content="hello",
        )
        await adapter._handle_message(msg)
        adapter._flush_dm_buffer("1")

        assert len(adapter._agent.enqueued) == 1
        inbound = adapter._agent.enqueued[0]
        assert inbound.sender == "毓峰"

    async def test_periodic_review_enqueues_batch_for_filter_all(self, tmp_path):
        adapter, _, history = _make_adapter(tmp_path)
        adapter._agent = _FakeAgent()
        adapter._loop = asyncio.get_running_loop()
        adapter._self_user_id = "999"
        history.upsert_channel(
            channel_id="123",
            guild_id="7",
            guild_name="MyGuild",
            channel_name="general",
            alias="#general @ MyGuild",
            filter_mode="all",
            source="auto_mention",
            review_interval_seconds=1,
        )
        history.append_message_create(
            channel_id="123",
            event={
                "event_time": "2026-02-24T10:00:00+00:00",
                "message_id": "m1",
                "message_time": "2026-02-24T10:00:00+00:00",
                "author_id": "u1",
                "author_name": "alice",
                "author_display_name": "Alice",
                "raw_content": "hello",
                "embeds": [],
                "stickers": [],
                "attachments": [],
                "normalized_text": "hello",
                "reply_to_message_id": None,
                "reply_to_author_id": None,
                "reply_to_author_name": None,
                "reply_to_preview_text": None,
            },
        )
        adapter._run_periodic_review_tick()
        assert len(adapter._agent.enqueued) == 1
        assert adapter._agent.enqueued[0].metadata["source"] == "guild_review"


@pytest.mark.asyncio
class TestDiscordAdapterSend:
    async def test_async_send_splits_and_references(self, tmp_path):
        adapter, _, _ = _make_adapter(tmp_path, send_delay_min=0, send_delay_max=0)
        send_channel = _FakeSendChannel(123, "general", _FakeGuild(7, "Guild"))
        fake_client = SimpleNamespace(
            get_channel=lambda cid: send_channel if cid == 123 else None,
            fetch_channel=None,
        )
        adapter._client = fake_client

        body = "a" * 2100
        await adapter._async_send(
            OutboundMessage(
                channel="discord",
                content=body,
                metadata={"channel_id": "123", "message_id": "456"},
            )
        )

        assert len(send_channel.sent) == 2
        first_kwargs = send_channel.sent[0][1]
        assert "reference" in first_kwargs
        assert first_kwargs["reference"].id == 456

    async def test_async_send_preserves_markdown(self, tmp_path):
        adapter, _, _ = _make_adapter(tmp_path, send_delay_min=0, send_delay_max=0)
        send_channel = _FakeSendChannel(123, "general", _FakeGuild(7, "Guild"))
        adapter._client = SimpleNamespace(
            get_channel=lambda cid: send_channel if cid == 123 else None,
            fetch_channel=None,
        )

        body = "## Hello **world**"
        await adapter._async_send(
            OutboundMessage(
                channel="discord",
                content=body,
                metadata={"channel_id": "123"},
            )
        )

        assert len(send_channel.sent) == 1
        assert send_channel.sent[0][0] == body

    async def test_async_send_records_outbound_in_history(self, tmp_path):
        adapter, _, history = _make_adapter(tmp_path, send_delay_min=0, send_delay_max=0)
        adapter._self_user_id = "999"
        send_channel = _FakeSendChannel(123, "general", _FakeGuild(7, "Guild"))
        adapter._client = SimpleNamespace(
            get_channel=lambda cid: send_channel if cid == 123 else None,
            fetch_channel=None,
        )

        await adapter._async_send(
            OutboundMessage(
                channel="discord",
                content="hi",
                metadata={"channel_id": "123"},
            )
        )

        events = history.read_events("123")
        assert len(events) == 1
        assert events[0]["event_type"] == "message_create"
        assert events[0]["author_id"] == "999"

    async def test_async_send_multi_chunk_records_each(self, tmp_path):
        adapter, _, history = _make_adapter(tmp_path, send_delay_min=0, send_delay_max=0)
        adapter._self_user_id = "999"
        send_channel = _FakeSendChannel(123, "general", _FakeGuild(7, "Guild"))
        adapter._client = SimpleNamespace(
            get_channel=lambda cid: send_channel if cid == 123 else None,
            fetch_channel=None,
        )

        await adapter._async_send(
            OutboundMessage(
                channel="discord",
                content="a" * 2100,
                metadata={"channel_id": "123"},
            )
        )

        events = history.read_events("123")
        assert len(events) == 2
        assert len({e["message_id"] for e in events}) == 2

    async def test_async_send_history_failure_does_not_break_send(self, tmp_path):
        adapter, _, history = _make_adapter(tmp_path, send_delay_min=0, send_delay_max=0)
        adapter._self_user_id = "999"
        send_channel = _FakeSendChannel(123, "general", _FakeGuild(7, "Guild"))
        adapter._client = SimpleNamespace(
            get_channel=lambda cid: send_channel if cid == 123 else None,
            fetch_channel=None,
        )
        history.append_message_create = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

        await adapter._async_send(
            OutboundMessage(
                channel="discord",
                content="hi",
                metadata={"channel_id": "123"},
            )
        )

        assert len(send_channel.sent) == 1

    async def test_async_send_stops_thinking_typing_before_send(self, tmp_path):
        adapter, _, _ = _make_adapter(tmp_path, send_delay_min=1, send_delay_max=1)
        send_channel = _FakeSendChannel(123, "general", _FakeGuild(7, "Guild"))
        fake_client = SimpleNamespace(
            get_channel=lambda cid: send_channel if cid == 123 else None,
            fetch_channel=None,
        )
        adapter._client = fake_client
        adapter._stop_thinking_typing = AsyncMock()  # type: ignore[method-assign]
        adapter._send_typing_once = AsyncMock()  # type: ignore[method-assign]

        async def _fast_sleep(_seconds):
            return None

        with patch("asyncio.sleep", side_effect=_fast_sleep):
            await adapter._async_send(
                OutboundMessage(
                    channel="discord",
                    content="hi",
                    metadata={"channel_id": "123"},
                )
            )

        assert adapter._stop_thinking_typing.await_count >= 1
        assert len(send_channel.sent) == 1

    async def test_turn_hooks_no_longer_schedule_typing(self, tmp_path):
        adapter, _, _ = _make_adapter(tmp_path)
        adapter._agent = _FakeAgent()
        adapter._agent.turn_context = SimpleNamespace(metadata={"channel_id": "123"})
        adapter._loop = MagicMock()
        adapter._loop_ready.set()

        with patch("asyncio.run_coroutine_threadsafe") as run_coro:
            adapter.on_turn_start("discord")
            adapter.on_turn_complete()
        assert run_coro.call_count == 0


@pytest.mark.asyncio
class TestDiscordPresence:
    async def test_presence_auto_marks_online_when_active(self, tmp_path):
        adapter, _, _ = _make_adapter(tmp_path, presence_mode="auto")
        adapter._client = SimpleNamespace(change_presence=AsyncMock())
        adapter._client_ready.set()
        adapter._presence_last_active_monotonic = adapter._presence_last_active_monotonic
        adapter._presence_last_status = None

        await adapter._refresh_presence_once()

        assert adapter._client.change_presence.await_count == 1
        assert adapter._presence_last_status == "online"

    async def test_dm_flush_marks_reviewed_seq(self, tmp_path):
        """DM flush should call mark_reviewed so startup catchup can detect gaps."""
        adapter, _, history = _make_adapter(tmp_path)
        adapter._agent = _FakeAgent()
        adapter._loop = asyncio.get_running_loop()
        adapter._self_user_id = "999"

        ch = _FakeInboundChannel(1, "dm", None)
        msg = _FakeMessage(
            message_id=10,
            channel=ch,
            author=_FakeUser(id=1, name="alice", display_name="Alice"),
            content="hello",
        )
        await adapter._handle_message(msg)
        adapter._flush_dm_buffer("1")

        cursor = history.get_cursor("1")
        assert cursor["last_reviewed_seq"] >= 1

    async def test_flush_all_pending_buffers_on_shutdown(self, tmp_path):
        """Graceful shutdown should flush all pending DM/mention buffers."""
        adapter, _, history = _make_adapter(tmp_path)
        adapter._agent = _FakeAgent()
        adapter._loop = asyncio.get_running_loop()
        adapter._self_user_id = "999"

        ch = _FakeInboundChannel(1, "dm", None)
        msg = _FakeMessage(
            message_id=10,
            channel=ch,
            author=_FakeUser(id=1, name="alice", display_name="Alice"),
            content="important message",
        )
        await adapter._handle_message(msg)
        assert len(adapter._dm_buffers) == 1
        assert len(adapter._agent.enqueued) == 0

        adapter._flush_all_pending_buffers()

        assert len(adapter._dm_buffers) == 0
        assert len(adapter._agent.enqueued) == 1
        assert "important message" in adapter._agent.enqueued[0].content

    async def test_catchup_unreviewed_dm_events(self, tmp_path):
        """Startup catchup should enqueue unreviewed DM events from history."""
        adapter, _, history = _make_adapter(tmp_path)
        adapter._agent = _FakeAgent()
        adapter._self_user_id = "999"

        # Simulate a DM channel that had messages flushed (last_reviewed_seq=1)
        history.upsert_channel(
            channel_id="1",
            guild_id=None,
            guild_name=None,
            channel_name="dm",
            alias="Alice",
            filter_mode="all",
            source="dm",
            extra={"dm_peer_user_id": "42"},
        )
        history.append_message_create(
            channel_id="1",
            event={
                "event_time": "2026-03-05T10:00:00+00:00",
                "message_id": "m1",
                "message_time": "2026-03-05T10:00:00+00:00",
                "author_id": "42",
                "author_name": "alice",
                "author_display_name": "Alice",
                "raw_content": "first msg",
                "embeds": [],
                "stickers": [],
                "attachments": [],
                "normalized_text": "first msg",
                "reply_to_message_id": None,
                "reply_to_author_id": None,
                "reply_to_author_name": None,
                "reply_to_preview_text": None,
            },
        )
        # Mark first message as reviewed (simulates a successful flush)
        history.mark_reviewed("1", seq=1, immediate=True)

        # Simulate a second message that was NOT flushed (crash during debounce)
        history.append_message_create(
            channel_id="1",
            event={
                "event_time": "2026-03-05T10:01:00+00:00",
                "message_id": "m2",
                "message_time": "2026-03-05T10:01:00+00:00",
                "author_id": "42",
                "author_name": "alice",
                "author_display_name": "Alice",
                "raw_content": "lost message",
                "embeds": [],
                "stickers": [],
                "attachments": [],
                "normalized_text": "lost message",
                "reply_to_message_id": None,
                "reply_to_author_id": None,
                "reply_to_author_name": None,
                "reply_to_preview_text": None,
            },
        )

        adapter._catchup_unreviewed_dm_events()

        assert len(adapter._agent.enqueued) == 1
        inbound = adapter._agent.enqueued[0]
        assert inbound.metadata["source"] == "dm_startup_catchup"
        assert inbound.metadata["is_dm"] is True
        assert inbound.metadata["reply_to"] == "42"
        assert "lost message" in inbound.content

    async def test_catchup_sets_baseline_seq_on_first_deploy(self, tmp_path):
        """First deploy should set baseline seq, not replay old DMs."""
        adapter, _, history = _make_adapter(tmp_path)
        adapter._agent = _FakeAgent()
        adapter._self_user_id = "999"

        history.upsert_channel(
            channel_id="1",
            guild_id=None,
            guild_name=None,
            channel_name="dm",
            alias="Alice",
            filter_mode="all",
            source="dm",
        )
        # Old messages exist but last_reviewed_seq is 0 (pre-tracking code)
        history.append_message_create(
            channel_id="1",
            event={
                "event_time": "2026-03-01T10:00:00+00:00",
                "message_id": "m1",
                "message_time": "2026-03-01T10:00:00+00:00",
                "author_id": "42",
                "author_name": "alice",
                "author_display_name": "Alice",
                "raw_content": "old message",
                "embeds": [],
                "stickers": [],
                "attachments": [],
                "normalized_text": "old message",
                "reply_to_message_id": None,
                "reply_to_author_id": None,
                "reply_to_author_name": None,
                "reply_to_preview_text": None,
            },
        )

        adapter._catchup_unreviewed_dm_events()

        # Should NOT replay old messages
        assert len(adapter._agent.enqueued) == 0
        # Should set baseline seq
        cursor = history.get_cursor("1")
        assert cursor["last_reviewed_seq"] == 1

    async def test_presence_auto_does_not_force_idle_after_timeout(self, tmp_path):
        adapter, _, _ = _make_adapter(
            tmp_path,
            presence_mode="auto",
            presence_idle_after_seconds=30,
        )
        adapter._client = SimpleNamespace(change_presence=AsyncMock())
        adapter._client_ready.set()
        adapter._presence_last_status = "online"
        adapter._presence_last_active_monotonic -= 120

        await adapter._refresh_presence_once()

        adapter._client.change_presence.assert_not_awaited()
        assert adapter._presence_last_status == "online"
