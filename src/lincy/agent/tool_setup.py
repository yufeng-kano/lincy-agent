"""Tool registry setup for the brain agent."""

from __future__ import annotations

from collections.abc import Callable
import logging
import os
from pathlib import Path
import sys
import threading
from typing import TYPE_CHECKING

from dotenv import dotenv_values

if TYPE_CHECKING:
    from .contact_map import ContactMap

from ..cli.claude_code_stream_json import (
    extract_text_from_claude_code_stream_json_lines,
)
from ..core.schema import ToolsConfig
from ..gui import (
    GUIManager,
    GUIWorker,
    SCREENSHOT_BY_SUBAGENT_DEFINITION,
    SCREENSHOT_DEFINITION,
    create_screenshot,
    create_screenshot_adaptive,
    create_screenshot_by_subagent,
)
from ..memory import (
    MEMORY_EDIT_DEFINITION,
    MEMORY_SEARCH_DEFINITION,
    BM25MemorySearch,
    MemoryEditor,
    create_bm25_memory_search,
    create_memory_edit,
)
from ..tools import (
    EDIT_FILE_DEFINITION,
    EXECUTE_SHELL_DEFINITION,
    READ_FILE_DEFINITION,
    READ_IMAGE_BY_SUBAGENT_DEFINITION,
    READ_IMAGE_DEFINITION,
    WEB_FETCH_DEFINITION,
    WEB_SEARCH_DEFINITION,
    WRITE_FILE_DEFINITION,
    CALENDAR_TOOL_DEFINITION,
    REMINDERS_TOOL_DEFINITION,
    NOTES_TOOL_DEFINITION,
    PHOTOS_TOOL_DEFINITION,
    MAIL_TOOL_DEFINITION,
    MacOSAppBridge,
    ShellExecutor,
    ToolRegistry,
    VisionAgent,
    create_calendar_tool,
    create_edit_file,
    create_execute_shell,
    create_mail_tool,
    create_notes_tool,
    create_photos_tool,
    create_read_file,
    create_read_image_adaptive,
    create_read_image_by_subagent,
    create_read_image_vision,
    create_read_image_with_sub_agent,
    create_reminders_tool,
    create_web_fetch,
    create_web_search,
    create_write_file,
)
from ..tools.security import is_memory_write_shell_command

logger = logging.getLogger(__name__)


def _normalize_memory_path(path: str) -> str:
    """Normalize path string for memory path checks."""
    return path.strip().replace("\\", "/")


def _is_memory_path(path: str, *, agent_os_dir: Path) -> bool:
    """Check whether a path points to memory/ in relative or absolute form."""
    normalized = _normalize_memory_path(path)
    if normalized.startswith("./"):
        normalized = normalized[2:]

    if normalized == "memory" or normalized.startswith("memory/"):
        return True
    if normalized.startswith(".agent/memory/"):
        return True

    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = agent_os_dir / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to((agent_os_dir / "memory").resolve())
        return True
    except Exception:
        return False


def setup_tools(
    tools_config: ToolsConfig,
    agent_os_dir: Path,
    *,
    memory_editor: MemoryEditor | None = None,
    bm25_search: BM25MemorySearch | None = None,
    brain_has_vision: bool = False,
    use_own_vision_ability: bool = False,
    own_vision_active: Callable[[], bool] | None = None,
    vision_agent: VisionAgent | None = None,
    gui_manager: GUIManager | None = None,
    gui_worker: GUIWorker | None = None,
    gui_lock: threading.Lock | None = None,
    screenshot_max_width: int | None = None,
    screenshot_quality: int = 80,
    contact_map: ContactMap | None = None,
    extra_allowed_paths: list[str] | None = None,
    on_shell_stdout_line: Callable[[str], None] | None = None,
    is_shell_cancel_requested: Callable[[], bool] | None = None,
    web_fetch_summarizer: object | None = None,
) -> tuple[ToolRegistry, list[str], ShellExecutor]:
    """Set up the tool registry with built-in tools."""
    registry = ToolRegistry()

    executor = ShellExecutor(
        agent_os_dir=agent_os_dir,
        blacklist=tools_config.shell.blacklist,
        timeout=tools_config.shell.timeout,
        export_env=tools_config.shell.export_env,
        is_cancel_requested=is_shell_cancel_requested,
    )
    output_transform = (
        extract_text_from_claude_code_stream_json_lines
        if on_shell_stdout_line
        else None
    )
    base_execute_shell = create_execute_shell(
        executor,
        on_stdout_line=on_shell_stdout_line,
        output_transform=output_transform,
    )

    def guarded_execute_shell(command: str, timeout: int | None = None) -> str:
        if is_memory_write_shell_command(command, agent_os_dir=agent_os_dir):
            return "Error: Direct memory writes via shell are blocked. Use memory_edit."
        return base_execute_shell(command, timeout)

    registry.register("execute_shell", guarded_execute_shell, EXECUTE_SHELL_DEFINITION)

    allowed_paths = list(tools_config.allowed_paths)
    allowed_paths.insert(0, str(agent_os_dir))
    if gui_manager is not None:
        allowed_paths.append(gui_manager.capture_dir)
    if extra_allowed_paths:
        allowed_paths.extend(extra_allowed_paths)

    registry.register(
        "read_file",
        create_read_file(allowed_paths, agent_os_dir),
        READ_FILE_DEFINITION,
    )
    base_write_file = create_write_file(allowed_paths, agent_os_dir)
    base_edit_file = create_edit_file(allowed_paths, agent_os_dir)

    def guarded_write_file(path: str, content: str) -> str:
        if _is_memory_path(path, agent_os_dir=agent_os_dir):
            return "Error: Direct memory writes are blocked. Use memory_edit."
        return base_write_file(path, content)

    def guarded_edit_file(
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        if _is_memory_path(path, agent_os_dir=agent_os_dir):
            return "Error: Direct memory edits are blocked. Use memory_edit."
        return base_edit_file(path, old_string, new_string, replace_all)

    registry.register("write_file", guarded_write_file, WRITE_FILE_DEFINITION)
    registry.register("edit_file", guarded_edit_file, EDIT_FILE_DEFINITION)

    if tools_config.apple_apps.enabled and sys.platform == "darwin":
        apple_bridge = MacOSAppBridge(
            base_dir=agent_os_dir,
            allowed_paths=allowed_paths,
            timeout_seconds=tools_config.apple_apps.timeout_seconds,
            max_search_results=tools_config.apple_apps.max_search_results,
            photos_export_dir=tools_config.apple_apps.photos_export_dir,
            mail_export_dir=tools_config.apple_apps.mail_export_dir,
            vision_agent=vision_agent,
            notes_summarizer=web_fetch_summarizer,
        )
        registry.register(
            "calendar_tool",
            create_calendar_tool(apple_bridge),
            CALENDAR_TOOL_DEFINITION,
        )
        registry.register(
            "reminders_tool",
            create_reminders_tool(apple_bridge),
            REMINDERS_TOOL_DEFINITION,
        )
        registry.register(
            "notes_tool",
            create_notes_tool(apple_bridge),
            NOTES_TOOL_DEFINITION,
        )
        registry.register(
            "photos_tool",
            create_photos_tool(apple_bridge),
            PHOTOS_TOOL_DEFINITION,
        )
        registry.register(
            "mail_tool",
            create_mail_tool(apple_bridge),
            MAIL_TOOL_DEFINITION,
        )

    if memory_editor is not None:
        registry.register(
            "memory_edit",
            create_memory_edit(
                memory_editor,
                allowed_paths=allowed_paths,
                base_dir=agent_os_dir,
            ),
            MEMORY_EDIT_DEFINITION,
        )

    if bm25_search is not None:
        registry.register(
            "memory_search",
            create_bm25_memory_search(bm25_search),
            MEMORY_SEARCH_DEFINITION,
        )

    if tools_config.web_fetch.enabled:
        # Allow read_image to access images saved by web_fetch.
        allowed_paths.append("/tmp/chat-agent-images")
        _summarizer = (
            web_fetch_summarizer
            if tools_config.web_fetch.summarize_with_llm
            else None
        )
        registry.register(
            "web_fetch",
            create_web_fetch(
                timeout=tools_config.web_fetch.timeout,
                default_max_chars=tools_config.web_fetch.default_max_chars,
                max_response_chars=tools_config.web_fetch.max_response_chars,
                max_response_bytes=tools_config.web_fetch.max_response_bytes,
                user_agent=tools_config.web_fetch.user_agent,
                allow_private_hosts=tools_config.web_fetch.allow_private_hosts,
                summarizer=_summarizer,
            ),
            WEB_FETCH_DEFINITION,
        )

    if tools_config.web_search.enabled:
        env_values = dotenv_values()
        api_key_env = tools_config.web_search.api_key_env
        api_key = env_values.get(api_key_env) or os.getenv(api_key_env)
        if api_key:
            registry.register(
                "web_search",
                create_web_search(
                    api_key=api_key,
                    timeout=tools_config.web_search.timeout,
                    default_max_results=tools_config.web_search.default_max_results,
                    max_results_limit=tools_config.web_search.max_results_limit,
                    include_raw_content=tools_config.web_search.include_raw_content,
                ),
                WEB_SEARCH_DEFINITION,
            )
        else:
            logger.warning(
                "web_search enabled but API key env is missing: %s",
                api_key_env,
            )

    # When use_own_vision_ability is true but some failover candidates lack
    # vision, own_vision_active selects per-candidate behavior: vision models
    # keep multimodal tools; non-vision models fall back to sub-agent text.
    vision_gate = own_vision_active
    adaptive_own_vision = (
        brain_has_vision
        and use_own_vision_ability
        and vision_gate is not None
    )

    if adaptive_own_vision and vision_gate is not None:
        registry.register(
            "read_image",
            create_read_image_adaptive(
                allowed_paths,
                agent_os_dir,
                vision_agent,
                vision_gate,
            ),
            READ_IMAGE_DEFINITION,
        )
    elif brain_has_vision and not use_own_vision_ability and vision_agent is not None:
        registry.register(
            "read_image_by_subagent",
            create_read_image_by_subagent(allowed_paths, agent_os_dir, vision_agent),
            READ_IMAGE_BY_SUBAGENT_DEFINITION,
        )
    elif brain_has_vision:
        registry.register(
            "read_image",
            create_read_image_vision(allowed_paths, agent_os_dir),
            READ_IMAGE_DEFINITION,
        )
    elif vision_agent is not None:
        registry.register(
            "read_image",
            create_read_image_with_sub_agent(allowed_paths, agent_os_dir, vision_agent),
            READ_IMAGE_DEFINITION,
        )

    if adaptive_own_vision and vision_gate is not None:
        registry.register(
            "screenshot",
            create_screenshot_adaptive(
                max_width=screenshot_max_width,
                quality=screenshot_quality,
                own_vision_active=vision_gate,
            ),
            SCREENSHOT_DEFINITION,
        )
        if gui_worker is not None:
            crop_dir = str(agent_os_dir / "tmp")
            registry.register(
                "screenshot_by_subagent",
                create_screenshot_by_subagent(
                    gui_worker,
                    save_dir=crop_dir,
                    gui_lock=gui_lock,
                ),
                SCREENSHOT_BY_SUBAGENT_DEFINITION,
            )
            allowed_paths.append(crop_dir)
    elif brain_has_vision and not use_own_vision_ability and gui_worker is not None:
        crop_dir = str(agent_os_dir / "tmp")
        registry.register(
            "screenshot_by_subagent",
            create_screenshot_by_subagent(
                gui_worker,
                save_dir=crop_dir,
                gui_lock=gui_lock,
            ),
            SCREENSHOT_BY_SUBAGENT_DEFINITION,
        )
        allowed_paths.append(crop_dir)
    elif brain_has_vision:
        registry.register(
            "screenshot",
            create_screenshot(
                max_width=screenshot_max_width,
                quality=screenshot_quality,
            ),
            SCREENSHOT_DEFINITION,
        )

    # Pinned context management
    from ..context.pinned_context import (
        pin_context as _pin_context,
        unpin_context as _unpin_context,
        list_pinned_context as _list_pinned_context,
    )
    from ..llm.schema import ToolDefinition, ToolParameter

    def _handle_pin_context(path: str, reason: str = "") -> str:
        from datetime import datetime, timezone
        return _pin_context(
            agent_os_dir,
            rel_path=path,
            reason=reason,
            pinned_at=datetime.now(timezone.utc).isoformat(),
        )

    def _handle_unpin_context(path: str) -> str:
        return _unpin_context(agent_os_dir, rel_path=path)

    def _handle_list_pinned_context() -> str:
        return _list_pinned_context(agent_os_dir)

    registry.register(
        "pin_context",
        _handle_pin_context,
        ToolDefinition(
            name="pin_context",
            description=(
                "Register a memory file to be auto-loaded at boot time. "
                "Pinned files appear as context on every session start, "
                "eliminating the need to search and read them each turn. "
                "Takes effect on next session reload. Max 8 files."
            ),
            parameters={
                "path": ToolParameter(
                    type="string",
                    description="Relative path under agent_os_dir (must start with memory/)",
                ),
                "reason": ToolParameter(
                    type="string",
                    description="Why this file should be loaded at boot",
                ),
            },
            required=["path", "reason"],
        ),
    )
    registry.register(
        "unpin_context",
        _handle_unpin_context,
        ToolDefinition(
            name="unpin_context",
            description="Remove a file from boot-time auto-loading.",
            parameters={
                "path": ToolParameter(
                    type="string",
                    description="Relative path to unpin",
                ),
            },
            required=["path"],
        ),
    )
    registry.register(
        "list_pinned_context",
        _handle_list_pinned_context,
        ToolDefinition(
            name="list_pinned_context",
            description="List all files currently pinned for boot-time loading.",
            parameters={},
        ),
    )

    if contact_map is not None:
        from ..tools.builtin.contact_mapping import (
            UPDATE_CONTACT_MAPPING_DEFINITION,
            create_update_contact_mapping,
        )

        registry.register(
            "update_contact_mapping",
            create_update_contact_mapping(contact_map),
            UPDATE_CONTACT_MAPPING_DEFINITION,
        )

    # Tools that modify external state and can be preempted when
    # fresher inbound arrives mid-tool-loop.
    registry.set_side_effect_tools(frozenset({
        "send_message",
        "memory_edit",
        "write_file",
        "edit_file",
        "execute_shell",
        "shell_task",
        "schedule_action",
        "update_contact_mapping",
        "gui_task",
        "calendar_tool",
        "reminders_tool",
        "notes_tool",
        "photos_tool",
        "mail_tool",
    }))

    return registry, allowed_paths, executor
