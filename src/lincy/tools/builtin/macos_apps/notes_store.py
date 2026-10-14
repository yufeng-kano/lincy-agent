"""Read-only access to the Apple Notes database (NoteStore.sqlite).

Notes.app has no public read API and every Apple Event can cost about a
second on a busy Mac, so all metadata and plain-text reads go straight to
the Core Data store. Writes stay in AppleScript (see notes.py).
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import gzip
import math
from pathlib import Path
import re
import sqlite3
from typing import Any

NOTE_STORE_PATH = (
    Path.home() / "Library" / "Group Containers" / "group.com.apple.notes" / "NoteStore.sqlite"
)

# Core Data stores timestamps as seconds since 2001-01-01 UTC.
_CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
# ZFOLDERTYPE of each account's "Recently Deleted" folder.
_TRASH_FOLDER_TYPE = 1
# Core Data numbers columns that several entities share in ZICCLOUDSYNCINGOBJECT
# (ZTITLE1 is the note title, ZTITLE2 the folder title, ...). The numbering is
# per OS release; these were verified on macOS 26.7.1 and 27.0.1.
_REQUIRED_COLUMNS = frozenset(
    {
        "ZTITLE1",
        "ZTITLE2",
        "ZNAME",
        "ZCREATIONDATE3",
        "ZMODIFICATIONDATE1",
        "ZFOLDER",
        "ZNOTEDATA",
        "ZPARENT",
        "ZOWNER",
        "ZFOLDERTYPE",
        "ZISPASSWORDPROTECTED",
        "ZMARKEDFORDELETION",
        "ZSERVERSHAREDATA",
        "ZNOTE",
        "ZTYPEUTI",
        "ZURLSTRING",
    }
)
# Attachment UTIs that Notes renders as inline data:image in body(); checked
# against body() HTML on real notes. Drawings (com.apple.paper) are not
# counted because body() only sometimes inlines them.
_IMAGE_ATTACHMENT_UTIS = ("public.png", "public.jpeg", "public.heic")
_OBJECT_ID_RE = re.compile(r"^x-coredata://(?P<store>[^/]+)/(?P<entity>\w+)/p(?P<pk>\d+)$")


@dataclass
class StoredNote:
    """One note row: tool-facing metadata plus values used for filtering."""

    meta: dict[str, Any]
    created: datetime
    modified: datetime
    # Decoded plain text; None for password-protected (encrypted) notes.
    text: str | None
    # Image attachments and the first link-card URL, for search results that
    # are built without the HTML body.
    image_count: int
    link_url: str | None


def notes_permission_problems() -> list[str]:
    """Return human-readable problems that prevent reading the Notes database."""
    try:
        NoteStore.open().close()
    except (PermissionError, RuntimeError) as exc:
        return [str(exc)]
    return []


def core_data_to_iso(value: float) -> str:
    """Render a Core Data timestamp exactly like JXA Date.toISOString() does.

    Notes scripting truncates to whole seconds; matching it keeps
    modified_at stable across cache entries written by the old JXA path.
    """
    moment = _CORE_DATA_EPOCH + timedelta(seconds=math.floor(value))
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def note_plain_text(zdata: bytes) -> str:
    """Extract the plain note text from gzipped ZICNOTEDATA.ZDATA.

    The payload is protobuf: NoteStoreProto.document (2) -> Document.note (3)
    -> Note.note_text (2). Attachments appear as U+FFFC in the text.
    """
    buffer = gzip.decompress(zdata)
    for field_number in (2, 3, 2):
        buffer = _first_bytes_field(buffer, field_number)
    return buffer.decode("utf-8")


def _read_varint(buffer: bytes, index: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        byte = buffer[index]
        index += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return value, index


def _first_bytes_field(buffer: bytes, field_number: int) -> bytes:
    """Return the first length-delimited field with the given number."""
    index = 0
    while index < len(buffer):
        key, index = _read_varint(buffer, index)
        wire_type = key & 7
        if wire_type == 0:
            _, index = _read_varint(buffer, index)
        elif wire_type == 1:
            index += 8
        elif wire_type == 5:
            index += 4
        elif wire_type == 2:
            length, index = _read_varint(buffer, index)
            if key >> 3 == field_number:
                return buffer[index : index + length]
            index += length
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
    raise ValueError(f"protobuf field {field_number} not found")


class NoteStore:
    """A read-only connection to NoteStore.sqlite; open one per tool call."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._con = connection
        self._uuid = connection.execute("SELECT Z_UUID FROM Z_METADATA").fetchone()[0]
        self._entities = dict(connection.execute("SELECT Z_NAME, Z_ENT FROM Z_PRIMARYKEY"))
        self._folders: dict[int, dict[str, Any]] | None = None

    @classmethod
    def open(cls) -> NoteStore:
        """Open the store read-only; WAL readers still see Notes' latest commits."""
        try:
            connection = sqlite3.connect(f"{NOTE_STORE_PATH.as_uri()}?mode=ro", uri=True)
            with closing(connection.cursor()) as cursor:
                columns = {
                    row[1] for row in cursor.execute("PRAGMA table_info(ZICCLOUDSYNCINGOBJECT)")
                }
        except sqlite3.Error as exc:
            raise PermissionError(
                f"Apple Notes database is not readable ({NOTE_STORE_PATH}): {exc}. "
                "Grant Full Disk Access in System Settings > Privacy & Security "
                "to the app that runs lincy (e.g. Terminal)."
            ) from exc
        missing = sorted(_REQUIRED_COLUMNS - columns)
        if missing:
            connection.close()
            raise RuntimeError(
                f"unsupported Apple Notes database schema, missing columns: {', '.join(missing)}"
            )
        return cls(connection)

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> NoteStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _object_id(self, entity: str, pk: int) -> str:
        return f"x-coredata://{self._uuid}/{entity}/p{pk}"

    def _parse_object_id(self, object_id: str, entity: str) -> int | None:
        match = _OBJECT_ID_RE.match(object_id.strip())
        if not match or match["store"] != self._uuid or match["entity"] != entity:
            return None
        return int(match["pk"])

    def folders(self) -> dict[int, dict[str, Any]]:
        """Folders by Z_PK, excluding Recently Deleted and anything inside it."""
        if self._folders is not None:
            return self._folders
        accounts = dict(
            self._con.execute(
                "SELECT Z_PK, ZNAME FROM ZICCLOUDSYNCINGOBJECT"
                " WHERE Z_ENT = ? AND ZMARKEDFORDELETION = 0",
                (self._entities["ICAccount"],),
            )
        )
        rows = {
            row[0]: row
            for row in self._con.execute(
                "SELECT Z_PK, ZTITLE2, ZPARENT, ZOWNER, ZFOLDERTYPE FROM ZICCLOUDSYNCINGOBJECT"
                " WHERE Z_ENT = ? AND ZMARKEDFORDELETION = 0",
                (self._entities["ICFolder"],),
            )
        }
        folders: dict[int, dict[str, Any]] = {}
        for pk, (_, title, parent_pk, owner, _) in rows.items():
            if owner not in accounts:
                continue
            names: list[str] = []
            in_trash = False
            current: int | None = pk
            while current in rows:
                _, name, current_parent, _, folder_type = rows[current]
                names.insert(0, name)
                in_trash = in_trash or folder_type == _TRASH_FOLDER_TYPE
                current = current_parent
            if in_trash:
                continue
            folders[pk] = {
                "id": self._object_id("ICFolder", pk),
                "name": title,
                "account": accounts[owner],
                "account_id": self._object_id("ICAccount", owner),
                "path": "/".join([accounts[owner], *names]),
                "parent_pk": parent_pk,
            }
        self._folders = folders
        return folders

    def find_folder(
        self, *, folder_id: str | None, folder_path: str | None
    ) -> dict[str, Any] | None:
        """Resolve a folder by id (preferred) or by catalog path."""
        for folder in self.folders().values():
            if folder_id and folder["id"] == folder_id:
                return folder
            if not folder_id and folder_path and folder["path"] == folder_path:
                return folder
        return None

    def catalog(self) -> list[dict[str, Any]]:
        """Accounts with their folder trees and direct note counts."""
        folders = self.folders()
        counts = dict(
            self._con.execute(
                "SELECT ZFOLDER, COUNT(*) FROM ZICCLOUDSYNCINGOBJECT"
                " WHERE Z_ENT = ? AND ZMARKEDFORDELETION = 0 AND ZFOLDER IS NOT NULL"
                " GROUP BY ZFOLDER",
                (self._entities["ICNote"],),
            )
        )
        nodes = {
            pk: {
                "id": folder["id"],
                "name": folder["name"],
                "account": folder["account"],
                "path": folder["path"],
                "note_count": counts.get(pk, 0),
                "children": [],
            }
            for pk, folder in folders.items()
        }
        accounts: dict[str, dict[str, Any]] = {}
        for pk, folder in sorted(folders.items(), key=lambda item: item[1]["path"]):
            parent = nodes.get(folder["parent_pk"])
            if parent is not None:
                parent["children"].append(nodes[pk])
                continue
            account = accounts.setdefault(
                folder["account_id"],
                {"id": folder["account_id"], "name": folder["account"], "folders": []},
            )
            account["folders"].append(nodes[pk])
        return sorted(accounts.values(), key=lambda account: account["name"])

    def notes(self) -> list[StoredNote]:
        """All notes outside Recently Deleted, with decoded plain text."""
        return self._select_notes("", ())

    def note(self, note_id: str) -> StoredNote | None:
        """One note by its Notes scripting id, with plain text."""
        pk = self._parse_object_id(note_id, "ICNote")
        if pk is None:
            return None
        rows = self._select_notes(" AND n.Z_PK = ?", (pk,))
        return rows[0] if rows else None

    def _select_notes(self, extra_where: str, params: tuple[Any, ...]) -> list[StoredNote]:
        folders = self.folders()
        rows = self._con.execute(
            "SELECT n.Z_PK, n.ZTITLE1, n.ZCREATIONDATE3, n.ZMODIFICATIONDATE1,"
            " n.ZISPASSWORDPROTECTED, n.ZFOLDER,"
            " n.ZSERVERSHAREDATA IS NOT NULL OR f.ZSERVERSHAREDATA IS NOT NULL,"
            " d.ZDATA, COALESCE(a.image_count, 0), a.link_url"
            " FROM ZICCLOUDSYNCINGOBJECT n"
            " JOIN ZICCLOUDSYNCINGOBJECT f ON f.Z_PK = n.ZFOLDER"
            " LEFT JOIN ZICNOTEDATA d ON d.Z_PK = n.ZNOTEDATA"
            " LEFT JOIN ("
            "   SELECT ZNOTE,"
            f"   SUM(ZTYPEUTI IN ({', '.join('?' * len(_IMAGE_ATTACHMENT_UTIS))})) AS image_count,"
            "   MIN(CASE WHEN ZTYPEUTI = 'public.url' THEN ZURLSTRING END) AS link_url"
            "   FROM ZICCLOUDSYNCINGOBJECT"
            "   WHERE Z_ENT = ? AND ZMARKEDFORDELETION = 0 GROUP BY ZNOTE"
            " ) a ON a.ZNOTE = n.Z_PK"
            " WHERE n.Z_ENT = ? AND n.ZMARKEDFORDELETION = 0" + extra_where,
            (
                *_IMAGE_ATTACHMENT_UTIS,
                self._entities["ICAttachment"],
                self._entities["ICNote"],
                *params,
            ),
        )
        notes: list[StoredNote] = []
        for row in rows:
            pk, title, created, modified, locked, folder_pk, shared, zdata, images, link = row
            folder = folders.get(folder_pk)
            if folder is None:
                continue
            notes.append(
                StoredNote(
                    meta={
                        "id": self._object_id("ICNote", pk),
                        "title": title,
                        "created_at": core_data_to_iso(created),
                        "modified_at": core_data_to_iso(modified),
                        "shared": bool(shared),
                        "password_protected": bool(locked),
                        "account": folder["account"],
                        "folder_id": folder["id"],
                        "folder_path": folder["path"],
                    },
                    created=_CORE_DATA_EPOCH + timedelta(seconds=created),
                    modified=_CORE_DATA_EPOCH + timedelta(seconds=modified),
                    text=None if locked else note_plain_text(zdata),
                    image_count=images,
                    link_url=link,
                )
            )
        return notes
