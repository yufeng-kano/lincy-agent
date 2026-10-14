"""Tests for the PhotoKit-backed photos_tool."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import Photos
import pytest

from lincy.tools.builtin.macos_apps import MacOSAppBridge
from lincy.tools.builtin.macos_apps import photos as photos_module
from lincy.tools.builtin.macos_apps import photos_db


class FakeFetch:
    def __init__(self, items):
        self._items = list(items)

    def count(self):
        return len(self._items)

    def objectAtIndex_(self, index):
        return self._items[index]


class FakeCollection:
    def __init__(self, identifier, title, *, folder=False, assets=()):
        self._identifier = identifier
        self._title = title
        self._folder = folder
        self.assets = list(assets)

    def localIdentifier(self):
        return self._identifier

    def localizedTitle(self):
        return self._title

    def canContainCollections(self):
        return self._folder


class FakeAsset:
    def __init__(self, identifier):
        self._identifier = identifier

    def localIdentifier(self):
        return self._identifier

    def creationDate(self):
        return None

    def location(self):
        return None

    def isFavorite(self):
        return False

    def pixelWidth(self):
        return 10

    def pixelHeight(self):
        return 20


class FakeResource:
    def __init__(self, resource_type, filename):
        self._type = resource_type
        self._filename = filename

    def type(self):
        return self._type

    def originalFilename(self):
        return self._filename


class FakeOptions:
    def __init__(self):
        self.predicate = None
        self.fetch_limit = None

    def init(self):
        return self

    def setPredicate_(self, predicate):
        self.predicate = predicate

    def setSortDescriptors_(self, descriptors):
        self.sort_descriptors = descriptors

    def setFetchLimit_(self, limit):
        self.fetch_limit = limit


def _make_bridge(tmp_path: Path) -> MacOSAppBridge:
    return MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=5,
        max_search_results=25,
        photos_export_dir="tmp/photos-exports",
    )


def _fake_photos(**overrides):
    """A stand-in for the Photos module carrying only what a test touches."""
    return SimpleNamespace(
        PHAccessLevelReadWrite=Photos.PHAccessLevelReadWrite,
        PHAuthorizationStatusAuthorized=Photos.PHAuthorizationStatusAuthorized,
        **overrides,
    )


@pytest.fixture
def granted(monkeypatch):
    monkeypatch.setattr(photos_module, "_require_photos_access", lambda: None)


def test_search_predicate_combines_dates_favorite_and_ids():
    start = datetime(2026, 10, 7, tzinfo=timezone.utc)
    end = datetime(2026, 10, 14, 23, 59, tzinfo=timezone.utc)

    predicate = photos_module._search_predicate(
        start=start, end=end, favorite=True, ids={"B/L0/001", "A/L0/001"}
    )

    reference = 978307200
    assert predicate.predicateFormat() == (
        f'creationDate >= CAST({start.timestamp() - reference:.6f}, "NSDate") '
        f'AND creationDate <= CAST({end.timestamp() - reference:.6f}, "NSDate") '
        'AND favorite == 1 AND localIdentifier IN {"A/L0/001", "B/L0/001"}'
    )


def test_search_predicate_is_none_without_filters():
    assert photos_module._search_predicate(start=None, end=None, favorite=None, ids=None) is None


def test_pick_resource_prefers_original_or_edited_render():
    resources = [
        FakeResource(Photos.PHAssetResourceTypeFullSizePhoto, "FullSizeRender.jpeg"),
        FakeResource(Photos.PHAssetResourceTypePhoto, "IMG_0001.HEIC"),
        FakeResource(Photos.PHAssetResourceTypePairedVideo, "IMG_0001.MOV"),
    ]

    original = photos_module._pick_resource(resources, use_originals=True)
    current = photos_module._pick_resource(resources, use_originals=False)

    assert original.originalFilename() == "IMG_0001.HEIC"
    assert current.originalFilename() == "FullSizeRender.jpeg"
    assert photos_module._export_filename(resources, original) == "IMG_0001.HEIC"
    assert photos_module._export_filename(resources, current) == "IMG_0001.jpeg"


def test_pick_resource_returns_none_without_media_resource():
    resources = [FakeResource(Photos.PHAssetResourceTypeAdjustmentData, "Adjustments.plist")]

    assert photos_module._pick_resource(resources, use_originals=False) is None


def test_denied_photos_access_raises_permission_error(monkeypatch, tmp_path: Path):
    library = SimpleNamespace(
        authorizationStatusForAccessLevel_=lambda level: Photos.PHAuthorizationStatusDenied
    )
    monkeypatch.setattr(photos_module, "Photos", _fake_photos(PHPhotoLibrary=library))

    with pytest.raises(PermissionError, match=r"status: denied.*Privacy & Security > Photos"):
        _make_bridge(tmp_path).photos_catalog()


def test_permission_problems_report_photos_and_full_disk_access(monkeypatch):
    library = SimpleNamespace(
        authorizationStatusForAccessLevel_=lambda level: Photos.PHAuthorizationStatusNotDetermined
    )
    monkeypatch.setattr(photos_module, "Photos", _fake_photos(PHPhotoLibrary=library))

    def unreadable():
        raise PermissionError("Cannot read the Photos library database; grant Full Disk Access")

    monkeypatch.setattr(photos_module, "open_library_database", unreadable)

    problems = photos_module.photos_permission_problems()

    assert len(problems) == 2
    assert "status: not determined" in problems[0]
    assert "Full Disk Access" in problems[1]


def test_open_library_database_names_full_disk_access(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(photos_db, "library_database_path", lambda: tmp_path / "missing" / "Photos.sqlite")

    with pytest.raises(PermissionError, match="Full Disk Access"):
        photos_db.open_library_database()


@pytest.fixture
def fake_library_db(monkeypatch, tmp_path: Path):
    """Minimal Photos.sqlite with the Keyword entity numbered like macOS 26."""
    path = tmp_path / "Photos.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER, Z_NAME TEXT);
INSERT INTO Z_PRIMARYKEY VALUES (1, 'AdditionalAssetAttributes'), (52, 'Keyword');
CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID TEXT);
CREATE TABLE ZADDITIONALASSETATTRIBUTES (
  Z_PK INTEGER PRIMARY KEY, ZASSET INTEGER, ZTITLE TEXT, ZORIGINALFILENAME TEXT, ZORIGINALFILESIZE INTEGER
);
CREATE TABLE ZASSETDESCRIPTION (Z_PK INTEGER PRIMARY KEY, ZASSETATTRIBUTES INTEGER, ZLONGDESCRIPTION TEXT);
CREATE TABLE ZKEYWORD (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT);
CREATE TABLE Z_1KEYWORDS (Z_1ASSETATTRIBUTES INTEGER, Z_52KEYWORDS INTEGER);
INSERT INTO ZASSET VALUES (1, 'U1'), (2, 'U2'), (3, 'U3');
INSERT INTO ZADDITIONALASSETATTRIBUTES VALUES
  (11, 1, 'Kyoto Trip', 'IMG_0001.HEIC', 1234),
  (12, 2, '', 'IMG_0002.JPG', 99),
  (13, 3, NULL, '100%_done.png', 5);
INSERT INTO ZASSETDESCRIPTION VALUES (21, 12, '講座 投影片');
INSERT INTO ZKEYWORD VALUES (31, 'family'), (32, 'travel');
INSERT INTO Z_1KEYWORDS VALUES (11, 31), (11, 32);
"""
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(photos_db, "library_database_path", lambda: path)
    return path


def test_asset_text_metadata_reads_title_description_keywords(fake_library_db):
    rows = photos_db.asset_text_metadata(["U1", "U2"])

    assert rows["U1"] == {
        "title": "Kyoto Trip",
        "filename": "IMG_0001.HEIC",
        "description": None,
        "keywords": ["family", "travel"],
        "size": 1234,
    }
    assert rows["U2"] == {
        "title": None,
        "filename": "IMG_0002.JPG",
        "description": "講座 投影片",
        "keywords": [],
        "size": 99,
    }
    assert "U3" not in rows


def test_match_query_uuids_covers_text_fields_case_insensitively(fake_library_db):
    assert photos_db.match_query_uuids("kyoto") == {"U1"}
    assert photos_db.match_query_uuids("img_000") == {"U1", "U2"}
    assert photos_db.match_query_uuids("講座") == {"U2"}
    assert photos_db.match_query_uuids("TRAVEL") == {"U1"}
    # LIKE wildcards in the query are literal text.
    assert photos_db.match_query_uuids("0%_") == {"U3"}
    assert photos_db.match_query_uuids("img%2") == set()


def test_search_rejects_library_wide_filename_sort(granted, tmp_path: Path):
    result = _make_bridge(tmp_path).photos_search(
        album_id=None,
        album_name=None,
        album_path=None,
        folder_id=None,
        folder_path=None,
        query=None,
        start=None,
        end=None,
        favorite=None,
        sort_by="filename_asc",
        limit=None,
    )

    assert result["ok"] is False
    assert "narrower scope" in result["error"]


def test_search_folder_scopes_to_subtree_album_members(granted, monkeypatch, tmp_path: Path):
    inner = FakeCollection("IN/L0/040", "Kyoto", assets=[FakeAsset("A/L0/001")])
    nested_album = FakeCollection("NE/L0/040", "Day 2", assets=[FakeAsset("B/L0/001")])
    nested = FakeCollection("SUB/L0/020", "2026", folder=True)
    trips = FakeCollection("TR/L0/020", "Trips", folder=True)
    outside = FakeCollection("OUT/L0/040", "Other", assets=[FakeAsset("C/L0/001")])
    tree = {
        None: [trips, outside],
        "TR/L0/020": [inner, nested],
        "SUB/L0/020": [nested_album],
    }

    def iter_collections(folder=None, parent_path=""):
        for child in tree[folder.localIdentifier() if folder else None]:
            path = f"{parent_path}/{child.localizedTitle()}" if parent_path else child.localizedTitle()
            yield path, child, folder
            if child.canContainCollections():
                yield from iter_collections(child, path)

    fetched_with: list[FakeOptions] = []

    def fetch_assets(options):
        fetched_with.append(options)
        return FakeFetch([FakeAsset("A/L0/001")])

    monkeypatch.setattr(photos_module, "_iter_collections", iter_collections)
    monkeypatch.setattr(
        photos_module,
        "Photos",
        _fake_photos(
            PHFetchOptions=SimpleNamespace(alloc=FakeOptions),
            PHAsset=SimpleNamespace(
                fetchAssetsInAssetCollection_options_=lambda album, options: FakeFetch(album.assets),
                fetchAssetsWithOptions_=fetch_assets,
            ),
        ),
    )
    monkeypatch.setattr(
        photos_module,
        "asset_text_metadata",
        lambda uuids: {
            uuid: {"title": None, "filename": "f.jpg", "description": None, "keywords": [], "size": 1}
            for uuid in uuids
        },
    )

    result = _make_bridge(tmp_path).photos_search(
        album_id=None,
        album_name=None,
        album_path=None,
        folder_id=None,
        folder_path="Trips",
        query=None,
        start="2026-10-07T00:00",
        end=None,
        favorite=None,
        sort_by=None,
        limit=5,
    )

    assert result["ok"] is True
    assert [(row["id"], row["scope_type"], row["scope_name"]) for row in result["results"]] == [
        ("A/L0/001", "folder", "Trips")
    ]
    predicate = fetched_with[0].predicate.predicateFormat()
    assert 'localIdentifier IN {"A/L0/001", "B/L0/001"}' in predicate
    # Local start time is converted with the app timezone (Asia/Taipei in tests).
    start_offset = datetime(2026, 10, 6, 16, tzinfo=timezone.utc).timestamp() - 978307200
    assert f'creationDate >= CAST({start_offset:.6f}, "NSDate")' in predicate
    assert fetched_with[0].fetch_limit == 5


class FakeChangeLibrary:
    def __init__(self, *, ok=True):
        self.ok = ok
        self.calls = 0

    def performChangesAndWait_error_(self, changes, error):
        self.calls += 1
        changes()
        if self.ok:
            return True, None
        return False, SimpleNamespace(localizedDescription=lambda: "The operation couldn't be completed.")


def _change_photos(library, recorded):
    placeholder = SimpleNamespace(localIdentifier=lambda: "NEW/L0/040")

    def create_album(title):
        recorded["title"] = title
        return SimpleNamespace(placeholderForCreatedAssetCollection=lambda: placeholder)

    def change_folder(folder):
        def add_children(children):
            recorded["folder_children"] = (folder.localIdentifier(), [c.localIdentifier() for c in children])

        return SimpleNamespace(addChildCollections_=add_children)

    def change_album(album):
        def add_assets(assets):
            recorded["added"] = (album.localIdentifier(), [a.localIdentifier() for a in assets])

        return SimpleNamespace(addAssets_=add_assets)

    return _fake_photos(
        PHPhotoLibrary=SimpleNamespace(sharedPhotoLibrary=lambda: library),
        PHAssetCollectionChangeRequest=SimpleNamespace(
            creationRequestForAssetCollectionWithTitle_=create_album,
            changeRequestForAssetCollection_=change_album,
        ),
        PHCollectionListChangeRequest=SimpleNamespace(changeRequestForCollectionList_=change_folder),
        PHAsset=SimpleNamespace(
            fetchAssetsInAssetCollection_options_=lambda album, options: FakeFetch(album.assets),
            fetchAssetsWithLocalIdentifiers_options_=lambda ids, options: FakeFetch(
                [FakeAsset(i) for i in ids if i.startswith("KNOWN")]
            ),
        ),
    )


def test_create_album_inside_folder(granted, monkeypatch, tmp_path: Path):
    folder = FakeCollection("TR/L0/020", "Trips", folder=True)
    created = FakeCollection("NEW/L0/040", "Kyoto")
    library = FakeChangeLibrary()
    recorded: dict = {}
    monkeypatch.setattr(photos_module, "Photos", _change_photos(library, recorded))
    monkeypatch.setattr(
        photos_module,
        "_find_folder",
        lambda folder_id, folder_path: ("Trips", folder) if folder_path == "Trips" else None,
    )
    monkeypatch.setattr(
        photos_module,
        "_find_album",
        lambda album_id, album_name, album_path: ("Trips/Kyoto", created, folder)
        if album_id == "NEW/L0/040"
        else None,
    )

    result = _make_bridge(tmp_path).photos_create_album(
        album_name="Kyoto", parent_folder_id=None, parent_folder_path="Trips"
    )

    assert recorded["title"] == "Kyoto"
    assert recorded["folder_children"] == ("TR/L0/020", ["NEW/L0/040"])
    assert result == {
        "ok": True,
        "album": {
            "id": "NEW/L0/040",
            "name": "Kyoto",
            "path": "Trips/Kyoto",
            "count": 0,
            "parent_folder_id": "TR/L0/020",
            "parent_folder_name": "Trips",
        },
    }


def test_create_album_unknown_parent_folder_changes_nothing(granted, monkeypatch, tmp_path: Path):
    library = FakeChangeLibrary()
    monkeypatch.setattr(photos_module, "Photos", _change_photos(library, {}))
    monkeypatch.setattr(photos_module, "_find_folder", lambda folder_id, folder_path: None)

    result = _make_bridge(tmp_path).photos_create_album(
        album_name="Kyoto", parent_folder_id="nope", parent_folder_path=None
    )

    assert result == {"ok": False, "error": "folder not found"}
    assert library.calls == 0


def test_create_album_reports_photokit_failure(granted, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(photos_module, "Photos", _change_photos(FakeChangeLibrary(ok=False), {}))

    result = _make_bridge(tmp_path).photos_create_album(
        album_name="Kyoto", parent_folder_id=None, parent_folder_path=None
    )

    assert result["ok"] is False
    assert "could not create album" in result["error"]


def test_add_to_album_adds_assets_and_returns_fresh_count(granted, monkeypatch, tmp_path: Path):
    album = FakeCollection("AL/L0/040", "Kyoto", assets=[FakeAsset("KNOWN-1/L0/001")])
    library = FakeChangeLibrary()
    recorded: dict = {}
    monkeypatch.setattr(photos_module, "Photos", _change_photos(library, recorded))
    monkeypatch.setattr(
        photos_module, "_find_album", lambda album_id, album_name, album_path: ("Kyoto", album, None)
    )

    result = _make_bridge(tmp_path).photos_add_to_album(
        album_id=None, album_name=None, album_path="Kyoto", media_ids=["KNOWN-1/L0/001"]
    )

    assert recorded["added"] == ("AL/L0/040", ["KNOWN-1/L0/001"])
    assert result["ok"] is True
    assert result["album"]["count"] == 1


def test_add_to_album_rejects_unknown_media_without_changes(granted, monkeypatch, tmp_path: Path):
    album = FakeCollection("AL/L0/040", "Kyoto")
    library = FakeChangeLibrary()
    monkeypatch.setattr(photos_module, "Photos", _change_photos(library, {}))
    monkeypatch.setattr(
        photos_module, "_find_album", lambda album_id, album_name, album_path: ("Kyoto", album, None)
    )

    result = _make_bridge(tmp_path).photos_add_to_album(
        album_id="AL/L0/040",
        album_name=None,
        album_path=None,
        media_ids=["KNOWN-1/L0/001", "GONE/L0/001"],
    )

    assert result == {"ok": False, "error": "media not found: GONE/L0/001"}
    assert library.calls == 0


def test_tool_output_is_json(granted, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(photos_module, "_iter_collections", lambda: iter(()))

    from lincy.tools.builtin.macos_apps import create_photos_tool

    payload = json.loads(create_photos_tool(_make_bridge(tmp_path))(action="catalog"))

    assert payload == {"ok": True, "folders": [], "albums": []}
