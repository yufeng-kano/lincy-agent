"""Tests for the database-backed Apple Notes reads and AppleScript writes."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
import gzip
from pathlib import Path
import sqlite3

import pytest

from lincy.tools.builtin.macos_apps import MacOSAppBridge
from lincy.tools.builtin.macos_apps import notes_store
from lincy.tools.builtin.macos_apps.notes_store import (
    NoteStore,
    core_data_to_iso,
    note_plain_text,
    notes_permission_problems,
)

STORE_UUID = "11111111-2222-3333-4444-555555555555"
ENT_ATTACHMENT, ENT_NOTE, ENT_ACCOUNT, ENT_FOLDER = 5, 12, 14, 15
CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _bytes_field(number: int, payload: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _note_data(text: str) -> bytes:
    """Build a gzipped NoteStoreProto shaped like real Notes data."""
    note = (
        _bytes_field(2, text.encode("utf-8"))
        + _bytes_field(5, _varint(1 << 3) + _varint(len(text)))
    )
    document = _varint(1 << 3) + _varint(0) + _varint(2 << 3) + _varint(0) + _bytes_field(3, note)
    return gzip.compress(_varint(1 << 3) + _varint(0) + _bytes_field(2, document))


def _ts(text: str) -> float:
    return (datetime.fromisoformat(text) - CORE_DATA_EPOCH).total_seconds()


def _note_id(pk: int) -> str:
    return f"x-coredata://{STORE_UUID}/ICNote/p{pk}"


def _folder_id(pk: int) -> str:
    return f"x-coredata://{STORE_UUID}/ICFolder/p{pk}"


def _build_store(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE Z_METADATA (Z_VERSION INTEGER, Z_UUID VARCHAR);
        CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER, Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER);
        CREATE TABLE ZICNOTEDATA (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZNOTE INTEGER, ZDATA BLOB);
        CREATE TABLE ZICCLOUDSYNCINGOBJECT (
            Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZMARKEDFORDELETION INTEGER,
            ZISPASSWORDPROTECTED INTEGER, ZNAME VARCHAR, ZTITLE1 VARCHAR, ZTITLE2 VARCHAR,
            ZPARENT INTEGER, ZOWNER INTEGER, ZFOLDERTYPE INTEGER, ZFOLDER INTEGER,
            ZNOTEDATA INTEGER, ZCREATIONDATE3 TIMESTAMP, ZMODIFICATIONDATE1 TIMESTAMP,
            ZSERVERSHAREDATA BLOB, ZNOTE INTEGER, ZTYPEUTI VARCHAR, ZURLSTRING VARCHAR
        );
        """
    )
    con.execute("INSERT INTO Z_METADATA VALUES (1, ?)", (STORE_UUID,))
    con.executemany(
        "INSERT INTO Z_PRIMARYKEY VALUES (?, ?, 3, 0)",
        [
            (ENT_ATTACHMENT, "ICAttachment"),
            (ENT_NOTE, "ICNote"),
            (ENT_ACCOUNT, "ICAccount"),
            (ENT_FOLDER, "ICFolder"),
        ],
    )
    con.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZMARKEDFORDELETION, ZISPASSWORDPROTECTED, ZNAME)"
        " VALUES (3, ?, 0, 0, 'iCloud')",
        (ENT_ACCOUNT,),
    )
    folders = [
        # pk, title, parent, folder type
        (1, "Recently Deleted", None, 1),
        (2, "Notes", None, 0),
        (10, "Reading", None, 0),
        (11, "Archive", 10, 0),
    ]
    con.executemany(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT"
        " (Z_PK, Z_ENT, ZMARKEDFORDELETION, ZISPASSWORDPROTECTED, ZTITLE2, ZPARENT, ZOWNER, ZFOLDERTYPE)"
        " VALUES (?, ?, 0, 0, ?, ?, 3, ?)",
        [(pk, ENT_FOLDER, title, parent, kind) for pk, title, parent, kind in folders],
    )
    notes = [
        # pk, title, folder, created, modified, locked, marked, text
        (100, "Talk A", 10, "2026-04-11T10:00:00+00:00", "2026-04-11T12:00:00.750+00:00", 0, 0, "Talk A\nspeaker Ming next Wednesday"),
        (101, "Talk B", 10, "2026-04-11T11:00:00+00:00", "2026-04-11T13:00:00+00:00", 0, 0, "Talk B\ntoday's talk notes"),
        (102, "Software update", 11, "2026-04-11T09:00:00+00:00", "2026-04-11T09:30:00+00:00", 0, 0, "Software update\nrelease notes"),
        (103, "Diary", 2, "2026-03-01T00:00:00+00:00", "2026-03-02T00:00:00+00:00", 1, 0, None),
        (104, "Old talk", 1, "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00", 0, 0, "talk in trash"),
        (105, "Purged talk", 10, "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00", 0, 1, "talk purged"),
    ]
    for pk, title, folder, created, modified, locked, marked, text in notes:
        data = _note_data(text) if text is not None else b"encrypted"
        con.execute("INSERT INTO ZICNOTEDATA VALUES (?, 19, ?, ?)", (pk + 1000, pk, data))
        con.execute(
            "INSERT INTO ZICCLOUDSYNCINGOBJECT"
            " (Z_PK, Z_ENT, ZMARKEDFORDELETION, ZISPASSWORDPROTECTED, ZTITLE1, ZFOLDER, ZNOTEDATA,"
            " ZCREATIONDATE3, ZMODIFICATIONDATE1) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pk, ENT_NOTE, marked, locked, title, folder, pk + 1000, _ts(created), _ts(modified)),
        )
    attachments = [
        # pk, note, uti, url, marked
        (300, 100, "public.png", None, 0),
        (301, 100, "com.apple.notes.table", None, 0),
        (302, 101, "public.url", "https://example.com/card", 0),
        (303, 101, "public.jpeg", None, 1),
    ]
    con.executemany(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZMARKEDFORDELETION, ZISPASSWORDPROTECTED,"
        " ZNOTE, ZTYPEUTI, ZURLSTRING) VALUES (?, ?, ?, 0, ?, ?, ?)",
        [(pk, ENT_ATTACHMENT, marked, note, uti, url) for pk, note, uti, url, marked in attachments],
    )
    # A deleted-note stub without a folder, as seen in real stores.
    con.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZMARKEDFORDELETION, ZISPASSWORDPROTECTED)"
        " VALUES (200, ?, 0, 0)",
        (ENT_NOTE,),
    )
    con.commit()
    con.close()


@pytest.fixture
def store_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "Group Containers" / "NoteStore.sqlite"
    path.parent.mkdir()
    _build_store(path)
    monkeypatch.setattr(notes_store, "NOTE_STORE_PATH", path)
    return path


def _bridge(tmp_path: Path, **kwargs) -> MacOSAppBridge:
    return MacOSAppBridge(
        base_dir=tmp_path / "agent",
        allowed_paths=[str(tmp_path)],
        timeout_seconds=5,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
        **kwargs,
    )


def _forbid_notes_app(bridge: MacOSAppBridge) -> None:
    def fail(*args, **kwargs):
        pytest.fail("search called Notes.app")

    bridge._run_jxa_json = fail
    bridge._run_applescript = fail


def _search(bridge: MacOSAppBridge, **kwargs):
    params = {
        "account": None,
        "folder_id": None,
        "folder_path": None,
        "query": None,
        "created_after": None,
        "created_before": None,
        "modified_after": None,
        "modified_before": None,
        "sort_by": None,
        "limit": None,
        "offset": None,
    }
    params.update(kwargs)
    return bridge.notes_search(**params)


def test_note_plain_text_reads_document_note_text_field():
    assert note_plain_text(_note_data("Title\nbody ￼ text")) == "Title\nbody ￼ text"


def test_core_data_to_iso_truncates_to_whole_seconds_like_jxa():
    assert core_data_to_iso(_ts("2026-04-11T12:00:00.750+00:00")) == "2026-04-11T12:00:00.000Z"


def test_catalog_builds_folder_tree_without_recently_deleted(store_path: Path):
    with NoteStore.open() as store:
        accounts = store.catalog()

    assert [account["name"] for account in accounts] == ["iCloud"]
    assert accounts[0]["id"] == f"x-coredata://{STORE_UUID}/ICAccount/p3"
    folders = accounts[0]["folders"]
    assert [folder["path"] for folder in folders] == ["iCloud/Notes", "iCloud/Reading"]
    reading = folders[1]
    assert reading["id"] == _folder_id(10)
    assert reading["note_count"] == 2
    assert [child["path"] for child in reading["children"]] == ["iCloud/Reading/Archive"]


def test_notes_excludes_trash_purged_and_folderless_rows(store_path: Path):
    with NoteStore.open() as store:
        notes = {note.meta["id"]: note for note in store.notes()}

    assert set(notes) == {_note_id(100), _note_id(101), _note_id(102), _note_id(103)}
    assert notes[_note_id(100)].text.startswith("Talk A")
    assert notes[_note_id(102)].meta["folder_path"] == "iCloud/Reading/Archive"
    locked = notes[_note_id(103)]
    assert locked.meta["password_protected"] is True
    assert locked.text is None


def test_note_lookup_requires_matching_store_and_entity(store_path: Path):
    with NoteStore.open() as store:
        assert store.note(_note_id(100)).meta["title"] == "Talk A"
        assert store.note(_note_id(104)) is None
        assert store.note("x-coredata://OTHER/ICNote/p100") is None
        assert store.note(_folder_id(10)) is None


def test_unreadable_store_reports_full_disk_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(notes_store, "NOTE_STORE_PATH", tmp_path / "missing" / "NoteStore.sqlite")

    problems = notes_permission_problems()

    assert len(problems) == 1
    assert "Full Disk Access" in problems[0]
    with pytest.raises(PermissionError):
        NoteStore.open()


def test_schema_without_expected_columns_fails_early(store_path: Path):
    con = sqlite3.connect(store_path)
    con.execute("ALTER TABLE ZICCLOUDSYNCINGOBJECT DROP COLUMN ZTITLE1")
    con.commit()
    con.close()

    with pytest.raises(RuntimeError, match="ZTITLE1"):
        NoteStore.open()
    assert "ZTITLE1" in notes_permission_problems()[0]


def test_search_filters_sorts_and_pages_from_database(store_path: Path, tmp_path: Path):
    bridge = _bridge(tmp_path)
    _forbid_notes_app(bridge)

    payload = _search(bridge, folder_path="iCloud/Reading", sort_by="created_asc", limit=1, offset=1)

    assert payload["ok"] is True
    assert payload["total_matches"] == 2
    assert payload["has_more"] is False
    assert [item["id"] for item in payload["results"]] == [_note_id(101)]

    payload = _search(bridge, modified_after="2026-04-11T20:30", modified_before="2026-04-11T21:00")
    assert [item["id"] for item in payload["results"]] == [_note_id(101)]

    payload = _search(bridge, account="Other")
    assert payload["total_matches"] == 0


def test_search_query_matches_database_text_and_skips_trash(store_path: Path, tmp_path: Path):
    bridge = _bridge(tmp_path)
    _forbid_notes_app(bridge)

    payload = _search(bridge, query="TALK")

    assert [item["id"] for item in payload["results"]] == [_note_id(101), _note_id(100)]
    assert _search(bridge, query="speaker ming")["total_matches"] == 1


def test_search_returns_locked_note_metadata_without_fetching_body(store_path: Path, tmp_path: Path):
    bridge = _bridge(tmp_path)
    _forbid_notes_app(bridge)

    payload = _search(bridge, folder_path="iCloud/Notes")

    result = payload["results"][0]
    assert result["password_protected"] is True
    assert result["content_kind"] == "locked_note"
    assert result["summary"] == ""
    assert result["summary_source"] == "none"


def test_search_unknown_folder_is_an_error(store_path: Path, tmp_path: Path):
    payload = _search(_bridge(tmp_path), folder_path="iCloud/Nope")

    assert payload == {"ok": False, "error": "notes folder not found"}


class _FakeSummarizer:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def chat(self, messages, response_schema=None, temperature=None):
        self.inputs.append(messages[1].content)
        return f"summary {len(self.inputs)}"


def test_uncached_search_summarizes_plain_text_without_notes_app(store_path: Path, tmp_path: Path):
    summarizer = _FakeSummarizer()
    bridge = _bridge(tmp_path, notes_summarizer=summarizer)
    _forbid_notes_app(bridge)

    first = _search(bridge, folder_path="iCloud/Reading", sort_by="created_asc")
    again = _search(bridge, folder_path="iCloud/Reading", sort_by="created_asc")

    talk_a, talk_b = first["results"]
    assert talk_a["summary_source"] == "plain_text"
    assert talk_a["summary"].startswith("summary")
    assert talk_a["has_images"] is True
    assert talk_a["image_count"] == 1
    assert talk_a["content_kind"] == "mixed_note"
    assert talk_b["image_count"] == 0
    assert talk_b["source_url"] == "https://example.com/card"
    assert talk_b["content_kind"] == "web_clip_text"
    assert any("speaker Ming next Wednesday" in text for text in summarizer.inputs)
    assert again["results"] == first["results"]
    assert len(summarizer.inputs) == 2


def test_search_query_matches_plain_summary_while_note_is_unchanged(store_path: Path, tmp_path: Path):
    bridge = _bridge(tmp_path, notes_summarizer=_FakeSummarizer())
    _forbid_notes_app(bridge)
    first = _search(bridge, folder_path="iCloud/Reading")
    summarized = next(item["id"] for item in first["results"] if item["summary"] == "summary 1")

    assert [item["id"] for item in _search(bridge, query="SUMMARY 1")["results"]] == [summarized]

    con = sqlite3.connect(store_path)
    con.execute(
        "UPDATE ZICCLOUDSYNCINGOBJECT SET ZMODIFICATIONDATE1 = ? WHERE Z_PK = ?",
        (_ts("2026-05-01T00:00:00+00:00"), int(summarized.rsplit("/p", 1)[1])),
    )
    con.commit()
    con.close()
    assert _search(bridge, query="summary 1")["total_matches"] == 0


def test_get_after_plain_text_search_fetches_body_and_upgrades_cache(store_path: Path, tmp_path: Path):
    summarizer = _FakeSummarizer()
    bridge = _bridge(tmp_path, notes_summarizer=summarizer)
    _forbid_notes_app(bridge)
    plain = _search(bridge, folder_path="iCloud/Reading/Archive")
    assert plain["results"][0]["summary_source"] == "plain_text"

    fetched: list[str] = []
    bridge._notes_fetch_body = lambda *, note_id: fetched.append(note_id) or "<div>poster about robotics</div>"
    note = bridge.notes_get(note_id=_note_id(102))["note"]
    assert fetched == [_note_id(102)]
    assert note["content_markdown"] == "poster about robotics"

    bridge._notes_fetch_body = lambda *, note_id: pytest.fail("search fetched body")
    rich = _search(bridge, folder_path="iCloud/Reading/Archive", query="robotics")

    result = rich["results"][0]
    assert result["summary_source"] == "cache"
    assert result["summary"] == "summary 2"
    assert "poster about robotics" in summarizer.inputs[1]


def test_get_renders_markdown_and_embedded_image_summary(store_path: Path, tmp_path: Path):
    class FakeVisionAgent:
        def describe(self, image_parts):
            assert image_parts[1].data
            return "Poster: next Wednesday 7pm"

    bridge = _bridge(tmp_path, vision_agent=FakeVisionAgent())
    image_data = base64.b64encode(b"fake-image-bytes").decode("ascii")
    bridge._notes_fetch_body = lambda *, note_id: (
        '<div><a href="https://example.com/post">source</a></div>'
        f'<div><img src="data:image/png;base64,{image_data}"></div>'
        "<div>explanation</div>"
    )

    payload = bridge.notes_get(note_id=_note_id(100))

    note = payload["note"]
    assert note["title"] == "Talk A"
    assert note["created_at"] == "2026-04-11T10:00:00.000Z"
    assert note["folder_id"] == _folder_id(10)
    assert note["folder_path"] == "iCloud/Reading"
    assert "data:image/png;base64" not in note["content_markdown"]
    assert "Poster" in note["content_markdown"]
    assert note["source_url"] == "https://example.com/post"
    assert note["content_kind"] == "web_clip_image"


def test_get_cached_note_reports_current_folder(store_path: Path, tmp_path: Path):
    bridge = _bridge(tmp_path)
    bridge._notes_fetch_body = lambda *, note_id: "<div>body</div>"
    bridge.notes_get(note_id=_note_id(100))

    con = sqlite3.connect(store_path)
    con.execute("UPDATE ZICCLOUDSYNCINGOBJECT SET ZFOLDER = 2 WHERE Z_PK = 100")
    con.commit()
    con.close()
    bridge._notes_fetch_body = lambda *, note_id: pytest.fail("cache miss")

    assert bridge.notes_get(note_id=_note_id(100))["note"]["folder_path"] == "iCloud/Notes"


def test_get_missing_note_is_an_error(store_path: Path, tmp_path: Path):
    payload = _bridge(tmp_path).notes_get(note_id=_note_id(104))

    assert payload == {"ok": False, "error": f"note not found: {_note_id(104)}"}


def _capture_applescript(bridge: MacOSAppBridge, result: str) -> list[dict]:
    calls: list[dict] = []

    def fake_run_applescript(script, *, env=None, utf8_files=None, **kwargs):
        calls.append({"script": script, "env": env or {}, "files": utf8_files or {}})
        return result

    bridge._run_applescript = fake_run_applescript
    bridge.notes_get = lambda *, note_id: {"ok": True, "note": {"id": note_id}}
    return calls


def test_writes_use_direct_id_references(store_path: Path, tmp_path: Path):
    bridge = _bridge(tmp_path)
    calls = _capture_applescript(bridge, _note_id(100))

    bridge.notes_create(
        folder_id=None,
        folder_path="iCloud/Reading/Archive",
        title="New",
        body="text",
        template_markdown=None,
        variables=None,
        images=None,
    )
    bridge.notes_update(
        note_id=_note_id(100),
        title=None,
        body="more",
        template_markdown=None,
        variables=None,
        images=None,
        append=True,
    )
    bridge.notes_move(note_id=_note_id(100), target_folder_id=None, target_folder_path="iCloud/Notes")

    create, update, move = calls
    assert "make new note at folder id folderId" in create["script"]
    assert create["env"]["FOLDER_ID"] == _folder_id(11)
    assert "set body of targetNote to (body of targetNote) & noteBody" in update["script"]
    assert "note id noteId" in update["script"]
    assert "move targetNote to folder id targetFolderId" in move["script"]
    assert move["env"]["TARGET_FOLDER_ID"] == _folder_id(2)
    assert not any("whose" in call["script"] for call in calls)


def test_update_rejects_missing_and_locked_notes(store_path: Path, tmp_path: Path):
    bridge = _bridge(tmp_path)
    calls = _capture_applescript(bridge, "")
    kwargs = dict(title=None, body="x", template_markdown=None, variables=None, images=None, append=False)

    assert bridge.notes_update(note_id=_note_id(104), **kwargs)["ok"] is False
    assert "password-protected" in bridge.notes_update(note_id=_note_id(103), **kwargs)["error"]
    assert bridge.notes_move(note_id=_note_id(104), target_folder_id=_folder_id(2), target_folder_path=None)["ok"] is False
    assert calls == []


def test_cache_refreshes_after_same_second_modification(store_path: Path, tmp_path: Path):
    bridge = _bridge(tmp_path)
    bridge._notes_fetch_body = lambda *, note_id: "<div>first</div>"
    bridge.notes_get(note_id=_note_id(100))

    con = sqlite3.connect(store_path)
    con.execute("UPDATE ZICCLOUDSYNCINGOBJECT SET ZMODIFICATIONDATE1 = ZMODIFICATIONDATE1 + 0.1 WHERE Z_PK = 100")
    con.commit()
    con.close()
    bridge._notes_fetch_body = lambda *, note_id: "<div>second</div>"

    note = bridge.notes_get(note_id=_note_id(100))["note"]
    assert note["modified_at"] == "2026-04-11T12:00:00.000Z"
    assert note["content_markdown"] == "second"


def test_unsynced_or_undecodable_note_degrades_alone(store_path: Path, tmp_path: Path):
    con = sqlite3.connect(store_path)
    # 106: body row without ZDATA and no timestamps (shared note not downloaded).
    # 107: ZDATA that is not gzip. 108: no ZICNOTEDATA row at all.
    con.execute("INSERT INTO ZICNOTEDATA VALUES (1106, 19, 106, NULL)")
    con.execute("INSERT INTO ZICNOTEDATA VALUES (1107, 19, 107, ?)", (b"not gzip",))
    con.executemany(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT"
        " (Z_PK, Z_ENT, ZMARKEDFORDELETION, ZISPASSWORDPROTECTED, ZTITLE1, ZFOLDER, ZNOTEDATA,"
        " ZCREATIONDATE3, ZMODIFICATIONDATE1) VALUES (?, ?, 0, 0, ?, 2, ?, ?, ?)",
        [
            (106, ENT_NOTE, "Shared", 1106, None, None),
            (107, ENT_NOTE, "Garbled", 1107, _ts("2026-03-05T00:00:00+00:00"), _ts("2026-03-06T00:00:00+00:00")),
            (108, ENT_NOTE, "No data", None, _ts("2026-03-03T00:00:00+00:00"), _ts("2026-03-04T00:00:00+00:00")),
        ],
    )
    con.commit()
    con.close()
    bridge = _bridge(tmp_path, notes_summarizer=_FakeSummarizer())
    _forbid_notes_app(bridge)

    payload = _search(bridge, folder_path="iCloud/Notes")

    results = {item["id"]: item for item in payload["results"]}
    # Undated notes sort last; the readable notes in other folders are unaffected.
    assert [item["id"] for item in payload["results"]] == [_note_id(107), _note_id(108), _note_id(103), _note_id(106)]
    for pk in (106, 107, 108):
        assert results[_note_id(pk)]["content_kind"] == "unavailable_note"
        assert results[_note_id(pk)]["summary_source"] == "none"
    assert results[_note_id(106)]["created_at"] is None
    assert results[_note_id(106)]["modified_at"] is None
    assert _search(bridge, query="talk")["total_matches"] == 2
    dated = _search(bridge, modified_after="2026-01-01T00:00", limit=10)
    assert dated["total_matches"] == 6
    assert _note_id(106) not in [item["id"] for item in dated["results"]]

    # Bodies of 106-108 were never searched, so a miss is not exact; the
    # locked note 103 has no searchable body at all and is not counted.
    missed = _search(bridge, query="nothing like this")
    assert (missed["total_matches"], missed["total_matches_exact"], missed["body_unavailable_count"]) == (0, False, 3)
    assert "body of 3 note(s)" in missed["warning"]
    by_title = _search(bridge, query="garbled")
    assert (by_title["total_matches"], by_title["body_unavailable_count"]) == (1, 2)
    unfiltered = _search(bridge, folder_path="iCloud/Notes")
    assert unfiltered["total_matches_exact"] is True
    assert unfiltered["warning"] is None
    assert "body_unavailable_count" not in unfiltered

    bridge._notes_fetch_body = lambda *, note_id: "<div>shared body</div>"
    note = bridge.notes_get(note_id=_note_id(106))["note"]
    assert note["content_markdown"] == "shared body"
    assert note["modified_at"] is None
