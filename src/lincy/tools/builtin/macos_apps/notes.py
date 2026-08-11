"""macOS app tool implementation."""

from __future__ import annotations

import base64
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import hashlib
from html import escape as html_escape
import re
from typing import Any

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
from .runtime import (
    _APPLE_NOTES_CACHE_VERSION,
    _APPLE_NOTES_DEFAULT_SEARCH_LIMIT,
    _APPLE_NOTES_IMAGE_PROMPT,
    _APPLE_NOTES_MAX_NOTE_WORKERS,
    _APPLE_NOTES_SUMMARY_MAX_INPUT_CHARS,
    _APPLE_NOTES_SUMMARY_SYSTEM_PROMPT,
    _DATA_IMAGE_RE,
    _error,
    _json_output,
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
        """List Notes accounts and folders."""
        script = """
const app = Application("Notes");
function walkFolder(folder, accountName) {
  const path = `${accountName}/${folder.name()}`;
  return {
    id: folder.id(),
    name: folder.name(),
    account: accountName,
    path,
    children: folder.folders().map((child) => walkChildFolder(child, path, accountName)),
  };
}
function walkChildFolder(folder, parentPath, accountName) {
  const path = `${parentPath}/${folder.name()}`;
  return {
    id: folder.id(),
    name: folder.name(),
    account: accountName,
    path,
    children: folder.folders().map((child) => walkChildFolder(child, path, accountName)),
  };
}
const accounts = app.accounts().map((account) => ({
  id: account.id(),
  name: account.name(),
  folders: account.folders().map((folder) => walkFolder(folder, account.name())),
}));
return { ok: true, accounts };
"""
        return self._run_jxa_json(
            script,
            operation="notes.catalog",
        )

    def _notes_list_candidates(
        self,
        *,
        account: str | None,
        folder_id: str | None,
        folder_path: str | None,
        created_after: str | None,
        created_before: str | None,
        modified_after: str | None,
        modified_before: str | None,
        sort_by: str | None,
    ) -> dict[str, Any]:
        """List note metadata without loading large note bodies."""
        script = f"""
const app = Application("Notes");
const payload = readPayload();
const scanLimit = clampLimit(payload.scan_limit, {self._max_search_results});
const createdAfter = payload.created_after ? new Date(payload.created_after) : null;
const createdBefore = payload.created_before ? new Date(payload.created_before) : null;
const modifiedAfter = payload.modified_after ? new Date(payload.modified_after) : null;
const modifiedBefore = payload.modified_before ? new Date(payload.modified_before) : null;
function flattenFolders(folder, accountName, parentPath) {{
  const path = parentPath ? `${{parentPath}}/${{folder.name()}}` : `${{accountName}}/${{folder.name()}}`;
  const entry = {{ id: folder.id(), name: folder.name(), account: accountName, path, notes: folder.notes() }};
  let rows = [entry];
  for (const child of folder.folders()) {{
    rows = rows.concat(flattenFolders(child, accountName, path));
  }}
  return rows;
}}
let folders = [];
for (const accountRow of app.accounts()) {{
  if (payload.account && accountRow.name() !== payload.account) {{
    continue;
  }}
  for (const folder of accountRow.folders()) {{
    folders = folders.concat(flattenFolders(folder, accountRow.name(), ""));
  }}
}}
if (payload.folder_id) {{
  folders = folders.filter((row) => row.id === payload.folder_id);
}}
if (payload.folder_path) {{
  folders = folders.filter((row) => row.path === payload.folder_path);
}}
const results = [];
for (const row of folders) {{
  for (const note of row.notes) {{
    const item = {{
      id: note.id(),
      title: note.name(),
      created_at: iso(note.creationDate()),
      modified_at: iso(note.modificationDate()),
      shared: !!note.shared(),
      password_protected: !!note.passwordProtected(),
      account: row.account,
      folder_id: row.id,
      folder_path: row.path,
    }};
    if (createdAfter && (!item.created_at || new Date(item.created_at) < createdAfter)) {{
      continue;
    }}
    if (createdBefore && (!item.created_at || new Date(item.created_at) > createdBefore)) {{
      continue;
    }}
    if (modifiedAfter && (!item.modified_at || new Date(item.modified_at) < modifiedAfter)) {{
      continue;
    }}
    if (modifiedBefore && (!item.modified_at || new Date(item.modified_at) > modifiedBefore)) {{
      continue;
    }}
    results.push(item);
    if (results.length >= scanLimit) {{
      break;
    }}
  }}
  if (results.length >= scanLimit) {{
    break;
  }}
}}
if (payload.sort_by === "modified_asc") {{
  results.sort((a, b) => compareIsoAsc(a.modified_at, b.modified_at));
}} else if (payload.sort_by === "created_desc") {{
  results.sort((a, b) => compareIsoDesc(a.created_at, b.created_at));
}} else if (payload.sort_by === "created_asc") {{
  results.sort((a, b) => compareIsoAsc(a.created_at, b.created_at));
}} else {{
  results.sort((a, b) => compareIsoDesc(a.modified_at, b.modified_at));
}}
return {{ ok: true, results, count: results.length }};
        """
        return self._run_jxa_json(
            script,
            payload={
                "account": account,
                "folder_id": folder_id,
                "folder_path": folder_path,
                "created_after": created_after,
                "created_before": created_before,
                "modified_after": modified_after,
                "modified_before": modified_before,
                "sort_by": sort_by,
                "scan_limit": self._max_search_results,
            },
            operation="notes.list_candidates",
            log_details={
                "account": account,
                "folder_id": folder_id,
                "folder_path": folder_path,
                "created_after": created_after,
                "created_before": created_before,
                "modified_after": modified_after,
                "modified_before": modified_before,
                "sort_by": sort_by,
                "scan_limit": self._max_search_results,
            },
        )

    def _notes_get_raw(self, *, note_id: str) -> dict[str, Any]:
        """Fetch one note by id with raw HTML/plaintext."""
        return self._run_jxa_json(
            """
const app = Application("Notes");
const payload = readPayload();
function buildFolderPath(folder) {
  const parts = [folder.name()];
  let container = null;
  try {
    container = folder.container();
  } catch (error) {
    container = null;
  }
  while (container) {
    try {
      parts.unshift(container.name());
      container = container.container();
    } catch (error) {
      break;
    }
  }
  return parts.join("/");
}
function resolveAccountName(folder) {
  let container = null;
  let accountName = null;
  try {
    container = folder.container();
  } catch (error) {
    container = null;
  }
  while (container) {
    try {
      accountName = container.name();
      container = container.container();
    } catch (error) {
      break;
    }
  }
  return accountName;
}
const matches = app.notes.whose({ id: payload.note_id })();
if (matches.length === 0) {
  return { ok: false, error: `note not found: ${payload.note_id}` };
}
const note = matches[0];
const folder = note.container();
return {
  ok: true,
  note: {
    id: note.id(),
    title: note.name(),
    body_html: valueOrNull(note.body()),
    plaintext: valueOrNull(note.plaintext()),
    created_at: iso(note.creationDate()),
    modified_at: iso(note.modificationDate()),
    shared: !!note.shared(),
    password_protected: !!note.passwordProtected(),
    account: resolveAccountName(folder),
    folder_id: folder.id(),
    folder_path: buildFolderPath(folder),
  },
};
""",
            payload={"note_id": note_id},
            operation="notes.get_raw",
            log_details={"note_id": note_id},
        )

    def _read_note_cache(self, *, note_id: str) -> dict[str, Any] | None:
        """Load the derived cache entry for one note."""
        return _load_json_file(
            self._apple_notes_cache_dir / _apple_notes_cache_filename(note_id)
        )

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

    def _build_note_view(
        self,
        raw_note: dict[str, Any],
        *,
        include_summary: bool,
    ) -> dict[str, Any]:
        """Build the LLM-facing note payload, using cache when possible."""
        note_id = raw_note["id"]
        modified_at = raw_note.get("modified_at")
        cached = self._read_note_cache(note_id=note_id)
        if (
            cached
            and cached.get("cache_version") == _APPLE_NOTES_CACHE_VERSION
            and cached.get("modified_at") == modified_at
        ):
            if include_summary and not cached.get("search_summary"):
                cached["search_summary"] = self._summarize_note_content(
                    title=cached.get("title"),
                    content_markdown=cached.get("content_markdown", ""),
                )
                self._write_note_cache(note_id=note_id, payload=cached)
            return cached

        content_markdown, image_hashes, has_images = self._render_note_markdown(
            note_id=note_id,
            body_html=raw_note.get("body_html") or "",
            plaintext=raw_note.get("plaintext") or "",
        )
        source_url = _extract_source_url(raw_note.get("body_html") or "")
        payload = {
            "id": note_id,
            "title": raw_note.get("title"),
            "created_at": raw_note.get("created_at"),
            "modified_at": modified_at,
            "shared": raw_note.get("shared", False),
            "password_protected": raw_note.get("password_protected", False),
            "account": raw_note.get("account"),
            "folder_id": raw_note.get("folder_id"),
            "folder_path": raw_note.get("folder_path"),
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

    def _build_note_search_entry(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Render one note candidate into a cached search entry."""
        raw_result = self._notes_get_raw(note_id=candidate["id"])
        if not raw_result.get("ok"):
            raise RuntimeError(raw_result.get("error") or "failed to fetch note")
        return self._build_note_view(raw_result["note"], include_summary=True)

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
        """Search notes using rendered Markdown and cached summaries."""
        metadata = self._notes_list_candidates(
            account=account,
            folder_id=folder_id,
            folder_path=folder_path,
            created_after=created_after,
            created_before=created_before,
            modified_after=modified_after,
            modified_before=modified_before,
            sort_by=sort_by,
        )
        if not metadata.get("ok"):
            return metadata
        candidates = metadata.get("results", [])
        if not candidates:
            return {
                "ok": True,
                "results": [],
                "count": 0,
                "total_matches": 0,
                "offset": max(0, offset or 0),
                "limit": max(1, min(limit or _APPLE_NOTES_DEFAULT_SEARCH_LIMIT, self._max_search_results)),
                "has_more": False,
            }

        workers = min(_APPLE_NOTES_MAX_NOTE_WORKERS, len(candidates))
        if workers <= 1:
            rendered = [self._build_note_search_entry(candidate) for candidate in candidates]
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                rendered = list(executor.map(self._build_note_search_entry, candidates))

        needle = (query or "").strip().lower()
        if needle:
            rendered = [
                note
                for note in rendered
                if needle in (note.get("title") or "").lower()
                or needle in (note.get("search_summary") or "").lower()
                or needle in (note.get("content_markdown") or "").lower()
            ]

        if sort_by == "modified_asc":
            rendered.sort(key=lambda item: item.get("modified_at") or "")
        elif sort_by == "created_desc":
            rendered.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        elif sort_by == "created_asc":
            rendered.sort(key=lambda item: item.get("created_at") or "")
        else:
            rendered.sort(key=lambda item: item.get("modified_at") or "", reverse=True)

        safe_offset = max(0, offset or 0)
        safe_limit = max(
            1,
            min(limit or _APPLE_NOTES_DEFAULT_SEARCH_LIMIT, self._max_search_results),
        )
        page = rendered[safe_offset : safe_offset + safe_limit]
        results = [
            {
                "id": note["id"],
                "title": note.get("title"),
                "summary": note.get("search_summary") or "",
                "created_at": note.get("created_at"),
                "modified_at": note.get("modified_at"),
                "account": note.get("account"),
                "folder_id": note.get("folder_id"),
                "folder_path": note.get("folder_path"),
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
            len(rendered),
            len(results),
            safe_offset,
            safe_limit,
        )
        return {
            "ok": True,
            "results": results,
            "count": len(results),
            "total_matches": len(rendered),
            "offset": safe_offset,
            "limit": safe_limit,
            "has_more": safe_offset + safe_limit < len(rendered),
        }

    def notes_get(self, *, note_id: str) -> dict[str, Any]:
        """Fetch one note by id and return rendered Markdown content."""
        raw_result = self._notes_get_raw(note_id=note_id)
        if not raw_result.get("ok"):
            return raw_result
        note = self._build_note_view(raw_result["note"], include_summary=False)
        return {
            "ok": True,
            "note": {
                "id": note["id"],
                "title": note.get("title"),
                "created_at": note.get("created_at"),
                "modified_at": note.get("modified_at"),
                "shared": note.get("shared", False),
                "password_protected": note.get("password_protected", False),
                "account": note.get("account"),
                "folder_id": note.get("folder_id"),
                "folder_path": note.get("folder_path"),
                "content_markdown": note.get("content_markdown", ""),
                "content_chars": note.get("content_chars", 0),
                "content_kind": note.get("content_kind"),
                "has_images": note.get("has_images", False),
                "image_count": note.get("image_count", 0),
                "source_url": note.get("source_url"),
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
  set targetFolder to first folder whose id is folderId
  tell targetFolder
    set newNote to make new note with properties {{body:noteBody}}
    return id of newNote
  end tell
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
        current = self._notes_get_raw(note_id=note_id)
        if not current.get("ok"):
            return current
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
        body_html = current["note"]["body_html"] or ""
        if append:
            payload = body_html + payload
        env = {"NOTE_ID": note_id}
        script = f"""
set noteId to system attribute "NOTE_ID"
set noteBody to {_applescript_utf8_file_read("NOTE_BODY")}
tell application "Notes"
  set targetNote to first note whose id is noteId
  set body of targetNote to noteBody
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
  set targetNote to first note whose id is noteId
  set targetFolder to first folder whose id is targetFolderId
  move targetNote to targetFolder
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
        """Resolve a Notes folder."""
        return self._run_jxa_json(
            """
const app = Application("Notes");
const payload = readPayload();
function flattenFolders(folder, accountName, parentPath) {
  const path = parentPath ? `${parentPath}/${folder.name()}` : `${accountName}/${folder.name()}`;
  let rows = [{ id: folder.id(), name: folder.name(), account: accountName, path }];
  for (const child of folder.folders()) {
    rows = rows.concat(flattenFolders(child, accountName, path));
  }
  return rows;
}
let folders = [];
for (const account of app.accounts()) {
  for (const folder of account.folders()) {
    folders = folders.concat(flattenFolders(folder, account.name(), ""));
  }
}
let target = null;
if (payload.folder_id) {
  target = folders.find((row) => row.id === payload.folder_id) || null;
} else if (payload.folder_path) {
  target = folders.find((row) => row.path === payload.folder_path) || null;
}
if (!target) {
  return { ok: false, error: "notes folder not found" };
}
return { ok: true, folder_id: target.id, folder_path: target.path, account: target.account, folder_name: target.name };
""",
            payload={"folder_id": folder_id, "folder_path": folder_path},
            operation="notes.resolve_folder",
            log_details={"folder_id": folder_id, "folder_path": folder_path},
        )


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
