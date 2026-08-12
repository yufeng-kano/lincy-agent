"""Tests for lincy.memory.curation.queue -- deterministic queue bookkeeping."""

from __future__ import annotations

import json
from pathlib import Path

from lincy.memory.curation.queue import load_queue, remove_queue_entry, upsert_queue_entry


def test_upsert_creates_new_entry(tmp_path: Path):
    upsert_queue_entry(agent_os_dir=tmp_path, rel_path="memory/agent/long-term.md", chars=20000, budget=10000)

    entries = load_queue(tmp_path)
    assert len(entries) == 1
    assert entries[0]["path"] == "memory/agent/long-term.md"
    assert entries[0]["chars"] == 20000
    assert entries[0]["budget"] == 10000
    assert entries[0]["first_seen"] == entries[0]["last_seen"]


def test_upsert_refreshes_existing_entry_without_duplicating(tmp_path: Path):
    upsert_queue_entry(agent_os_dir=tmp_path, rel_path="memory/agent/long-term.md", chars=20000, budget=10000)
    upsert_queue_entry(agent_os_dir=tmp_path, rel_path="memory/agent/long-term.md", chars=21000, budget=10000)

    entries = load_queue(tmp_path)
    assert len(entries) == 1
    assert entries[0]["chars"] == 21000


def test_remove_queue_entry_drops_only_matching_path(tmp_path: Path):
    upsert_queue_entry(agent_os_dir=tmp_path, rel_path="memory/a.md", chars=1, budget=1)
    upsert_queue_entry(agent_os_dir=tmp_path, rel_path="memory/b.md", chars=1, budget=1)

    remove_queue_entry(tmp_path, "memory/a.md")

    entries = load_queue(tmp_path)
    assert [e["path"] for e in entries] == ["memory/b.md"]


def test_load_queue_ignores_malformed_file(tmp_path: Path):
    queue_path = tmp_path / "state" / "memory-curation-queue.json"
    queue_path.parent.mkdir(parents=True)
    queue_path.write_text("not json")

    assert load_queue(tmp_path) == []


def test_load_queue_ignores_non_list_json(tmp_path: Path):
    queue_path = tmp_path / "state" / "memory-curation-queue.json"
    queue_path.parent.mkdir(parents=True)
    queue_path.write_text(json.dumps({"not": "a list"}))

    assert load_queue(tmp_path) == []
