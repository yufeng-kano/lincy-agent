"""macOS app tool implementation."""

from __future__ import annotations

import base64
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import hashlib
from html import escape as html_escape
import re
from typing import Any

from ....llm.session import llm_session
from ....llm.schema import ContentPart, Message, ToolDefinition, ToolParameter
from .notes_template import (
    _apple_notes_cache_filename,
    _applescript_utf8_file_read,
    _build_note_html,
    _coerce_note_content_kind,
    _coerce_template_mapping,
    _ensure_note_title_html,
    _extract_source_url,
    _html_to_markdown,
    _load_json_file,
    _normalize_markdown,
    _render_note_template_html,
    _write_json_file,
)
from .notes_store import NoteStore, StoredNote
from .runtime import (
    _APPLE_NOTES_CACHE_VERSION,
    _APPLE_NOTES_DEFAULT_SEARCH_LIMIT,
    _APPLE_NOTES_IMAGE_PROMPT,
    _APPLE_NOTES_MAX_NOTE_WORKERS,
    _APPLE_NOTES_SUMMARY_MAX_INPUT_CHARS,
    _APPLE_NOTES_SUMMARY_SYSTEM_PROMPT,
    _DATA_IMAGE_RE,
    _datetime_in_app_tz,
    _error,
    _json_output,
    _parse_local_datetime,
    logger,
)

NOTES_TOOL_DEFINITION = ToolDefinition(
    name="notes_tool",
    description=(
        "Access the user's real macOS Notes data. "
        "'catalog' lists accounts and folder structure, "
        "'search' searches notes by folder/query, "
        "'get' fetches one note by id, "
        "'create' creates a note in a specific folder, "
        "'update' updates a note by id, "
        "'move' moves a note to another folder."
    ),
    parameters={
        "action": ToolParameter(
            type="string",
            description="Action to perform.",
            enum=["catalog", "search", "get", "create", "update", "move"],
        ),
        "account": ToolParameter(
            type="string",
            description="Exact Notes account name, such as 'iCloud'.",
        ),
        "folder_id": ToolParameter(
            type="string",
            description="Folder id. Preferred over folder_path when known.",
        ),
        "folder_path": ToolParameter(
            type="string",
            description="Folder path from catalog, e.g. 'iCloud/待讀'.",
        ),
        "target_folder_id": ToolParameter(
            type="string",
            description="Destination folder id for move.",
        ),
        "target_folder_path": ToolParameter(
            type="string",
            description="Destination folder path for move, e.g. 'iCloud/已讀'.",
        ),
        "note_id": ToolParameter(
            type="string",
            description="Note id. Required for get/update/move.",
        ),
        "query": ToolParameter(
            type="string",
            description="Case-insensitive text query matched against note title, rendered markdown, and cached summary.",
        ),
        "created_after": ToolParameter(
            type="string",
            description="Lower bound for note creation time when searching.",
        ),
        "created_before": ToolParameter(
            type="string",
            description="Upper bound for note creation time when searching.",
        ),
        "modified_after": ToolParameter(
            type="string",
            description="Lower bound for note modification time when searching.",
        ),
        "modified_before": ToolParameter(
            type="string",
            description="Upper bound for note modification time when searching.",
        ),
        "title": ToolParameter(
            type="string",
            description=(
                "Canonical note title. This controls the actual Notes note name. "
                "When title is provided, do not repeat the same text as the first "
                "Markdown heading unless you intentionally want a duplicated visible title."
            ),
        ),
        "body": ToolParameter(
            type="string",
            description="Plain note body content. Required for create/update unless template_markdown is used.",
        ),
        "template_markdown": ToolParameter(
            type="string",
            description=(
                "Optional Markdown template used to render the full note body. "
                "Supports #/##/### headings, paragraphs, bold/italic/code, lists, links, simple tables, and image placeholders. "
                "If title is already provided, body content should usually start at ## instead of repeating the same # heading."
            ),
        ),
        "variables": ToolParameter(
            type="object",
            description=(
                "Optional free-form text variables for template_markdown. "
                "Template placeholders like {title} or {summary} can use any key name."
            ),
            json_schema={"additionalProperties": {"type": "string"}},
        ),
        "images": ToolParameter(
            type="object",
            description=(
                "Optional free-form image variables for template_markdown. "
                "Use either {image_key} or ![alt](image_key) in the template, then map image_key to an absolute file path."
            ),
            json_schema={"additionalProperties": {"type": "string"}},
        ),
        "append": ToolParameter(
            type="boolean",
            description="When true, append body to the existing note instead of replacing it.",
        ),
        "sort_by": ToolParameter(
            type="string",
            description="Sort order for search results.",
            enum=["modified_desc", "modified_asc", "created_desc", "created_asc"],
        ),
        "limit": ToolParameter(
            type="integer",
            description="Maximum number of search results to return. Defaults to 5.",
        ),
        "offset": ToolParameter(
            type="integer",
            description="Zero-based page offset for search results.",
        ),
    },
    required=["action"],
)

class MacOSAppBridge:

    def notes_catalog(self) -> dict[str, Any]:
        """List Notes accounts and the folder tree from the Notes database."""
        with NoteStore.open() as store:
            return {"ok": True, "accounts": store.catalog()}

    def _notes_fetch_body(self, *, note_id: str) -> str:
        """Fetch one note's HTML body through a direct id reference (one Apple Event)."""
        result = self._run_jxa_json(
            """
const payload = readPayload();
return { ok: true, body_html: valueOrNull(Application("Notes").notes.byId(payload.note_id).body()) };
""",
            payload={"note_id": note_id},
            operation="notes.get_body",
            log_details={"note_id": note_id},
        )
        return result.get("body_html") or ""

    def _read_note_cache(self, *, note_id: str) -> dict[str, Any] | None:
        """Load the derived cache entry for one note."""
        return _load_json_file(
            self._apple_notes_cache_dir / _apple_notes_cache_filename(note_id)
        )

    def _read_fresh_note_cache(self, note: StoredNote) -> dict[str, Any] | None:
        """Load the cache entry only when it matches the note's current version.

        modified_at is truncated to whole seconds like Notes scripting, so a
        create followed by an update within one second would look unchanged;
        the full-precision database timestamp tells them apart. Without a
        timestamp freshness cannot be proven, so nothing is fresh.
        """
        if note.modified is None:
            return None
        cached = self._read_note_cache(note_id=note.meta["id"])
        if (
            cached
            and cached.get("cache_version") == _APPLE_NOTES_CACHE_VERSION
            and cached.get("modified_stamp") == note.modified.isoformat()
        ):
            return cached
        return None

    def _write_note_cache(self, *, note_id: str, payload: dict[str, Any]) -> None:
        """Persist the derived cache entry for one note."""
        cache_payload = dict(payload)
        cache_payload["cache_version"] = _APPLE_NOTES_CACHE_VERSION
        _write_json_file(
            self._apple_notes_cache_dir / _apple_notes_cache_filename(note_id),
            cache_payload,
        )

    def _describe_embedded_image(
        self,
        *,
        image_bytes: bytes,
        media_type: str,
        image_index: int,
    ) -> str:
        """Describe one embedded note image with the shared vision agent."""
        if self._vision_agent is None:
            return f"Embedded image {image_index} omitted."
        try:
            description = self._vision_agent.describe(
                [
                    ContentPart(type="text", text=_APPLE_NOTES_IMAGE_PROMPT),
                    ContentPart(
                        type="image",
                        media_type=media_type,
                        data=base64.b64encode(image_bytes).decode("ascii"),
                    ),
                ]
            )
        except Exception as exc:
            logger.warning("apple-notes embedded image vision failed: %s", exc)
            return f"Embedded image {image_index} unavailable."
        return description.strip() or f"Embedded image {image_index}."

    def _render_note_markdown(
        self,
        *,
        note_id: str,
        body_html: str,
        plaintext: str,
    ) -> tuple[str, list[str], bool]:
        """Convert raw Notes HTML into Markdown and replace inline images with text."""
        image_hashes: list[str] = []
        image_counter = 0
        has_images = False

        def replace_data_image(match: re.Match[str]) -> str:
            nonlocal image_counter, has_images
            has_images = True
            image_counter += 1
            src = match.group("src")
            header, _, data_part = src.partition(",")
            media_type = header[5:].split(";", 1)[0] if header.startswith("data:") else "image/png"
            try:
                image_bytes = base64.b64decode(data_part, validate=False)
                image_hashes.append(hashlib.sha256(image_bytes).hexdigest())
            except Exception:
                return f"<p>[Embedded image {image_counter}]</p>"
            description = self._describe_embedded_image(
                image_bytes=image_bytes,
                media_type=media_type,
                image_index=image_counter,
            )
            escaped = html_escape(description).replace("\n", "<br>")
            return f"<p>[Embedded image {image_counter} summary]<br>{escaped}</p>"

        rendered_html = _DATA_IMAGE_RE.sub(replace_data_image, body_html or "")
        markdown = _normalize_markdown(_html_to_markdown(rendered_html)) if rendered_html else ""
        if not markdown:
            markdown = _normalize_markdown(plaintext or "")
        if not markdown:
            markdown = "(empty note)"
        logger.info(
            "apple-notes render note_id=%s has_images=%s image_count=%d markdown_chars=%d",
            note_id,
            has_images,
            len(image_hashes),
            len(markdown),
        )
        return markdown, image_hashes, has_images

    @llm_session("notes_summarizer")
    def _summarize_note_content(self, *, title: str | None, content_markdown: str) -> str:
        """Generate a short search summary for one note."""
        fallback = _normalize_markdown(content_markdown)[:280]
        if self._notes_summarizer is None:
            return fallback
        user_content = (
            f"標題：{title or '(untitled)'}\n"
            f"內容：\n{content_markdown[:_APPLE_NOTES_SUMMARY_MAX_INPUT_CHARS]}"
        )
        try:
            summary = self._notes_summarizer.chat(
                [
                    Message(role="system", content=_APPLE_NOTES_SUMMARY_SYSTEM_PROMPT),
                    Message(role="user", content=user_content),
                ]
            )
        except Exception as exc:
            logger.warning("apple-notes summary failed: %s", exc)
            return fallback
        normalized = _normalize_markdown(summary or "")
        return normalized or fallback

    def _build_note_view(self, note: StoredNote, *, include_summary: bool) -> dict[str, Any]:
        """Build the LLM-facing note payload, using cache when possible.

        Metadata always comes from the database so a cached entry never
        reports a stale folder after a move.
        """
        note_id = note.meta["id"]
        if note.meta["password_protected"]:
            # Locked notes are encrypted at rest and Notes scripting returns an
            # empty body, so only metadata is available.
            return {
                **note.meta,
                "content_markdown": "",
                "content_chars": 0,
                "has_images": False,
                "image_count": 0,
                "image_hashes": [],
                "source_url": None,
                "content_kind": "locked_note",
                "search_summary": "",
            }
        cached = self._read_fresh_note_cache(note)
        if cached:
            if include_summary and not cached.get("search_summary"):
                cached["search_summary"] = self._summarize_note_content(
                    title=note.meta["title"],
                    content_markdown=cached.get("content_markdown", ""),
                )
                self._write_note_cache(note_id=note_id, payload=cached)
            return {**cached, **note.meta}

        body_html = self._notes_fetch_body(note_id=note_id)
        content_markdown, image_hashes, has_images = self._render_note_markdown(
            note_id=note_id,
            body_html=body_html,
            plaintext=note.text or "",
        )
        source_url = _extract_source_url(body_html)
        payload = {
            **note.meta,
            "modified_stamp": note.modified.isoformat() if note.modified else None,
            "content_markdown": content_markdown,
            "content_chars": len(content_markdown),
            "has_images": has_images,
            "image_count": len(image_hashes),
            "image_hashes": image_hashes,
            "source_url": source_url,
            "content_kind": _coerce_note_content_kind(
                source_url=source_url,
                has_images=has_images,
            ),
            "search_summary": None,
        }
        if include_summary:
            payload["search_summary"] = self._summarize_note_content(
                title=payload.get("title"),
                content_markdown=content_markdown,
            )
        self._write_note_cache(note_id=note_id, payload=payload)
        return payload

    def _note_search_entry(self, note: StoredNote) -> dict[str, Any]:
        """Build one search result without calling Notes.app.

        A fresh rich cache entry (written by get) is used as is; otherwise the
        summary comes from the database plain text, because body() can stall
        Notes.app for tens of seconds.
        """
        locked = note.meta["password_protected"]
        if locked or self._read_fresh_note_cache(note) is not None:
            view = self._build_note_view(note, include_summary=True)
            return {**view, "summary_source": "none" if locked else "cache"}
        if note.text is None:
            # The body is not downloaded yet or not decodable, so there is
            # nothing to summarize; say so instead of reporting an empty note.
            return {
                **note.meta,
                "search_summary": "",
                "summary_source": "none",
                "content_chars": 0,
                "has_images": note.image_count > 0,
                "image_count": note.image_count,
                "source_url": note.link_url,
                "content_kind": "unavailable_note",
            }
        stamp = note.modified.isoformat() if note.modified else None
        text = _normalize_markdown(note.text.replace("\ufffc", ""))
        # Plain-text summaries live in their own keys so they never make the
        # rich content_markdown look fresh; a later get upgrades the entry.
        entry = self._read_note_cache(note_id=note.meta["id"]) or {}
        if stamp is None or entry.get("plain_summary_stamp") != stamp:
            entry["plain_summary"] = self._summarize_note_content(
                title=note.meta["title"],
                content_markdown=text,
            )
            entry["plain_summary_stamp"] = stamp
            self._write_note_cache(note_id=note.meta["id"], payload=entry)
        source_url = _extract_source_url(text) or note.link_url
        has_images = note.image_count > 0
        return {
            **note.meta,
            "search_summary": entry["plain_summary"],
            "summary_source": "plain_text",
            "content_chars": len(text),
            "has_images": has_images,
            "image_count": note.image_count,
            "source_url": source_url,
            "content_kind": _coerce_note_content_kind(
                source_url=source_url,
                has_images=has_images,
            ),
        }

    def _note_matches_query(self, note: StoredNote, needle: str) -> bool:
        """Match title and database plain text, then cached Markdown and summary.

        The cache adds text the database lacks, such as vision summaries of
        embedded images, for notes that were rendered before.
        """
        if needle in (note.meta["title"] or "").lower() or needle in (note.text or "").lower():
            return True
        cached = self._read_fresh_note_cache(note)
        return bool(cached) and (
            needle in (cached.get("search_summary") or "").lower()
            or needle in (cached.get("content_markdown") or "").lower()
        )

    def notes_search(
        self,
        *,
        account: str | None,
        folder_id: str | None,
        folder_path: str | None,
        query: str | None,
        created_after: str | None,
        created_before: str | None,
        modified_after: str | None,
        modified_before: str | None,
        sort_by: str | None,
        limit: int | None,
        offset: int | None,
    ) -> dict[str, Any]:
        """Search notes from the Notes database; only the returned page is rendered."""
        bounds = {
            name: _datetime_in_app_tz(_parse_local_datetime(value, field_name=name))
            for name, value in (
                ("created_after", created_after),
                ("created_before", created_before),
                ("modified_after", modified_after),
                ("modified_before", modified_before),
            )
            if value
        }
        needle = (query or "").strip().lower()
        with NoteStore.open() as store:
            folder = None
            if folder_id or folder_path:
                folder = store.find_folder(folder_id=folder_id, folder_path=folder_path)
                if folder is None:
                    return {"ok": False, "error": "notes folder not found"}
            notes = store.notes()

        # A note without a timestamp cannot satisfy a bound on that date.
        candidates = [
            note
            for note in notes
            if (not account or note.meta["account"] == account)
            and (folder is None or note.meta["folder_id"] == folder["id"])
            and ("created_after" not in bounds or (note.created is not None and note.created >= bounds["created_after"]))
            and ("created_before" not in bounds or (note.created is not None and note.created <= bounds["created_before"]))
            and ("modified_after" not in bounds or (note.modified is not None and note.modified >= bounds["modified_after"]))
            and ("modified_before" not in bounds or (note.modified is not None and note.modified <= bounds["modified_before"]))
        ]
        matched = (
            [note for note in candidates if self._note_matches_query(note, needle)]
            if needle
            else candidates
        )
        sort_key, reverse = {
            "modified_asc": (lambda note: note.modified, False),
            "created_desc": (lambda note: note.created, True),
            "created_asc": (lambda note: note.created, False),
        }.get(sort_by or "", (lambda note: note.modified, True))
        # Notes without the sort date go last in both directions.
        matched = sorted((note for note in matched if sort_key(note) is not None), key=sort_key, reverse=reverse) + [
            note for note in matched if sort_key(note) is None
        ]

        safe_offset = max(0, offset or 0)
        safe_limit = max(
            1,
            min(limit or _APPLE_NOTES_DEFAULT_SEARCH_LIMIT, self._max_search_results),
        )
        page_notes = matched[safe_offset : safe_offset + safe_limit]
        workers = min(_APPLE_NOTES_MAX_NOTE_WORKERS, len(page_notes))
        if workers <= 1:
            page = [self._note_search_entry(note) for note in page_notes]
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                page = list(executor.map(self._note_search_entry, page_notes))
        results = [
            {
                "id": note["id"],
                "title": note.get("title"),
                "summary": note.get("search_summary") or "",
                "summary_source": note["summary_source"],
                "created_at": note.get("created_at"),
                "modified_at": note.get("modified_at"),
                "account": note.get("account"),
                "folder_id": note.get("folder_id"),
                "folder_path": note.get("folder_path"),
                "password_protected": note.get("password_protected", False),
                "content_kind": note.get("content_kind"),
                "has_images": note.get("has_images", False),
                "image_count": note.get("image_count", 0),
                "source_url": note.get("source_url"),
                "content_chars": note.get("content_chars", 0),
            }
            for note in page
        ]
        logger.info(
            "apple-notes search folder=%s query_chars=%d scanned=%d matched=%d returned=%d offset=%d limit=%d",
            folder_path or folder_id or account or "*",
            len(query or ""),
            len(candidates),
            len(matched),
            len(results),
            safe_offset,
            safe_limit,
        )
        return {
            "ok": True,
            "results": results,
            "count": len(results),
            "total_matches": len(matched),
            "offset": safe_offset,
            "limit": safe_limit,
            "has_more": safe_offset + safe_limit < len(matched),
        }

    def _load_note(self, note_id: str) -> StoredNote | dict[str, Any]:
        """Look up one note in the database, or return the tool error payload."""
        with NoteStore.open() as store:
            note = store.note(note_id)
        if note is None:
            return {"ok": False, "error": f"note not found: {note_id}"}
        return note

    def notes_get(self, *, note_id: str) -> dict[str, Any]:
        """Fetch one note by id and return rendered Markdown content."""
        note = self._load_note(note_id)
        if isinstance(note, dict):
            return note
        view = self._build_note_view(note, include_summary=False)
        return {
            "ok": True,
            "note": {
                "id": view["id"],
                "title": view.get("title"),
                "created_at": view.get("created_at"),
                "modified_at": view.get("modified_at"),
                "shared": view.get("shared", False),
                "password_protected": view.get("password_protected", False),
                "account": view.get("account"),
                "folder_id": view.get("folder_id"),
                "folder_path": view.get("folder_path"),
                "content_markdown": view.get("content_markdown", ""),
                "content_chars": view.get("content_chars", 0),
                "content_kind": view.get("content_kind"),
                "has_images": view.get("has_images", False),
                "image_count": view.get("image_count", 0),
                "source_url": view.get("source_url"),
            },
        }

    def notes_create(
        self,
        *,
        folder_id: str | None,
        folder_path: str | None,
        title: str | None,
        body: str | None,
        template_markdown: str | None,
        variables: dict[str, str] | None,
        images: dict[str, str] | None,
    ) -> dict[str, Any]:
        """Create a note."""
        target = self._resolve_note_folder(folder_id=folder_id, folder_path=folder_path)
        if not target.get("ok"):
            return target
        variables = dict(variables or {})
        if title is not None and "title" not in variables:
            variables["title"] = title
        if template_markdown is not None:
            note_body = _render_note_template_html(
                template_markdown=template_markdown,
                variables=variables,
                images=images or {},
                allowed_paths=self._allowed_paths,
                base_dir=self._base_dir,
            )
            note_body = _ensure_note_title_html(note_body, title)
        else:
            note_body = _build_note_html(title, body or "")
        env = {"FOLDER_ID": target["folder_id"]}
        script = f"""
set folderId to system attribute "FOLDER_ID"
set noteBody to {_applescript_utf8_file_read("NOTE_BODY")}
tell application "Notes"
  set newNote to make new note at folder id folderId with properties {{body:noteBody}}
  return id of newNote
end tell
"""
        note_id = self._run_applescript(
            script,
            env=env,
            utf8_files={"NOTE_BODY": note_body},
            operation="notes.create",
            log_details={
                "folder_id": target["folder_id"],
                "folder_path": target["folder_path"],
                "title": title or "",
                "body": body or "",
                "template_markdown": template_markdown or "",
                "variables": variables,
                "images": images or {},
            },
        )
        return self.notes_get(note_id=note_id)

    def notes_update(
        self,
        *,
        note_id: str,
        title: str | None,
        body: str | None,
        template_markdown: str | None,
        variables: dict[str, str] | None,
        images: dict[str, str] | None,
        append: bool,
    ) -> dict[str, Any]:
        """Update a note."""
        current = self._load_note(note_id)
        if isinstance(current, dict):
            return current
        if current.meta["password_protected"]:
            return {"ok": False, "error": f"note is password-protected: {note_id}"}
        variables = dict(variables or {})
        if title is not None and "title" not in variables:
            variables["title"] = title
        if template_markdown is not None:
            payload = _render_note_template_html(
                template_markdown=template_markdown,
                variables=variables,
                images=images or {},
                allowed_paths=self._allowed_paths,
                base_dir=self._base_dir,
            )
            if not append:
                payload = _ensure_note_title_html(payload, title)
        else:
            payload = _build_note_html(title, body or "")
        # Appending inside AppleScript reads and writes the body in one call.
        new_body = "(body of targetNote) & noteBody" if append else "noteBody"
        env = {"NOTE_ID": note_id}
        script = f"""
set noteId to system attribute "NOTE_ID"
set noteBody to {_applescript_utf8_file_read("NOTE_BODY")}
tell application "Notes"
  set targetNote to note id noteId
  set body of targetNote to {new_body}
  return id of targetNote
end tell
"""
        updated_id = self._run_applescript(
            script,
            env=env,
            utf8_files={"NOTE_BODY": payload},
            operation="notes.update",
            log_details={
                "note_id": note_id,
                "title": title or "",
                "body": body or "",
                "template_markdown": template_markdown or "",
                "variables": variables,
                "images": images or {},
                "append": append,
            },
        )
        return self.notes_get(note_id=updated_id)

    def notes_move(
        self,
        *,
        note_id: str,
        target_folder_id: str | None,
        target_folder_path: str | None,
    ) -> dict[str, Any]:
        """Move a note to another folder."""
        current = self._load_note(note_id)
        if isinstance(current, dict):
            return current
        target = self._resolve_note_folder(
            folder_id=target_folder_id,
            folder_path=target_folder_path,
        )
        if not target.get("ok"):
            return target
        env = {
            "NOTE_ID": note_id,
            "TARGET_FOLDER_ID": target["folder_id"],
        }
        script = """
set noteId to system attribute "NOTE_ID"
set targetFolderId to system attribute "TARGET_FOLDER_ID"
tell application "Notes"
  set targetNote to note id noteId
  move targetNote to folder id targetFolderId
  return id of targetNote
end tell
"""
        moved_id = self._run_applescript(
            script,
            env=env,
            operation="notes.move",
            log_details={
                "note_id": note_id,
                "target_folder_id": target["folder_id"],
                "target_folder_path": target["folder_path"],
            },
        )
        return self.notes_get(note_id=moved_id)

    def _resolve_note_folder(
        self,
        *,
        folder_id: str | None,
        folder_path: str | None,
    ) -> dict[str, Any]:
        """Resolve a Notes folder from the Notes database."""
        with NoteStore.open() as store:
            folder = store.find_folder(folder_id=folder_id, folder_path=folder_path)
        if folder is None:
            return {"ok": False, "error": "notes folder not found"}
        return {
            "ok": True,
            "folder_id": folder["id"],
            "folder_path": folder["path"],
            "account": folder["account"],
            "folder_name": folder["name"],
        }


def create_notes_tool(bridge: MacOSAppBridge) -> Callable[..., str]:
    """Create notes_tool bound to the bridge."""

    def notes_tool(
        action: str,
        account: str | None = None,
        folder_id: str | None = None,
        folder_path: str | None = None,
        target_folder_id: str | None = None,
        target_folder_path: str | None = None,
        note_id: str | None = None,
        query: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        modified_after: str | None = None,
        modified_before: str | None = None,
        title: str | None = None,
        body: str | None = None,
        template_markdown: str | None = None,
        variables: dict[str, Any] | None = None,
        images: dict[str, Any] | None = None,
        append: bool = False,
        sort_by: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> str:

            text_variables = _coerce_template_mapping(variables, field_name="variables")
            image_variables = _coerce_template_mapping(images, field_name="images")
            if action == "catalog":
                return _json_output(bridge.notes_catalog())
            if action == "search":
                return _json_output(
                    bridge.notes_search(
                        account=account,
                        folder_id=folder_id,
                        folder_path=folder_path,
                        query=query,
                        created_after=created_after,
                        created_before=created_before,
                        modified_after=modified_after,
                        modified_before=modified_before,
                        sort_by=sort_by,
                        limit=limit,
                        offset=offset,
                    )
                )
            if action == "get":
                if not note_id:
                    return _error("'note_id' is required for get")
                return _json_output(bridge.notes_get(note_id=note_id))
            if action == "create":
                if body is None and template_markdown is None:
                    return _error("'body' or 'template_markdown' is required for create")
                if not folder_id and not folder_path:
                    return _error("'folder_id' or 'folder_path' is required for create")
                return _json_output(
                    bridge.notes_create(
                        folder_id=folder_id,
                        folder_path=folder_path,
                        title=title,
                        body=body,
                        template_markdown=template_markdown,
                        variables=text_variables,
                        images=image_variables,
                    )
                )
            if action == "update":
                if not note_id:
                    return _error("'note_id' is required for update")
                if body is None and template_markdown is None:
                    return _error("'body' or 'template_markdown' is required for update")
                return _json_output(
                    bridge.notes_update(
                        note_id=note_id,
                        title=title,
                        body=body,
                        template_markdown=template_markdown,
                        variables=text_variables,
                        images=image_variables,
                        append=append,
                    )
                )
            if action == "move":
                if not note_id:
                    return _error("'note_id' is required for move")
                if not target_folder_id and not target_folder_path:
                    return _error("'target_folder_id' or 'target_folder_path' is required for move")
                return _json_output(
                    bridge.notes_move(
                        note_id=note_id,
                        target_folder_id=target_folder_id,
                        target_folder_path=target_folder_path,
                    )
                )
            return _error(f"unknown action '{action}'")


    return notes_tool
