"""macOS app tool implementation."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .notes_template import _applescript_utf8_file_read
from .runtime import *

PHOTOS_TOOL_DEFINITION = ToolDefinition(
    name="photos_tool",
    description=(
        "Access the user's real macOS Photos library. "
        "'catalog' lists albums/folders, "
        "'search' searches media by album/date/query/favorite, "
        "'get_album' fetches a single album by id, name, or path, "
        "'get_media' fetches media metadata by id, "
        "'export' exports media items to files, "
        "'create_album' creates an album, "
        "'add_to_album' adds media items to an album."
    ),
    parameters={
        "action": ToolParameter(
            type="string",
            description="Action to perform.",
            enum=[
                "catalog",
                "search",
                "get_album",
                "get_media",
                "export",
                "create_album",
                "add_to_album",
            ],
        ),
        "album_id": ToolParameter(
            type="string",
            description="Album id. Preferred over album_name when known.",
        ),
        "album_name": ToolParameter(
            type="string",
            description="Exact album name.",
        ),
        "album_path": ToolParameter(
            type="string",
            description="Album path from catalog, e.g. 'Trips/2026 Kyoto'.",
        ),
        "folder_id": ToolParameter(
            type="string",
            description="Photos folder id. Search scopes across albums inside that folder subtree.",
        ),
        "folder_path": ToolParameter(
            type="string",
            description="Photos folder path from catalog, e.g. 'Trips/2026'.",
        ),
        "parent_folder_id": ToolParameter(
            type="string",
            description="Optional parent Photos folder id when creating an album.",
        ),
        "parent_folder_path": ToolParameter(
            type="string",
            description="Optional parent Photos folder path when creating an album.",
        ),
        "query": ToolParameter(
            type="string",
            description="Case-insensitive text query matched against media title, filename, description, and keywords.",
        ),
        "start": ToolParameter(
            type="string",
            description="Local ISO datetime lower bound for media date.",
        ),
        "end": ToolParameter(
            type="string",
            description="Local ISO datetime upper bound for media date.",
        ),
        "favorite": ToolParameter(
            type="boolean",
            description="Filter by favorite flag.",
        ),
        "sort_by": ToolParameter(
            type="string",
            description="Sort order for search results.",
            enum=["date_desc", "date_asc", "filename_asc"],
        ),
        "media_ids": ToolParameter(
            type="array",
            description="List of Photos media item ids. Required for get_media/export/add_to_album.",
            items={"type": "string"},
        ),
        "destination_dir": ToolParameter(
            type="string",
            description="Export destination directory. Must be within allowed paths.",
        ),
        "use_originals": ToolParameter(
            type="boolean",
            description="When true, export original assets. Default true.",
        ),
        "limit": ToolParameter(
            type="integer",
            description="Maximum number of search results to return.",
        ),
    },
    required=["action"],
)

class MacOSAppBridge:

    def photos_catalog(self) -> dict[str, Any]:
        """List Photos folders and albums."""
        script = """
const app = Application("Photos");
function walkFolder(folder, parentPath) {
  const path = parentPath ? `${parentPath}/${folder.name()}` : folder.name();
  return {
    id: folder.id(),
    name: folder.name(),
    path,
    albums: folder.albums().map((album) => ({
      id: album.id(),
      name: album.name(),
      path: `${path}/${album.name()}`,
      count: album.mediaItems().length,
    })),
    children: folder.folders().map((child) => walkFolder(child, path)),
  };
}
const rootFolders = app.folders().filter((folder) => !folder.parent());
const folders = rootFolders.map((folder) => walkFolder(folder, ""));
const topLevelAlbums = app.albums()
  .filter((album) => !album.parent())
  .map((album) => ({
    id: album.id(),
    name: album.name(),
    path: album.name(),
    count: album.mediaItems().length,
  }));
return { ok: true, folders, albums: topLevelAlbums };
"""
        return self._run_jxa_json(script)

    def photos_search(
        self,
        *,
        album_id: str | None,
        album_name: str | None,
        album_path: str | None,
        folder_id: str | None,
        folder_path: str | None,
        query: str | None,
        start: str | None,
        end: str | None,
        favorite: bool | None,
        sort_by: str | None,
        limit: int | None,
    ) -> dict[str, Any]:
        """Search the Photos library."""
        if (
            not any([album_id, album_name, album_path, folder_id, folder_path, query, start, end])
            and favorite is None
            and sort_by is not None
        ):
            return {
                "ok": False,
                "error": (
                    "sorting the entire Photos library requires a narrower scope; "
                    "provide album_path, folder_path, query, start/end, or favorite"
                ),
            }
        script = f"""
const app = Application("Photos");
const payload = readPayload();
const limit = clampLimit(payload.limit, {self._max_search_results});
const query = lower(payload.query || "");
const start = payload.start ? new Date(payload.start) : null;
const end = payload.end ? new Date(payload.end) : null;
function flattenFolder(folder, parentPath) {{
  const path = parentPath ? `${{parentPath}}/${{folder.name()}}` : folder.name();
  let rows = [{{
    id: folder.id(),
    name: folder.name(),
    path,
    albums: folder.albums().map((album) => ({{
      id: album.id(),
      name: album.name(),
      path: `${{path}}/${{album.name()}}`,
      album,
    }})),
  }}];
  for (const child of folder.folders()) {{
    rows = rows.concat(flattenFolder(child, path));
  }}
  return rows;
}}
let folderRows = [];
for (const folder of app.folders()) {{
  try {{
    if (!folder.parent()) {{
      folderRows = folderRows.concat(flattenFolder(folder, ""));
    }}
  }} catch (error) {{}}
}}
let scopeType = "library";
let scopeName = null;
let items = [];
if (payload.album_id || payload.album_name || payload.album_path) {{
  let album = null;
  if (payload.album_id) {{
    const matches = app.albums.whose({{ id: payload.album_id }})();
    album = matches.length > 0 ? matches[0] : null;
  }} else if (payload.album_path) {{
    for (const folderRow of folderRows) {{
      const match = folderRow.albums.find((row) => row.path === payload.album_path);
      if (match) {{
        album = match.album;
        break;
      }}
    }}
    if (!album) {{
      const topLevelAlbum = app.albums().find((candidate) => candidate.name() === payload.album_path && !candidate.parent());
      album = topLevelAlbum || null;
    }}
  }} else {{
    album = app.albums.byName(payload.album_name);
    if (!album.exists()) {{
      album = null;
    }}
  }}
  if (!album) {{
    return {{ ok: false, error: "album not found" }};
  }}
  scopeType = "album";
  scopeName = album.name();
  items = album.mediaItems();
}} else if (payload.folder_id || payload.folder_path) {{
  const targetFolder = folderRows.find((row) => row.id === payload.folder_id || row.path === payload.folder_path);
  if (!targetFolder) {{
    return {{ ok: false, error: "folder not found" }};
  }}
  scopeType = "folder";
  scopeName = targetFolder.path;
  const targetFolders = folderRows.filter((row) => row.path === targetFolder.path || row.path.startsWith(`${{targetFolder.path}}/`));
  const byId = new Map();
  for (const row of targetFolders) {{
    for (const albumRow of row.albums) {{
      for (const item of albumRow.album.mediaItems()) {{
        byId.set(item.id(), item);
      }}
    }}
  }}
  items = Array.from(byId.values());
}} else {{
  items = app.mediaItems();
}}
const results = [];
for (const item of items) {{
  const keywords = item.keywords() || [];
  const row = {{
    id: item.id(),
    title: valueOrNull(item.name()),
    filename: valueOrNull(item.filename()),
    description: valueOrNull(item.description()),
    date: iso(item.date()),
    favorite: !!item.favorite(),
    keywords: keywords.map((keyword) => keyword.toString()),
    width: valueOrNull(item.width()),
    height: valueOrNull(item.height()),
    size: valueOrNull(item.size()),
    location: valueOrNull(item.location()),
    scope_type: scopeType,
    scope_name: scopeName,
  }};
  if (start && row.date && new Date(row.date) < start) {{
    continue;
  }}
  if (end && row.date && new Date(row.date) > end) {{
    continue;
  }}
  if (payload.favorite !== null && payload.favorite !== undefined && row.favorite !== payload.favorite) {{
    continue;
  }}
  const haystack = lower(`${{row.title || ""}}\\n${{row.filename || ""}}\\n${{row.description || ""}}\\n${{row.keywords.join(" ")}}`);
  if (query && !haystack.includes(query)) {{
    continue;
  }}
  results.push(row);
  if (!payload.sort_by && results.length >= limit) {{
    break;
  }}
}}
if (payload.sort_by === "date_asc") {{
  results.sort((a, b) => compareIsoAsc(a.date, b.date));
}} else if (payload.sort_by === "filename_asc") {{
  results.sort((a, b) => compareTextAsc(a.filename, b.filename));
}} else if (payload.sort_by === "date_desc") {{
  results.sort((a, b) => compareIsoDesc(a.date, b.date));
}}
results.splice(limit);
return {{ ok: true, results, count: results.length }};
"""
        return self._run_jxa_json(
            script,
            payload={
                "album_id": album_id,
                "album_name": album_name,
                "album_path": album_path,
                "folder_id": folder_id,
                "folder_path": folder_path,
                "query": query,
                "start": start,
                "end": end,
                "favorite": favorite,
                "sort_by": sort_by,
                "limit": limit,
            },
        )

    def photos_create_album(
        self,
        *,
        album_name: str,
        parent_folder_id: str | None,
        parent_folder_path: str | None,
    ) -> dict[str, Any]:
        """Create a Photos album."""
        if parent_folder_path and not parent_folder_id:
            resolved = self._resolve_photo_folder(
                folder_id=None,
                folder_path=parent_folder_path,
            )
            if not resolved.get("ok"):
                return resolved
            parent_folder_id = resolved["folder"]["id"]
        env = {"PARENT_FOLDER_ID": parent_folder_id or ""}
        script = f"""
set albumName to {_applescript_utf8_file_read("ALBUM_NAME")}
set parentFolderId to system attribute "PARENT_FOLDER_ID"
tell application "Photos"
  if parentFolderId is "" then
    set targetAlbum to make new album named albumName
  else
    set targetFolder to first folder whose id is parentFolderId
    set targetAlbum to make new album named albumName at targetFolder
  end if
  return id of targetAlbum
end tell
"""
        album_id = self._run_applescript(
            script,
            env=env,
            utf8_files={"ALBUM_NAME": album_name},
        )
        return self._photos_get_album(album_id=album_id)

    def photos_add_to_album(
        self,
        *,
        album_id: str | None,
        album_name: str | None,
        album_path: str | None,
        media_ids: list[str],
    ) -> dict[str, Any]:
        """Add media items to an album."""
        target = self._resolve_photo_album(
            album_id=album_id,
            album_name=album_name,
            album_path=album_path,
        )
        if not target.get("ok"):
            return target
        env = {
            "ALBUM_ID": target["album"]["id"],
            "MEDIA_IDS": "\n".join(media_ids),
        }
        script = """
set albumId to system attribute "ALBUM_ID"
set mediaIdsText to system attribute "MEDIA_IDS"
tell application "Photos"
  set targetAlbum to first album whose id is albumId
  set targetItems to {}
  repeat with mediaId in paragraphs of mediaIdsText
    if mediaId is not "" then
      set end of targetItems to (first media item whose id is mediaId)
    end if
  end repeat
  add targetItems to targetAlbum
  return count of media items of targetAlbum
end tell
"""
        count = int(self._run_applescript(script, env=env))
        result = self._photos_get_album(album_id=target["album"]["id"])
        if result.get("ok"):
            result["album"]["count"] = count
        return result

    def photos_export(
        self,
        *,
        media_ids: list[str],
        destination_dir: str | None,
        use_originals: bool,
    ) -> dict[str, Any]:
        """Export Photos media to files."""
        export_dir = self._prepare_export_dir(destination_dir, self._photos_export_dir)
        before = {path.name for path in export_dir.iterdir()} if export_dir.exists() else set()
        export_dir.mkdir(parents=True, exist_ok=True)
        env = {
            "MEDIA_IDS": "\n".join(media_ids),
            "USE_ORIGINALS": "1" if use_originals else "0",
        }
        script = f"""
set mediaIdsText to system attribute "MEDIA_IDS"
set exportDirText to {_applescript_utf8_file_read("EXPORT_DIR")}
set exportDir to POSIX file exportDirText
set useOriginals to (system attribute "USE_ORIGINALS") is "1"
tell application "Photos"
  set targetItems to {{}}
  repeat with mediaId in paragraphs of mediaIdsText
    if mediaId is not "" then
      set end of targetItems to (first media item whose id is mediaId)
    end if
  end repeat
  if useOriginals then
    export targetItems to exportDir with using originals
  else
    export targetItems to exportDir
  end if
end tell
"""
        self._run_applescript(
            script,
            env=env,
            utf8_files={"EXPORT_DIR": str(export_dir)},
        )
        files = sorted(
            str(path)
            for path in export_dir.iterdir()
            if path.is_file() and path.name not in before
        )
        return {
            "ok": True,
            "destination_dir": str(export_dir),
            "files": files,
            "count": len(files),
        }

    def _resolve_photo_album(
        self,
        *,
        album_id: str | None,
        album_name: str | None,
        album_path: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a Photos album."""
        if album_id:
            return self._photos_get_album(album_id=album_id)
        if album_path:
            result = self._run_jxa_json(
                """
const app = Application("Photos");
const payload = readPayload();
function flattenFolder(folder, parentPath) {
  const path = parentPath ? `${parentPath}/${folder.name()}` : folder.name();
  let rows = folder.albums().map((album) => ({
    id: album.id(),
    name: album.name(),
    path: `${path}/${album.name()}`,
    count: album.mediaItems().length,
    parent_folder_id: folder.id(),
    parent_folder_name: folder.name(),
  }));
  for (const child of folder.folders()) {
    rows = rows.concat(flattenFolder(child, path));
  }
  return rows;
}
let albums = app.albums()
  .filter((album) => !album.parent())
  .map((album) => ({
    id: album.id(),
    name: album.name(),
    path: album.name(),
    count: album.mediaItems().length,
    parent_folder_id: null,
    parent_folder_name: null,
  }));
for (const folder of app.folders()) {
  try {
    if (!folder.parent()) {
      albums = albums.concat(flattenFolder(folder, ""));
    }
  } catch (error) {}
}
const target = albums.find((album) => album.path === payload.album_path);
if (!target) {
  return { ok: false, error: `album not found: ${payload.album_path}` };
}
return { ok: true, album: target };
""",
                payload={"album_path": album_path},
            )
            return result
        if album_name:
            result = self._run_jxa_json(
                """
const app = Application("Photos");
const payload = readPayload();
const album = app.albums.byName(payload.album_name);
if (!album.exists()) {
  return { ok: false, error: `album not found: ${payload.album_name}` };
}
return { ok: true, album: { id: album.id(), name: album.name(), count: album.mediaItems().length } };
""",
                payload={"album_name": album_name},
            )
            return result
        return {"ok": False, "error": "album_id or album_name is required"}

    def _resolve_photo_folder(
        self,
        *,
        folder_id: str | None,
        folder_path: str | None,
    ) -> dict[str, Any]:
        """Resolve a Photos folder."""
        return self._run_jxa_json(
            """
const app = Application("Photos");
const payload = readPayload();
function flattenFolder(folder, parentPath) {
  const path = parentPath ? `${parentPath}/${folder.name()}` : folder.name();
  let rows = [{ id: folder.id(), name: folder.name(), path }];
  for (const child of folder.folders()) {
    rows = rows.concat(flattenFolder(child, path));
  }
  return rows;
}
let folders = [];
for (const folder of app.folders()) {
  try {
    if (!folder.parent()) {
      folders = folders.concat(flattenFolder(folder, ""));
    }
  } catch (error) {}
}
let target = null;
if (payload.folder_id) {
  target = folders.find((row) => row.id === payload.folder_id) || null;
} else if (payload.folder_path) {
  target = folders.find((row) => row.path === payload.folder_path) || null;
}
if (!target) {
  return { ok: false, error: "folder not found" };
}
return { ok: true, folder: target };
""",
            payload={"folder_id": folder_id, "folder_path": folder_path},
        )

    def photos_get_album(
        self,
        *,
        album_id: str | None,
        album_name: str | None,
        album_path: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one album by id or exact name."""
        return self._resolve_photo_album(
            album_id=album_id,
            album_name=album_name,
            album_path=album_path,
        )

    def photos_get_media(self, *, media_ids: list[str]) -> dict[str, Any]:
        """Fetch media metadata by ids."""
        return self._run_jxa_json(
            """
const app = Application("Photos");
const payload = readPayload();
const results = [];
for (const mediaId of payload.media_ids || []) {
  const matches = app.mediaItems.whose({ id: mediaId })();
  if (matches.length === 0) {
    continue;
  }
  const item = matches[0];
  const keywords = item.keywords() || [];
  results.push({
    id: item.id(),
    title: valueOrNull(item.name()),
    filename: valueOrNull(item.filename()),
    description: valueOrNull(item.description()),
    date: iso(item.date()),
    favorite: !!item.favorite(),
    keywords: keywords.map((keyword) => keyword.toString()),
    width: valueOrNull(item.width()),
    height: valueOrNull(item.height()),
    size: valueOrNull(item.size()),
    location: valueOrNull(item.location()),
  });
}
return { ok: true, results, count: results.length };
""",
            payload={"media_ids": media_ids},
        )

    def _photos_get_album(self, *, album_id: str) -> dict[str, Any]:
        """Fetch one album by id."""
        return self._run_jxa_json(
            """
const app = Application("Photos");
const payload = readPayload();
const matches = app.albums.whose({ id: payload.album_id })();
if (matches.length === 0) {
  return { ok: false, error: `album not found: ${payload.album_id}` };
}
const album = matches[0];
const parent = album.parent();
return {
  ok: true,
  album: {
    id: album.id(),
    name: album.name(),
    count: album.mediaItems().length,
    parent_folder_id: parent ? parent.id() : null,
    parent_folder_name: parent ? parent.name() : null,
  },
};
""",
            payload={"album_id": album_id},
        )



def create_photos_tool(bridge: MacOSAppBridge) -> Callable[..., str]:
    """Create photos_tool bound to the bridge."""

    def photos_tool(
        action: str,
        album_id: str | None = None,
        album_name: str | None = None,
        album_path: str | None = None,
        folder_id: str | None = None,
        folder_path: str | None = None,
        parent_folder_id: str | None = None,
        parent_folder_path: str | None = None,
        query: str | None = None,
        start: str | None = None,
        end: str | None = None,
        favorite: bool | None = None,
        sort_by: str | None = None,
        media_ids: list[str] | None = None,
        destination_dir: str | None = None,
        use_originals: bool = True,
        limit: int | None = None,
    ) -> str:

            if action == "catalog":
                return _json_output(bridge.photos_catalog())
            if action == "search":
                return _json_output(
                    bridge.photos_search(
                        album_id=album_id,
                        album_name=album_name,
                        album_path=album_path,
                        folder_id=folder_id,
                        folder_path=folder_path,
                        query=query,
                        start=start,
                        end=end,
                        favorite=favorite,
                        sort_by=sort_by,
                        limit=limit,
                    )
                )
            if action == "get_media":
                if not media_ids:
                    return _error("'media_ids' is required for get_media")
                return _json_output(bridge.photos_get_media(media_ids=media_ids))
            if action == "get_album":
                if not album_id and not album_name and not album_path:
                    return _error("'album_id', 'album_name', or 'album_path' is required for get_album")
                return _json_output(
                    bridge.photos_get_album(
                        album_id=album_id,
                        album_name=album_name,
                        album_path=album_path,
                    )
                )
            if action == "create_album":
                if not album_name:
                    return _error("'album_name' is required for create_album")
                return _json_output(
                    bridge.photos_create_album(
                        album_name=album_name,
                        parent_folder_id=parent_folder_id,
                        parent_folder_path=parent_folder_path,
                    )
                )
            if action == "add_to_album":
                if not media_ids:
                    return _error("'media_ids' is required for add_to_album")
                if not album_id and not album_name and not album_path:
                    return _error("'album_id', 'album_name', or 'album_path' is required for add_to_album")
                return _json_output(
                    bridge.photos_add_to_album(
                        album_id=album_id,
                        album_name=album_name,
                        album_path=album_path,
                        media_ids=media_ids,
                    )
                )
            if action == "export":
                if not media_ids:
                    return _error("'media_ids' is required for export")
                return _json_output(
                    bridge.photos_export(
                        media_ids=media_ids,
                        destination_dir=destination_dir,
                        use_originals=use_originals,
                    )
                )
            return _error(f"unknown action '{action}'")


    return photos_tool
