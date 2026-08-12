"""Tests for lincy.memory.curation.snapshot -- archive-snapshot guarantee."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from lincy.memory.curation.snapshot import (
    resolve_curation_target,
    snapshot_path,
    write_verified_snapshot,
)


def test_resolve_curation_target_rejects_archive_path(tmp_path: Path):
    archive_file = tmp_path / "memory/archive/temp-memory/2030-01-01.md"
    archive_file.parent.mkdir(parents=True)
    archive_file.write_text("archived")

    with pytest.raises(ValueError, match="memory/archive"):
        resolve_curation_target(tmp_path, "memory/archive/temp-memory/2030-01-01.md")


def test_resolve_curation_target_rejects_path_escaping_workspace(tmp_path: Path):
    with pytest.raises(ValueError, match="escapes workspace"):
        resolve_curation_target(tmp_path, "../outside.md")


def test_resolve_curation_target_requires_existing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        resolve_curation_target(tmp_path, "memory/agent/missing.md")


def test_resolve_curation_target_returns_resolved_path(tmp_path: Path):
    target = tmp_path / "memory/agent/long-term.md"
    target.parent.mkdir(parents=True)
    target.write_text("content")

    resolved = resolve_curation_target(tmp_path, "memory/agent/long-term.md")

    assert resolved == target.resolve()


def test_snapshot_path_layout():
    path = snapshot_path("memory/people/alice.md", date(2030, 1, 10))
    assert path.as_posix() == "memory/archive/curation/memory/people/alice.md/2030-01-10.md"


def test_write_verified_snapshot_writes_and_verifies(tmp_path: Path):
    snapshot = tmp_path / "memory/archive/curation/memory/people/alice.md/2030-01-10.md"

    write_verified_snapshot(snapshot, "original content\n")

    assert snapshot.read_text() == "original content\n"


def test_write_verified_snapshot_is_idempotent_for_identical_content(tmp_path: Path):
    snapshot = tmp_path / "snap.md"
    write_verified_snapshot(snapshot, "same\n")
    write_verified_snapshot(snapshot, "same\n")

    assert snapshot.read_text() == "same\n"


def test_write_verified_snapshot_refuses_to_replace_differing_snapshot(tmp_path: Path):
    snapshot = tmp_path / "snap.md"
    snapshot.write_text("different existing snapshot\n")

    with pytest.raises(FileExistsError):
        write_verified_snapshot(snapshot, "new content\n")

    assert snapshot.read_text() == "different existing snapshot\n"
