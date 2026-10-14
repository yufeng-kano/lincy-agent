"""Read-only Photos.sqlite access for metadata PhotoKit does not expose.

PhotoKit has no public API for an asset's title, caption, keywords, original
filename or file size, and its fetch predicates reject text matching on any of
them. Reading the library database read-only lets a text query run as one SQL
statement instead of walking the whole library in Python. Rows are joined to
PhotoKit by asset UUID (the part of `localIdentifier` before the first '/').
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

import Photos

_FULL_DISK_ACCESS_HINT = (
    "Grant Full Disk Access to the app that runs lincy (e.g. Terminal) in "
    "System Settings > Privacy & Security > Full Disk Access."
)


def library_database_path() -> Path:
    # The system library is not always "Photos Library.photoslibrary"; the
    # name is localized and user-chosen, so ask PhotoKit where it lives.
    url = Photos.PHPhotoLibrary.sharedPhotoLibrary().photoLibraryURL()
    return Path(url.path()) / "database" / "Photos.sqlite"


def open_library_database() -> sqlite3.Connection:
    """Open the system Photos library database read-only."""
    path = library_database_path()
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        connection.execute("SELECT 1 FROM ZASSET LIMIT 1")
    except sqlite3.Error as exc:
        raise PermissionError(
            f"Cannot read the Photos library database {path} ({exc}). {_FULL_DISK_ACCESS_HINT}"
        ) from exc
    return connection


def _keyword_join(connection: sqlite3.Connection) -> tuple[str, str, str]:
    """Return (table, attributes column, keyword column) of the keyword join table.

    Core Data names many-to-many tables after entity numbers, which shift
    between macOS releases (Keyword is 52 on macOS 26, 53 on macOS 27).
    """
    entities = dict(
        connection.execute(
            "SELECT Z_NAME, Z_ENT FROM Z_PRIMARYKEY "
            "WHERE Z_NAME IN ('AdditionalAssetAttributes', 'Keyword')"
        )
    )
    attributes = entities["AdditionalAssetAttributes"]
    return (
        f"Z_{attributes}KEYWORDS",
        f"Z_{attributes}ASSETATTRIBUTES",
        f"Z_{entities['Keyword']}KEYWORDS",
    )


def asset_text_metadata(uuids: list[str]) -> dict[str, dict[str, Any]]:
    """Return title/filename/description/keywords/size keyed by asset UUID."""
    connection = open_library_database()
    try:
        table, attributes_column, keyword_column = _keyword_join(connection)
        rows = connection.execute(
            f"""
SELECT
  asset.ZUUID,
  attributes.ZTITLE,
  attributes.ZORIGINALFILENAME,
  description.ZLONGDESCRIPTION,
  attributes.ZORIGINALFILESIZE,
  (
    SELECT json_group_array(keyword.ZTITLE)
    FROM {table} AS link JOIN ZKEYWORD AS keyword ON keyword.Z_PK = link.{keyword_column}
    WHERE link.{attributes_column} = attributes.Z_PK
  )
FROM ZASSET AS asset
JOIN ZADDITIONALASSETATTRIBUTES AS attributes ON attributes.ZASSET = asset.Z_PK
LEFT JOIN ZASSETDESCRIPTION AS description ON description.ZASSETATTRIBUTES = attributes.Z_PK
WHERE asset.ZUUID IN (SELECT value FROM json_each(?))
""",
            (json.dumps(uuids),),
        ).fetchall()
    finally:
        connection.close()
    return {
        uuid: {
            "title": title or None,
            "filename": filename,
            "description": description or None,
            "keywords": json.loads(keywords) if keywords else [],
            "size": size,
        }
        for uuid, title, filename, description, size, keywords in rows
    }


def match_query_uuids(query: str) -> set[str]:
    """Return UUIDs whose title, original filename, description or keywords contain query.

    SQLite LIKE folds ASCII case only, which covers the filenames and Latin
    titles a case-insensitive query is for; CJK text has no case.
    """
    pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    connection = open_library_database()
    try:
        table, attributes_column, keyword_column = _keyword_join(connection)
        rows = connection.execute(
            f"""
SELECT asset.ZUUID
FROM ZASSET AS asset
JOIN ZADDITIONALASSETATTRIBUTES AS attributes ON attributes.ZASSET = asset.Z_PK
LEFT JOIN ZASSETDESCRIPTION AS description ON description.ZASSETATTRIBUTES = attributes.Z_PK
WHERE attributes.ZTITLE LIKE :pattern ESCAPE '\\'
   OR attributes.ZORIGINALFILENAME LIKE :pattern ESCAPE '\\'
   OR description.ZLONGDESCRIPTION LIKE :pattern ESCAPE '\\'
   OR EXISTS (
     SELECT 1 FROM {table} AS link JOIN ZKEYWORD AS keyword ON keyword.Z_PK = link.{keyword_column}
     WHERE link.{attributes_column} = attributes.Z_PK AND keyword.ZTITLE LIKE :pattern ESCAPE '\\'
   )
""",
            {"pattern": pattern},
        ).fetchall()
    finally:
        connection.close()
    return {uuid for (uuid,) in rows}
