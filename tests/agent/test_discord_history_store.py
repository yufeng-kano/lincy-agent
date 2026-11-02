"""Tests for DiscordHistoryStore."""

from lincy.agent.discord_history import DiscordHistoryStore


class TestDiscordHistoryStore:
    def test_registry_upsert_and_reload(self, tmp_path):
        store = DiscordHistoryStore(tmp_path)
        entry = store.upsert_channel(
            channel_id="c1",
            guild_id="g1",
            guild_name="Guild",
            channel_name="general",
            alias="#general @ Guild",
            filter_mode="all",
            source="auto_mention",
            review_interval_seconds=60,
        )
        assert entry["filter"] == "all"

        store2 = DiscordHistoryStore(tmp_path)
        loaded = store2.get_channel_entry("c1")
        assert loaded is not None
        assert loaded["alias"] == "#general @ Guild"

    def test_cursor_seq_increments_on_append(self, tmp_path):
        store = DiscordHistoryStore(tmp_path)
        seq1 = store.append_message_create(
            channel_id="c1",
            event={
                "event_time": "2026-02-24T10:00:00+00:00",
                "message_id": "m1",
                "message_time": "2026-02-24T10:00:00+00:00",
                "author_id": "u1",
                "author_name": "a",
                "author_display_name": "A",
                "raw_content": "hi",
                "embeds": [],
                "stickers": [],
                "attachments": [],
                "normalized_text": "hi",
            },
        )
        seq2 = store.append_message_create(
            channel_id="c1",
            event={
                "event_time": "2026-02-24T10:01:00+00:00",
                "message_id": "m2",
                "message_time": "2026-02-24T10:01:00+00:00",
                "author_id": "u1",
                "author_name": "a",
                "author_display_name": "A",
                "raw_content": "yo",
                "embeds": [],
                "stickers": [],
                "attachments": [],
                "normalized_text": "yo",
            },
        )
        assert (seq1, seq2) == (1, 2)
        cursor = store.get_cursor("c1")
        assert cursor["next_event_seq"] == 3

    def test_fold_latest_version_marks_edited(self, tmp_path):
        store = DiscordHistoryStore(tmp_path)
        common = {
            "event_time": "2026-02-24T10:00:00+00:00",
            "message_id": "m1",
            "message_time": "2026-02-24T10:00:00+00:00",
            "author_id": "u1",
            "author_name": "a",
            "author_display_name": "A",
            "embeds": [],
            "stickers": [],
            "attachments": [],
            "reply_to_message_id": None,
            "reply_to_author_id": None,
            "reply_to_author_name": None,
            "reply_to_preview_text": None,
        }
        store.append_message_create(
            channel_id="c1",
            event={**common, "raw_content": "old", "normalized_text": "old"},
        )
        store.append_message_edit(
            channel_id="c1",
            event={
                **common,
                "event_time": "2026-02-24T10:01:00+00:00",
                "edited_at": "2026-02-24T10:01:00+00:00",
                "raw_content": "new",
                "normalized_text": "new",
            },
        )
        folded = store.fold_latest_messages(store.read_events("c1"))
        assert len(folded) == 1
        assert folded[0]["content"] == "new"
        assert folded[0]["edited"] is True

    def test_edit_of_older_message_keeps_backfill_cursor(self, tmp_path):
        store = DiscordHistoryStore(tmp_path)
        base = {
            "event_time": "2026-02-24T10:00:00+00:00",
            "message_time": "2026-02-24T10:00:00+00:00",
            "author_id": "u1",
            "raw_content": "x",
            "normalized_text": "x",
        }
        store.append_message_create(channel_id="c1", event={**base, "message_id": "100"})
        store.append_message_create(channel_id="c1", event={**base, "message_id": "200"})

        store.append_message_edit(channel_id="c1", event={**base, "message_id": "100"})
        assert store.get_cursor("c1")["last_seen_message_id"] == "200"

        store.append_message_create(channel_id="c1", event={**base, "message_id": "300"})
        assert store.get_cursor("c1")["last_seen_message_id"] == "300"

    def test_image_summary_cache_put_get(self, tmp_path):
        store = DiscordHistoryStore(tmp_path)
        store.put_image_summary("abc", {"summary": "cat", "tool": "read_image_by_subagent"})
        cached = store.get_image_summary("abc")
        assert cached is not None
        assert cached["summary"] == "cat"
        assert cached["sha256"] == "abc"

    def test_corrupt_history_line_tolerated(self, tmp_path):
        store = DiscordHistoryStore(tmp_path)
        path = store.history_dir / "c1.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"seq":1}\nnot-json\n{"seq":2,"message_id":"m2"}\n', encoding="utf-8")
        events = store.read_events("c1")
        assert len(events) == 2

    def test_make_media_path_uniqueness(self, tmp_path):
        store = DiscordHistoryStore(tmp_path)
        p1 = store.make_media_path("c1", "m1", "img.png")
        p1.parent.mkdir(parents=True, exist_ok=True)
        p1.write_bytes(b"x")
        p2 = store.make_media_path("c1", "m1", "img.png")
        assert p2 != p1
        assert p2.name.startswith("img_")
