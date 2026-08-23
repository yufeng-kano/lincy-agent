from __future__ import annotations

import json
from pathlib import Path

from ..json_store import load_json
from ..llm.base import Message
from ..llm.schema import ContentPart, ToolCall, make_tool_result_message
from ..timezone_utils import format_local_stamp
from .cache_breakpoints import build_cache_control
from .conversation import Conversation

_TOOL_BOOT_CALL_ID = "boot_ctx_0"
_TOOL_BOOT_NAME = "read_startup_context"

_PINNED_CALL_ID = "pinned_ctx_0"
_PINNED_TOOL_NAME = "read_pinned_context"

_RENDERED_STATIC_METADATA_KEY = "rendered_static"


class ContextBuilder:
    """Assembles context to send to LLM."""

    def __init__(
        self,
        system_prompt: str | None = None,
        agent_os_dir: Path | None = None,
        boot_files: list[str] | None = None,
        boot_files_as_tool: list[str] | None = None,
        preserve_turns: int = 6,
        provider: str = "openai",
        cache_ttl: str | None = None,
        fingerprint_boot_files: bool = False,
        fingerprint_boot_files_as_tool: bool = False,
    ):
        self.system_prompt = system_prompt
        self.agent_os_dir = agent_os_dir
        self.boot_files = boot_files
        self.boot_files_as_tool = boot_files_as_tool
        self.preserve_turns = preserve_turns
        self.provider = provider
        self.cache_ttl = cache_ttl
        self._fingerprint_boot_files = fingerprint_boot_files
        self._fingerprint_boot_files_as_tool = fingerprint_boot_files_as_tool
        self._boot_content_cache: str | None = None
        self._tool_boot_segments: list[tuple[str, str]] = []
        self._pinned_segments: list[tuple[str, str]] = []
        # Render cache: frozen rendered content for conversation messages.
        # Keyed by position in conversation.get_messages(). Once a message
        # is no longer the latest user message, its rendered content is
        # frozen and reused on subsequent builds, preventing prompt cache
        # prefix divergence.
        self._rendered_conv: list[Message] = []
        # Parallel list of source SessionEntry objects for identity checks.
        # Used to detect truncation/replace without relying on content comparison.
        self._rendered_conv_sources: list[object] = []

    def reload_boot_files(self) -> None:
        """Read boot files from disk and cache the result.

        Called on init, resume, context_refresh, overflow recovery,
        and skill rescan.  Only clears the render cache when boot
        content actually changed — a directory mtime bump (e.g. skill
        rescan with no content change) must not invalidate the prompt
        cache prefix.
        """
        old_fingerprint = self.boot_fingerprint()
        self._boot_content_cache = self._read_file_sections(self.boot_files)
        self._tool_boot_segments = self._read_file_segments(
            self.boot_files_as_tool,
        )
        self._pinned_segments = self._read_file_segments(
            self._load_pinned_paths(),
        )
        if self.boot_fingerprint() != old_fingerprint:
            self._rendered_conv.clear()
            self._rendered_conv_sources.clear()

    def clear_render_cache(self) -> None:
        """Clear frozen rendered messages.

        Call on context_refresh, overflow recovery, or session resume
        when the conversation is reset or truncated.
        """
        self._rendered_conv.clear()
        self._rendered_conv_sources.clear()

    def export_render_cache(self) -> list[Message]:
        """Return a snapshot of the frozen rendered messages for persistence."""
        return list(self._rendered_conv)

    def import_render_cache(
        self,
        rendered: list[Message],
        sources: list[object],
    ) -> bool:
        """Restore a previously persisted render cache.

        *rendered* should come from disk; *sources* must be the live
        conversation entry objects at matching positions so that the
        ``is``-based identity check in ``build()`` works correctly.
        """
        if not self._render_cache_matches_sources(rendered, sources):
            self.clear_render_cache()
            return False
        self._rendered_conv = list(rendered)
        self._rendered_conv_sources = list(sources)
        return True

    @classmethod
    def _render_cache_matches_sources(
        cls,
        rendered: list[Message],
        sources: list[object],
    ) -> bool:
        if len(rendered) != len(sources):
            return False
        return all(
            cls._render_cache_entry_matches_source(cached, source)
            for cached, source in zip(rendered, sources, strict=True)
        )

    @classmethod
    def _render_cache_entry_matches_source(
        cls,
        rendered: Message,
        source: object,
    ) -> bool:
        source_msg = getattr(source, "message", source)
        if not isinstance(source_msg, Message):
            return False
        if rendered.role != source_msg.role:
            return False
        if rendered.name != source_msg.name:
            return False
        if rendered.tool_call_id != source_msg.tool_call_id:
            return False
        if cls._tool_call_signature(rendered.tool_calls) != cls._tool_call_signature(
            source_msg.tool_calls,
        ):
            return False
        return cls._rendered_content_matches_source(rendered, source_msg)

    @staticmethod
    def _tool_call_signature(tool_calls: list[ToolCall] | None) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            (
                tool_call.id,
                tool_call.name,
                json.dumps(
                    tool_call.arguments,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
            )
            for tool_call in (tool_calls or [])
        )

    @classmethod
    def _rendered_content_matches_source(
        cls,
        rendered: Message,
        source: Message,
    ) -> bool:
        if source.content is None:
            return rendered.content is None or not cls._content_text(rendered.content)

        source_text = cls._content_text(source.content)
        rendered_text = cls._content_text(rendered.content)
        if source_text == rendered_text:
            return True
        if source.role in {"user", "assistant"} and source_text:
            return source_text in rendered_text
        return False

    @staticmethod
    def _content_text(content: str | list[ContentPart] | None) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                part.text or ""
                for part in content
                if isinstance(part, ContentPart) and part.type == "text"
            )
        return ""

    def boot_fingerprint(self) -> str:
        """Return a stable hash of the current boot content.

        Used to invalidate the persisted render cache when boot files
        change between runs (e.g. after a kernel upgrade).
        Always includes system_prompt; boot_files and boot_files_as_tool
        are opt-in via constructor flags.
        """
        import hashlib

        h = hashlib.sha256()
        h.update((self.system_prompt or "").encode())
        if self._fingerprint_boot_files:
            h.update((self._boot_content_cache or "").encode())
        if self._fingerprint_boot_files_as_tool:
            for _path, content in self._tool_boot_segments:
                h.update(content.encode())
        return h.hexdigest()[:16]

    def update_system_prompt(self, system_prompt: str) -> None:
        """Replace the resolved system prompt (e.g. after date change)."""
        self.system_prompt = system_prompt

    def _read_file_sections(self, file_list: list[str] | None) -> str | None:
        """Read files from disk and return combined <file> content."""
        if not self.agent_os_dir or not file_list:
            return None

        sections: list[str] = []
        for rel_path in file_list:
            full_path = self.agent_os_dir / rel_path
            try:
                content = full_path.read_text(encoding="utf-8")
                sections.append(
                    f'<file path="{rel_path}">\n{content.rstrip()}\n</file>'
                )
            except FileNotFoundError:
                sections.append(f'<file path="{rel_path}">\n[File not found]\n</file>')

        if not sections:
            return None
        return "\n\n".join(sections)

    def _read_file_segments(
        self,
        file_list: list[str] | None,
    ) -> list[tuple[str, str]]:
        """Read files from disk and return per-file (path, content) tuples.

        Each file becomes a separate cache block so unchanged files keep
        their cache hit when other files change (e.g. after archive).
        """
        if not self.agent_os_dir or not file_list:
            return []
        segments: list[tuple[str, str]] = []
        for rel_path in file_list:
            full_path = self.agent_os_dir / rel_path
            try:
                content = full_path.read_text(encoding="utf-8").rstrip()
            except FileNotFoundError:
                content = "[File not found]"
            segments.append((rel_path, content))
        return segments

    def _load_pinned_paths(self) -> list[str] | None:
        """Load pinned context paths from the registry file.

        A corrupt registry logs a warning (via load_json) instead of
        silently dropping every pinned file from the prompt.
        """
        if not self.agent_os_dir:
            return None
        registry = self.agent_os_dir / "state" / "pinned_context.json"
        if not registry.exists():
            return None
        data = load_json(registry, default={})
        pins = data.get("pins", []) if isinstance(data, dict) else []
        return [p["path"] for p in pins if isinstance(p, dict) and p.get("path")]

    @staticmethod
    def _build_synthetic_file_messages(
        segments: list[tuple[str, str]],
        *,
        call_id_prefix: str,
        tool_name: str,
    ) -> list[Message]:
        """Build a synthetic tool-call/result pair for injected files.

        Each file gets its own tool result so Anthropic's backward prefix
        checking can cache unchanged files independently.
        """
        if not segments:
            return []

        # One assistant message with parallel tool calls
        call_msg = Message(
            role="assistant",
            content=None,
            tool_calls=[
                ToolCall(
                    id=f"{call_id_prefix}_{i}",
                    name=tool_name,
                    arguments={"file": rel_path},
                )
                for i, (rel_path, _content) in enumerate(segments)
            ],
        )

        # One tool result per file (separate cache blocks)
        result_msgs = [
            make_tool_result_message(
                tool_call_id=f"{call_id_prefix}_{i}",
                name=tool_name,
                content=f'<file path="{rel_path}">\n{content}\n</file>',
            )
            for i, (rel_path, content) in enumerate(segments)
        ]
        return [call_msg] + result_msgs

    def _build_tool_boot_messages(self) -> list[Message]:
        """Build synthetic tool messages for tool-tier boot files."""
        return self._build_synthetic_file_messages(
            self._tool_boot_segments,
            call_id_prefix=_TOOL_BOOT_CALL_ID,
            tool_name=_TOOL_BOOT_NAME,
        )

    def _build_pinned_context_messages(self) -> list[Message]:
        """Build synthetic tool messages for pinned context files."""
        return self._build_synthetic_file_messages(
            self._pinned_segments,
            call_id_prefix=_PINNED_CALL_ID,
            tool_name=_PINNED_TOOL_NAME,
        )

    @staticmethod
    def _inject_conversation_cache_breakpoint(
        kept_conv: list[Message],
        cache_ctrl: dict[str, str],
    ) -> list[Message]:
        """Inject BP3: keep the previous user turn as the reusable cache endpoint.

        Sets ``Message.cache_control`` on the target message.  Content type
        is never changed — the provider adapter reads ``cache_control`` during
        serialization and wraps the content block accordingly.
        """
        # Find the last user message (current turn start)
        last_user_pos = None
        for i in range(len(kept_conv) - 1, -1, -1):
            if kept_conv[i].role == "user":
                last_user_pos = i
                break

        if last_user_pos is None or last_user_pos == 0:
            return kept_conv

        # Prefer the previous user turn.  The responder will add the latest
        # user turn as a second conversation breakpoint.  Keeping the previous
        # user endpoint stable avoids a cold read after tool-heavy turns, where
        # the previous final assistant was not itself a cached endpoint.
        for i in range(last_user_pos - 1, -1, -1):
            msg = kept_conv[i]
            if msg.role != "user":
                continue
            if isinstance(msg.content, str) and msg.content:
                break
        else:
            # Fallback for non-standard histories without an earlier user turn.
            for i in range(last_user_pos - 1, -1, -1):
                msg = kept_conv[i]
                if msg.role == "system":
                    continue
                if msg.role == "tool":
                    continue
                if msg.role == "assistant" and msg.tool_calls:
                    continue
                if not isinstance(msg.content, str) or not msg.content:
                    continue
                break
            else:
                return kept_conv

        # Set cache_control on the message (metadata only, content unchanged)
        kept_conv = list(kept_conv)
        kept_conv[i] = Message(
            role=msg.role,
            content=msg.content,
            reasoning_content=msg.reasoning_content,
            reasoning_details=msg.reasoning_details,
            tool_calls=msg.tool_calls,
            tool_call_id=msg.tool_call_id,
            name=msg.name,
            timestamp=msg.timestamp,
            cache_control=cache_ctrl,
        )

        return kept_conv

    def _cache_control_dict(self) -> dict[str, str] | None:
        """Build cache_control dict from cache_ttl setting."""
        return build_cache_control(self.cache_ttl)

    @staticmethod
    def _find_last_user_message_index(messages: list[Message]) -> int | None:
        """Return the index of the latest user message in conversation order."""
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].role == "user":
                return i
        return None

    def build(self, conversation: Conversation) -> list[Message]:
        """Build context from conversation history."""
        prefix: list[Message] = []
        cache_ctrl = self._cache_control_dict()

        # BP1: system prompt (most stable, largest block)
        if self.system_prompt:
            prefix.append(Message(
                role="system",
                content=self.system_prompt,
                cache_control=cache_ctrl,
            ))

        # BP2: system-tier boot files (snapshot-based: cached by reload_boot_files)
        boot_content = self._boot_content_cache
        if boot_content:
            prefix.append(Message(
                role="system",
                content=f"[Core Rules]\n\n{boot_content}",
                cache_control=cache_ctrl,
            ))

        # Inject tool-tier boot files as synthetic tool-call/result pair
        prefix.extend(self._build_tool_boot_messages())

        # Inject pinned context files (agent-registered, loaded at reload time)
        prefix.extend(self._build_pinned_context_messages())

        # Process conversation messages with render cache. Non-latest
        # messages reuse their frozen rendered content (channel/sender tag,
        # timestamp prefix) so re-rendering stays stable and cheap across
        # turns.
        all_msgs = conversation.get_messages()
        last_user_idx = self._find_last_user_message_index(all_msgs)

        # Invalidate render cache when conversation was truncated/replaced.
        # Track source entries by object identity (Conversation is append-only
        # in normal flow; truncate/replace creates different objects).
        if len(self._rendered_conv_sources) > len(all_msgs):
            self._rendered_conv.clear()
            self._rendered_conv_sources.clear()

        conv_messages: list[Message] = []
        for idx, msg in enumerate(all_msgs):
            # Reuse frozen rendered message for non-latest positions,
            # but only if the source entry is the exact same object.
            if idx < len(self._rendered_conv) and idx != last_user_idx:
                if self._rendered_conv_sources[idx] is msg:
                    conv_messages.append(self._rendered_conv[idx])
                    continue
                # Source entry changed; invalidate from this point.
                del self._rendered_conv[idx:]
                del self._rendered_conv_sources[idx:]

            content = msg.content
            entry_metadata = getattr(msg, "metadata", None) or {}
            rendered_static = bool(entry_metadata.get(_RENDERED_STATIC_METADATA_KEY))

            # Inject [channel, from sender] tag for user messages
            if (
                not rendered_static
                and msg.role == "user"
                and isinstance(content, str)
                and content
            ):
                channel = getattr(msg, "channel", None)
                sender = getattr(msg, "sender", None)
                if channel and sender:
                    content = f"[{channel}, from {sender}] {content}"
                elif channel:
                    content = f"[{channel}] {content}"
                # Per-turn dynamic blocks ([Runtime Context], [Timing Notice],
                # [Agent Notes]) are NOT injected here.
                # They used to be appended to the latest user message and,
                # once frozen by the render cache, would persist forever on
                # every historical user message. They are now applied only
                # to the outgoing request's latest user message by the
                # responder overlay (see agent/turn_overlay.py and
                # agent/responder.py), never written back into Conversation
                # or this render cache. See docs/dev/agent-task-system.md
                # and docs/dev/token-only-context-policy.md.

            if (
                not rendered_static
                and msg.timestamp
                and msg.role in ("user", "assistant")
                and isinstance(content, str)
                and content
            ):
                content = f"[{format_local_stamp(msg.timestamp)}] {content}"

            rendered = Message(
                role=msg.role,
                content=content,
                reasoning_content=msg.reasoning_content,
                reasoning_details=msg.reasoning_details,
                tool_calls=msg.tool_calls,
                tool_call_id=msg.tool_call_id,
                name=msg.name,
                codex_compaction_encrypted_content=msg.codex_compaction_encrypted_content,
            )
            # Update render cache (extend or overwrite latest).
            if idx < len(self._rendered_conv):
                self._rendered_conv[idx] = rendered
                self._rendered_conv_sources[idx] = msg
            else:
                self._rendered_conv.append(rendered)
                self._rendered_conv_sources.append(msg)
            conv_messages.append(rendered)

        # BP3: cache conversation prefix before current turn
        if cache_ctrl and conv_messages:
            conv_messages = self._inject_conversation_cache_breakpoint(
                conv_messages,
                cache_ctrl,
            )

        return prefix + conv_messages
