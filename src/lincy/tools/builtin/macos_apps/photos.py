"""photos_tool: the macOS Photos library through PhotoKit.

PhotoKit reads the library in-process instead of sending Apple Events to
Photos.app, which cost about a second per event on the production Mac. Text
metadata PhotoKit does not expose comes from `photos_db`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Any

from Foundation import NSURL, NSDate, NSPredicate, NSSortDescriptor
import Photos

from ....llm.schema import ToolDefinition, ToolParameter
from .photos_db import asset_text_metadata, match_query_uuids, open_library_database
from .runtime import (
    _datetime_in_app_tz,
    _datetime_to_app_iso,
    _error,
    _json_output,
    _parse_local_datetime,
)

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


_STATUS_NAMES = {
    Photos.PHAuthorizationStatusNotDetermined: "not determined",
    Photos.PHAuthorizationStatusRestricted: "restricted",
    Photos.PHAuthorizationStatusDenied: "denied",
    Photos.PHAuthorizationStatusLimited: "limited",
}
# Every asset localIdentifier is "<UUID>/L0/001"; Photos.sqlite rows only
# carry the UUID, so this suffix maps database matches back to PhotoKit ids.
_ASSET_ID_SUFFIX = "/L0/001"
# Export slots: each slot writes the first resource type present. A Live
# Photo is a still plus a paired video, and a RAW+JPEG import keeps the second
# file as AlternatePhoto, so one asset can export several files.
_ORIGINAL_RESOURCE_TYPES = (
    (Photos.PHAssetResourceTypePhoto,),
    (Photos.PHAssetResourceTypeAlternatePhoto,),
    (Photos.PHAssetResourceTypePairedVideo,),
    (Photos.PHAssetResourceTypeVideo,),
    (Photos.PHAssetResourceTypeAudio,),
)
# Full-size renders exist only for edited assets; otherwise the original is
# already the current version. An edit renders one photo, so the RAW/JPEG
# alternate has no current version.
_CURRENT_RESOURCE_TYPES = (
    (Photos.PHAssetResourceTypeFullSizePhoto, Photos.PHAssetResourceTypePhoto),
    (Photos.PHAssetResourceTypeFullSizePairedVideo, Photos.PHAssetResourceTypePairedVideo),
    (Photos.PHAssetResourceTypeFullSizeVideo, Photos.PHAssetResourceTypeVideo),
    (Photos.PHAssetResourceTypeAudio,),
)


def _photos_access_problem() -> str | None:
    status = Photos.PHPhotoLibrary.authorizationStatusForAccessLevel_(Photos.PHAccessLevelReadWrite)
    if status == Photos.PHAuthorizationStatusAuthorized:
        return None
    return (
        f"Photos full access is not granted (status: {_STATUS_NAMES.get(status, status)}). "
        "Grant it in System Settings > Privacy & Security > Photos, "
        "choosing Full Access for the app that runs lincy (e.g. Terminal)."
    )


def _require_photos_access() -> None:
    # The tool never requests authorization itself: a prompt raised from a
    # background agent process would be attributed to whatever app launched it.
    problem = _photos_access_problem()
    if problem:
        raise PermissionError(problem)


def photos_permission_problems() -> list[str]:
    """Return human-readable problems that stop photos_tool from working."""
    problems = []
    problem = _photos_access_problem()
    if problem:
        problems.append(problem)
    try:
        open_library_database().close()
    except PermissionError as exc:
        problems.append(str(exc))
    return problems


def _all_sources_fetch_options() -> Any:
    # PhotoKit's default fetch skips iCloud Shared Album and iTunes-synced assets.
    options = Photos.PHFetchOptions.alloc().init()
    options.setIncludeAssetSourceTypes_(
        Photos.PHAssetSourceTypeUserLibrary
        | Photos.PHAssetSourceTypeCloudShared
        | Photos.PHAssetSourceTypeiTunesSynced
    )
    return options


def _fetched(result: Any) -> list[Any]:
    # PHFetchResult does not support Python iteration under PyObjC.
    return [result.objectAtIndex_(index) for index in range(result.count())]


def _iter_collections(folder: Any = None, parent_path: str = "") -> Iterator[tuple[str, Any, Any]]:
    """Yield (path, collection, parent folder) for every user album and folder, depth first."""
    children = (
        Photos.PHCollectionList.fetchTopLevelUserCollectionsWithOptions_(None)
        if folder is None
        else Photos.PHCollection.fetchCollectionsInCollectionList_options_(folder, None)
    )
    for child in _fetched(children):
        title = child.localizedTitle()
        path = f"{parent_path}/{title}" if parent_path else title
        yield path, child, folder
        if child.canContainCollections():
            yield from _iter_collections(child, path)


def _find_album(
    album_id: str | None,
    album_name: str | None,
    album_path: str | None,
) -> tuple[str, Any, Any] | None:
    for path, collection, parent in _iter_collections():
        if collection.canContainCollections():
            continue
        if album_id:
            matched = collection.localIdentifier() == album_id
        elif album_path:
            matched = path == album_path
        else:
            matched = collection.localizedTitle() == album_name
        if matched:
            return path, collection, parent
    return None


def _find_folder(folder_id: str | None, folder_path: str | None) -> tuple[str, Any] | None:
    for path, collection, _ in _iter_collections():
        if collection.canContainCollections() and (
            collection.localIdentifier() == folder_id or path == folder_path
        ):
            return path, collection
    return None


def _album_row(path: str, album: Any, parent: Any) -> dict[str, Any]:
    return {
        "id": album.localIdentifier(),
        "name": album.localizedTitle(),
        "path": path,
        "count": Photos.PHAsset.fetchAssetsInAssetCollection_options_(album, None).count(),
        "parent_folder_id": parent.localIdentifier() if parent else None,
        "parent_folder_name": parent.localizedTitle() if parent else None,
    }


def _asset_uuid(asset: Any) -> str:
    return asset.localIdentifier().split("/", 1)[0]


def _asset_rows(assets: list[Any]) -> list[dict[str, Any]]:
    text = asset_text_metadata([_asset_uuid(asset) for asset in assets])
    rows = []
    for asset in assets:
        created = asset.creationDate()
        location = asset.location()
        # Photos.sqlite can lack a row PhotoKit already returns (an asset
        # still importing or syncing); its text metadata is then just unknown.
        metadata = text.get(_asset_uuid(asset)) or {
            "title": None, "filename": None, "description": None, "keywords": [], "size": None,
        }
        rows.append(
            {
                "id": asset.localIdentifier(),
                **metadata,
                "date": (
                    _datetime_to_app_iso(
                        datetime.fromtimestamp(created.timeIntervalSince1970(), tz=timezone.utc)
                    )
                    if created
                    else None
                ),
                "favorite": bool(asset.isFavorite()),
                "width": asset.pixelWidth(),
                "height": asset.pixelHeight(),
                "location": list(location.coordinate()) if location else None,
            }
        )
    return rows


def _search_predicate(
    *,
    start: datetime | None,
    end: datetime | None,
    favorite: bool | None,
    ids: set[str] | None,
) -> Any:
    clauses: list[str] = []
    arguments: list[Any] = []
    if start is not None:
        clauses.append("creationDate >= %@")
        arguments.append(NSDate.dateWithTimeIntervalSince1970_(start.timestamp()))
    if end is not None:
        clauses.append("creationDate <= %@")
        arguments.append(NSDate.dateWithTimeIntervalSince1970_(end.timestamp()))
    if favorite is not None:
        clauses.append("favorite == %@")
        arguments.append(favorite)
    if ids is not None:
        clauses.append("localIdentifier IN %@")
        arguments.append(sorted(ids))
    if not clauses:
        return None
    return NSPredicate.predicateWithFormat_argumentArray_(" AND ".join(clauses), arguments)


def _pick_resources(resources: list[Any], use_originals: bool) -> list[Any]:
    """Return the resources to export, at most one per slot."""
    by_type: dict[int, Any] = {}
    for resource in resources:
        by_type.setdefault(resource.type(), resource)
    picked = []
    for slot in _ORIGINAL_RESOURCE_TYPES if use_originals else _CURRENT_RESOURCE_TYPES:
        present = [by_type[resource_type] for resource_type in slot if resource_type in by_type]
        if present:
            picked.append(present[0])
    return picked


def _export_filename(resources: list[Any], chosen: Any) -> str:
    """Name the file after the original import name, with the chosen resource's extension.

    Edited renders are stored as e.g. "FullSizeRender.jpeg", which says
    nothing about which photo it is.
    """
    originals = _pick_resources(resources, use_originals=True)
    original = originals[0] if originals else chosen
    return Path(original.originalFilename()).stem + Path(chosen.originalFilename()).suffix


class MacOSAppBridge:

    def photos_catalog(self) -> dict[str, Any]:
        """List Photos folders and albums."""
        _require_photos_access()
        folders_by_id: dict[str, dict[str, Any]] = {}
        folders: list[dict[str, Any]] = []
        albums: list[dict[str, Any]] = []
        for path, collection, parent in _iter_collections():
            parent_row = folders_by_id[parent.localIdentifier()] if parent else None
            if collection.canContainCollections():
                row = {
                    "id": collection.localIdentifier(),
                    "name": collection.localizedTitle(),
                    "path": path,
                    "albums": [],
                    "children": [],
                }
                folders_by_id[row["id"]] = row
                (parent_row["children"] if parent_row else folders).append(row)
            else:
                (parent_row["albums"] if parent_row else albums).append(
                    _album_row(path, collection, parent)
                )
        return {"ok": True, "folders": folders, "albums": albums}

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
        """Search the Photos library; scope, dates and favorite are filtered by PhotoKit."""
        _require_photos_access()
        # Date sorts happen inside the fetch; only a filename sort needs every
        # matching asset loaded, so only that one requires a narrower scope.
        if (
            sort_by == "filename_asc"
            and not any([album_id, album_name, album_path, folder_id, folder_path, query, start, end])
            and favorite is None
        ):
            return {
                "ok": False,
                "error": (
                    "sorting the entire Photos library by filename requires a narrower scope; "
                    "provide album_path, folder_path, query, start/end, or favorite"
                ),
            }
        limit = max(1, min(limit or self._max_search_results, self._max_search_results))
        start_at = _datetime_in_app_tz(_parse_local_datetime(start, field_name="start")) if start else None
        end_at = _datetime_in_app_tz(_parse_local_datetime(end, field_name="end")) if end else None

        album = None
        ids: set[str] | None = None
        scope_type, scope_name = "library", None
        if album_id or album_name or album_path:
            found_album = _find_album(album_id, album_name, album_path)
            if found_album is None:
                return {"ok": False, "error": "album not found"}
            album = found_album[1]
            scope_type, scope_name = "album", album.localizedTitle()
        elif folder_id or folder_path:
            found_folder = _find_folder(folder_id, folder_path)
            if found_folder is None:
                return {"ok": False, "error": "folder not found"}
            scope_type, scope_name = "folder", found_folder[0]
            # PhotoKit cannot fetch assets of a folder subtree directly, so
            # collect member ids per album and filter/sort them in one fetch.
            ids = {
                asset.localIdentifier()
                for path, collection, _ in _iter_collections(found_folder[1], found_folder[0])
                if not collection.canContainCollections()
                for asset in _fetched(Photos.PHAsset.fetchAssetsInAssetCollection_options_(collection, None))
            }
        if query:
            matched = {uuid + _ASSET_ID_SUFFIX for uuid in match_query_uuids(query)}
            ids = matched if ids is None else ids & matched

        options = _all_sources_fetch_options()
        options.setPredicate_(_search_predicate(start=start_at, end=end_at, favorite=favorite, ids=ids))
        options.setSortDescriptors_(
            [NSSortDescriptor.sortDescriptorWithKey_ascending_("creationDate", sort_by == "date_asc")]
        )
        if sort_by != "filename_asc":
            options.setFetchLimit_(limit)
        fetched = (
            Photos.PHAsset.fetchAssetsInAssetCollection_options_(album, options)
            if album is not None
            else Photos.PHAsset.fetchAssetsWithOptions_(options)
        )
        results = _asset_rows(_fetched(fetched))
        if sort_by == "filename_asc":
            results.sort(key=lambda row: (row["filename"] or "").casefold())
            del results[limit:]
        for row in results:
            row["scope_type"] = scope_type
            row["scope_name"] = scope_name
        return {"ok": True, "results": results, "count": len(results)}

    def photos_get_album(
        self,
        *,
        album_id: str | None,
        album_name: str | None,
        album_path: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one album by id, path or exact name."""
        _require_photos_access()
        found = _find_album(album_id, album_name, album_path)
        if found is None:
            return {"ok": False, "error": f"album not found: {album_id or album_path or album_name}"}
        return {"ok": True, "album": _album_row(*found)}

    def photos_get_media(self, *, media_ids: list[str]) -> dict[str, Any]:
        """Fetch media metadata by ids, in the requested order; unknown ids are skipped."""
        _require_photos_access()
        fetched = _fetched(
            Photos.PHAsset.fetchAssetsWithLocalIdentifiers_options_(media_ids, _all_sources_fetch_options())
        )
        by_id = {row["id"]: row for row in _asset_rows(fetched)}
        results = [by_id[media_id] for media_id in media_ids if media_id in by_id]
        return {"ok": True, "results": results, "count": len(results)}

    def photos_export(
        self,
        *,
        media_ids: list[str],
        destination_dir: str | None,
        use_originals: bool,
    ) -> dict[str, Any]:
        """Export Photos media to files through PHAssetResourceManager."""
        _require_photos_access()
        export_dir = self._prepare_export_dir(destination_dir, self._photos_export_dir)
        assets = {
            asset.localIdentifier(): asset
            for asset in _fetched(
                Photos.PHAsset.fetchAssetsWithLocalIdentifiers_options_(media_ids, _all_sources_fetch_options())
            )
        }
        missing = [media_id for media_id in media_ids if media_id not in assets]
        if missing:
            return {"ok": False, "error": f"media not found: {', '.join(missing)}"}
        export_dir.mkdir(parents=True, exist_ok=True)

        manager = Photos.PHAssetResourceManager.defaultManager()
        options = Photos.PHAssetResourceRequestOptions.alloc().init()
        # Optimized-storage libraries keep many originals only in iCloud.
        options.setNetworkAccessAllowed_(True)
        files: list[str] = []
        failed: list[dict[str, str]] = []
        for media_id in media_ids:
            resources = list(Photos.PHAssetResource.assetResourcesForAsset_(assets[media_id]))
            chosen = _pick_resources(resources, use_originals)
            if not chosen:
                failed.append({"id": media_id, "error": "asset has no exportable photo/video resource"})
                continue
            # Live Photos and RAW+JPEG pairs export one file per resource.
            for resource in chosen:
                name = _export_filename(resources, resource)
                target = export_dir / name
                suffix_index = 1
                while target.exists():
                    target = export_dir / f"{Path(name).stem} ({suffix_index}){Path(name).suffix}"
                    suffix_index += 1

                # The completion handler runs on a PhotoKit queue, so a plain
                # Event works without a run loop on the calling thread. It binds
                # this iteration's objects because a timed-out write can still
                # complete after the loop has moved on.
                done = threading.Event()
                errors: list[str] = []

                def finished(error: Any, done: threading.Event = done, errors: list[str] = errors) -> None:
                    if error is not None:
                        errors.append(str(error.localizedDescription()))
                    done.set()

                manager.writeDataForAssetResource_toFile_options_completionHandler_(
                    resource, NSURL.fileURLWithPath_(str(target)), options, finished
                )
                if not done.wait(self._timeout_seconds):
                    failed.append(
                        {
                            "id": media_id,
                            "error": (
                                f"timed out after {self._timeout_seconds:.0f}s writing {target.name}; "
                                "the iCloud download may still finish in the background"
                            ),
                        }
                    )
                elif errors:
                    failed.append(
                        {"id": media_id, "error": f"could not export {target.name} (iCloud download or write failed): {errors[0]}"}
                    )
                else:
                    files.append(str(target))
        if failed and not files:
            return {"ok": False, "error": "; ".join(f"{row['id']}: {row['error']}" for row in failed)}
        return {
            "ok": True,
            "destination_dir": str(export_dir),
            "files": files,
            "count": len(files),
            "failed": failed,
        }

    def photos_create_album(
        self,
        *,
        album_name: str,
        parent_folder_id: str | None,
        parent_folder_path: str | None,
    ) -> dict[str, Any]:
        """Create a Photos album, optionally inside a folder."""
        _require_photos_access()
        parent = None
        if parent_folder_id or parent_folder_path:
            found_folder = _find_folder(parent_folder_id, parent_folder_path)
            if found_folder is None:
                return {"ok": False, "error": "folder not found"}
            parent = found_folder[1]
        created: list[str] = []

        def changes() -> None:
            request = Photos.PHAssetCollectionChangeRequest.creationRequestForAssetCollectionWithTitle_(album_name)
            placeholder = request.placeholderForCreatedAssetCollection()
            created.append(placeholder.localIdentifier())
            if parent is not None:
                Photos.PHCollectionListChangeRequest.changeRequestForCollectionList_(parent).addChildCollections_(
                    [placeholder]
                )

        ok, error = Photos.PHPhotoLibrary.sharedPhotoLibrary().performChangesAndWait_error_(changes, None)
        if not ok:
            return {"ok": False, "error": f"Photos could not create album: {error.localizedDescription()}"}
        return self.photos_get_album(album_id=created[0], album_name=None)

    def photos_add_to_album(
        self,
        *,
        album_id: str | None,
        album_name: str | None,
        album_path: str | None,
        media_ids: list[str],
    ) -> dict[str, Any]:
        """Add media items to an album."""
        _require_photos_access()
        found = _find_album(album_id, album_name, album_path)
        if found is None:
            return {"ok": False, "error": f"album not found: {album_id or album_path or album_name}"}
        album = found[1]
        assets = _fetched(
            Photos.PHAsset.fetchAssetsWithLocalIdentifiers_options_(media_ids, _all_sources_fetch_options())
        )
        found_ids = {asset.localIdentifier() for asset in assets}
        missing = [media_id for media_id in media_ids if media_id not in found_ids]
        if missing:
            return {"ok": False, "error": f"media not found: {', '.join(missing)}"}

        def changes() -> None:
            Photos.PHAssetCollectionChangeRequest.changeRequestForAssetCollection_(album).addAssets_(assets)

        ok, error = Photos.PHPhotoLibrary.sharedPhotoLibrary().performChangesAndWait_error_(changes, None)
        if not ok:
            return {"ok": False, "error": f"Photos could not add media to album: {error.localizedDescription()}"}
        return {"ok": True, "album": _album_row(*found)}


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
