import json
import logging
import os
import sys
import threading
from collections.abc import Callable
from datetime import datetime

from dotenv import dotenv_values

from ..agent import AgentCore, setup_tools
from ..agent.tool_setup import validate_excluded_tools
from ..agent.skill_check import SkillCheckAgent
from ..agent.adapters.cli import CLIAdapter
from ..agent.contact_map import ContactMap
from ..agent.thread_registry import ThreadRegistry
from ..agent.queue import PersistentPriorityQueue
from ..agent.scope import DEFAULT_SCOPE_RESOLVER
from ..agent.shared_state import load_or_init as load_shared_state_cache
from ..agent.shared_state_replay import rebuild_shared_state_from_sessions
from ..agent.ui_event_stream import FanoutUiSink, UiEventExportSink, UiEventStore
from ..brain_prompt_policy import BrainPromptPolicy
from ..context import ContextBuilder, Conversation
from ..context.cache_breakpoints import (
    build_cache_control,
    resolve_breakpoint_cache_ttl,
)
from ..core import load_config
from ..core.schema import CodexConfig, CopilotConfig, GrokConfig, OpenAIConfig
from ..llm import create_agent_client
from ..memory import (
    BM25MemorySearch,
    MemoryEditor,
    MemoryEditPlanner,
    SessionCommitLog,
)
from ..memory.backup import MemoryBackupManager
from ..skills import rebuild_personal_skills_index
from ..workspace import WorkspaceManager, WorkspaceInitializer
from ..workspace.people import ensure_user_memory_file, resolve_user_selector
from ..tools import VisionAgent
from ..gui import (
    GUIManager,
    GUISessionStore,
    GUIWorker,
)
from ..llm.providers.copilot_runtime import CopilotRuntime

from .commands import CommandHandler
from ..session import SessionManager, pick_session
from ..session.debug_client import wrap_llm_client_with_session_debug
from ..tui import (
    ChatTextualApp,
    QueueUiSink,
    TextualController,
    TextualUiConsole,
    TurnCancelController,
    UiSink,
)
from ..timezone_utils import now as tz_now


logger = logging.getLogger(__name__)


class _RetryUiHandler(logging.Handler):
    """Route LLM retry logs to visible UI warnings."""

    def __init__(self, console):
        super().__init__()
        self._console = console

    def emit(self, record: logging.LogRecord) -> None:
        self._console.print_warning(f"LLM retry: {self.format(record)}", indent=2)


def _install_llm_retry_ui_handler(console) -> None:
    """Install one visible handler for lincy.llm.retry logs."""
    retry_logger = logging.getLogger("lincy.llm.retry")
    for handler in list(retry_logger.handlers):
        if isinstance(handler, _RetryUiHandler):
            retry_logger.removeHandler(handler)
    if not callable(getattr(console, "print_warning", None)):
        # Some test doubles / minimal consoles do not implement warning output.
        # Skip installing the UI retry handler instead of crashing on log emit.
        return
    retry_handler = _RetryUiHandler(console)
    retry_handler.setLevel(logging.DEBUG)
    retry_logger.addHandler(retry_handler)
    retry_logger.setLevel(logging.DEBUG)


def _agent_supports_response_schema(agent_config) -> bool:
    """Return true only when every failover candidate accepts response_schema."""
    return all(
        llm_config.supports_response_schema()
        for llm_config in [agent_config.llm, *agent_config.llm_fallbacks]
    )


def _codex_cache_bucket(ttl: str, *, current_time: datetime | None = None) -> str | None:
    now = current_time or tz_now()
    if ttl == "24h":
        return now.strftime("%Y%m%d")
    if ttl == "1h":
        return now.strftime("%Y%m%d%H")
    if ttl == "ephemeral":
        return f"{now.strftime('%Y%m%d%H')}{now.minute // 5:02d}"
    return None


def _make_codex_cache_key_provider(
    *,
    session_id_getter: Callable[[], str | None],
    namespace: str,
    enabled: bool,
    ttl: str,
) -> Callable[[], str | None] | None:
    if not enabled:
        return None

    bucket = _codex_cache_bucket(ttl)
    if bucket is None:
        logging.getLogger(__name__).warning(
            "codex cache.ttl %r is unsupported; prompt cache key disabled",
            ttl,
        )
        return None

    def _provider() -> str | None:
        session_id = session_id_getter()
        if not session_id:
            return None
        # Official Codex CLI uses conversation_id as prompt_cache_key:
        # https://github.com/openai/codex/blob/main/codex-rs/core/src/client.rs
        # This project adds a namespace + TTL bucket so agent.yaml cache.ttl
        # changes the key lifetime instead of being silently ignored.
        next_bucket = _codex_cache_bucket(ttl)
        if next_bucket is None:
            return None
        return f"{session_id}:{namespace}:{next_bucket}"

    return _provider


def _make_grok_conv_id_provider(
    *,
    session_id_getter: Callable[[], str | None],
    namespace: str,
    enabled: bool,
    ttl: str,
) -> Callable[[], str | None]:
    """Build sticky ``x-grok-conv-id`` values for xAI prompt-cache routing.

    xAI auto-caches shared prefixes, but hits are per-server. The official
    Chat Completions recommendation is to always set ``x-grok-conv-id`` so
    consecutive turns land on the same host.

    - Always sticky on session + agent namespace when a session exists.
    - When cache.enabled, also rotate a TTL bucket (same buckets as codex)
      so ``cache.ttl`` actually changes key lifetime instead of being ignored.
    """

    def _provider() -> str | None:
        session_id = session_id_getter()
        if not session_id:
            return None
        if not enabled:
            return f"{session_id}:{namespace}"
        bucket = _codex_cache_bucket(ttl)
        if bucket is None:
            logging.getLogger(__name__).warning(
                "grok cache.ttl %r is unsupported; sticky conv id without TTL bucket",
                ttl,
            )
            return f"{session_id}:{namespace}"
        return f"{session_id}:{namespace}:{bucket}"

    return _provider


def _emit_pre_tui_message(console, level: str, message: str) -> None:
    """Mirror startup diagnostics to stderr before Textual takes over."""
    printer = getattr(console, f"print_{level}", None)
    if callable(printer):
        printer(message)
    log_fn = getattr(logger, level, logger.info)
    log_fn(message)
    print(f"[chat-cli startup] {message}", file=sys.stderr, flush=True)


def main(user: str, resume: str | None = None) -> None:
    """Main entry point for the CLI."""
    user_selector = user.strip()
    if not user_selector:
        raise ValueError("user is required")

    config = load_config()
    agent_os_dir = config.get_agent_os_dir()

    # Must be first: everything downstream may call tz_now()
    from ..timezone_utils import configure_runtime_timezone
    configure_runtime_timezone(config.app.timezone)

    # The Textual app owns this exact instance (it needs drain/set_on_emit).
    ui_sink = QueueUiSink()
    # Everything else writes through a fan-out so the web dashboard can tap the
    # same typed event stream without the TUI knowing about it.
    sink_for_agents: UiSink = ui_sink
    if config.channels.web.enabled:
        ui_event_store = UiEventStore(agent_os_dir / "state" / "ui_events" / "events.jsonl")
        ui_event_store.rotate_on_start()
        sink_for_agents = FanoutUiSink((ui_sink, UiEventExportSink(ui_event_store)))

    cancel_controller = TurnCancelController(ui_sink=sink_for_agents)
    controller = TextualController(ui_sink=sink_for_agents, cancel=cancel_controller)

    # Check workspace initialization
    workspace = WorkspaceManager(agent_os_dir)
    console = TextualUiConsole(sink_for_agents)

    if not workspace.is_initialized():
        _emit_pre_tui_message(
            console,
            "error",
            f"Workspace not initialized at {agent_os_dir}",
        )
        _emit_pre_tui_message(
            console,
            "info",
            "Run 'uv run python -m lincy init' first.",
        )
        return

    # Auto-upgrade kernel if needed
    initializer = WorkspaceInitializer(workspace)
    migration_result = None
    if initializer.needs_upgrade():
        _emit_pre_tui_message(console, "info", "Upgrading kernel...")
        migration_result = initializer.upgrade_kernel()
        for v in migration_result.applied_versions:
            _emit_pre_tui_message(console, "info", f"  Applied: v{v}")
        _emit_pre_tui_message(console, "info", "Kernel upgraded.")

    rebuild_personal_skills_index(agent_os_dir)

    try:
        user_id, display_name = resolve_user_selector(workspace.memory_dir, user_selector)
        ensure_user_memory_file(workspace.memory_dir, user_id, display_name)
    except ValueError as e:
        _emit_pre_tui_message(console, "error", str(e))
        return

    # Load bootloader prompt and resolve {agent_os_dir} placeholder
    brain_prompt_policy = BrainPromptPolicy(
        kernel_dir=workspace.kernel_dir,
        config=config,
    )
    try:
        system_prompt = workspace.get_system_prompt("brain")
        system_prompt = system_prompt.replace("{agent_os_dir}", str(agent_os_dir))
        system_prompt = brain_prompt_policy.resolve(system_prompt)
    except FileNotFoundError as e:
        _emit_pre_tui_message(console, "error", f"Failed to load system prompt: {e}")
        return

    debug = config.tui.debug
    console.set_debug(debug)
    console.set_current_user(user_id)
    console.set_show_tool_use(config.tui.show_tool_use)
    # Surface LLM retry attempts in normal UI (not only debug mode).
    _install_llm_retry_ui_handler(console)

    copilot_runtime = CopilotRuntime(config.features.copilot.initiator_policy)
    session_mgr: SessionManager | None = None

    def _provider_kwargs(
        llm_config,
        *,
        dispatch_mode: str,
        cache_retention: str | None = None,
        cache_enabled: bool = False,
        cache_ttl: str = "ephemeral",
        cache_namespace: str | None = None,
    ):
        """Build provider-specific kwargs for create_client."""
        kwargs: dict[str, object] = {}
        if isinstance(llm_config, CopilotConfig):
            kwargs.update({
                "runtime": copilot_runtime,
                "dispatch_mode": dispatch_mode,
            })
        if isinstance(llm_config, OpenAIConfig) and cache_retention:
            kwargs["prompt_cache_retention"] = cache_retention
        if isinstance(llm_config, GrokConfig) and cache_namespace is not None:
            # Always sticky-route for max xAI cache hits; TTL bucket when enabled.
            kwargs["conv_id_provider"] = _make_grok_conv_id_provider(
                session_id_getter=(
                    lambda: session_mgr.current_session_id if session_mgr is not None else None
                ),
                namespace=cache_namespace,
                enabled=cache_enabled,
                ttl=cache_ttl,
            )
        if isinstance(llm_config, CodexConfig):
            kwargs["session_id_provider"] = (
                lambda: session_mgr.current_session_id if session_mgr is not None else None
            )
            kwargs["turn_id_provider"] = (
                lambda: session_mgr.current_turn_id if session_mgr is not None else None
            )
            if cache_namespace is not None:
                cache_key_provider = _make_codex_cache_key_provider(
                    session_id_getter=(
                        lambda: session_mgr.current_session_id if session_mgr is not None else None
                    ),
                    namespace=cache_namespace,
                    enabled=cache_enabled,
                    ttl=cache_ttl,
                )
                if cache_key_provider is not None:
                    kwargs["cache_key_provider"] = cache_key_provider
        return kwargs

    def _provider_kwargs_factory(
        *,
        dispatch_mode: str,
        cache_retention: str | None = None,
        cache_enabled: bool = False,
        cache_ttl: str = "ephemeral",
        cache_namespace: str | None = None,
    ):
        def _factory(llm_config):
            return _provider_kwargs(
                llm_config,
                dispatch_mode=dispatch_mode,
                cache_retention=cache_retention,
                cache_enabled=cache_enabled,
                cache_ttl=cache_ttl,
                cache_namespace=cache_namespace,
            )

        return _factory

    brain_agent_config = config.agents["brain"]

    # Compute OpenAI cache retention early so it can be passed to client creation.
    _brain_cache_retention: str | None = None
    if (
        brain_agent_config.cache.enabled
        and isinstance(brain_agent_config.llm, OpenAIConfig)
        and brain_agent_config.cache.ttl == "24h"
    ):
        _brain_cache_retention = "24h"

    client = create_agent_client(
        brain_agent_config,
        retry_label="brain",
        provider_kwargs_factory=_provider_kwargs_factory(
            dispatch_mode="first_user_then_agent",
            cache_retention=_brain_cache_retention,
            cache_enabled=brain_agent_config.cache.enabled,
            cache_ttl=brain_agent_config.cache.ttl,
            cache_namespace="brain",
        ),
    )
    memory_sync_client = None
    if getattr(brain_agent_config.llm, "provider", "") == "openrouter":
        memory_sync_client = create_agent_client(
            brain_agent_config,
            retry_label="memory_sync",
            provider_kwargs_factory=_provider_kwargs_factory(
                dispatch_mode="first_user_then_agent",
                cache_enabled=brain_agent_config.cache.enabled,
                cache_ttl=brain_agent_config.cache.ttl,
                cache_namespace="memory_sync",
            ),
        )

    if "memory_editor" not in config.agents:
        _emit_pre_tui_message(
            console,
            "error",
            "Missing required agent config: agents.memory_editor",
        )
        return

    memory_editor_config = config.agents["memory_editor"]
    if not memory_editor_config.enabled:
        _emit_pre_tui_message(
            console,
            "error",
            "agents.memory_editor must be enabled.",
        )
        return

    memory_editor_client = create_agent_client(
        memory_editor_config,
        retry_label="memory_editor",
        provider_kwargs_factory=_provider_kwargs_factory(
            dispatch_mode="always_agent",
            cache_enabled=memory_editor_config.cache.enabled,
            cache_ttl=memory_editor_config.cache.ttl,
            cache_namespace="memory_editor",
        ),
    )

    try:
        memory_editor_prompt = workspace.get_system_prompt("memory_editor")
    except FileNotFoundError as e:
        _emit_pre_tui_message(
            console,
            "error",
            f"Failed to load memory_editor prompt: {e}",
        )
        return

    memory_editor_parse_retry: str | None = None
    try:
        memory_editor_parse_retry = workspace.get_agent_prompt(
            "memory_editor",
            "parse-retry",
            current_user=user_id,
        )
    except FileNotFoundError:
        pass

    memory_planner = MemoryEditPlanner(
        memory_editor_client,
        memory_editor_prompt,
        supports_response_schema=_agent_supports_response_schema(memory_editor_config),
        parse_retries=memory_editor_config.post_parse_retries,
        parse_retry_prompt=memory_editor_parse_retry,
    )
    memory_editor = MemoryEditor(
        commit_log=SessionCommitLog(),
        planner=memory_planner,
        warnings_config=config.tools.memory_edit.warnings,
    )

    timezone = config.app.timezone
    console.set_timezone(timezone)

    # Session persistence
    session_mgr = SessionManager(agent_os_dir / "session" / "brain")

    state_dir = agent_os_dir / "state"

    from ..agent.task_store import TaskStore
    from ..agent.note_store import NoteStore

    task_store = TaskStore(state_dir)
    note_store = NoteStore(state_dir)

    shared_state_store = None
    if config.context.common_ground.enabled:
        cache_path = state_dir / "shared_state.json"
        load_result = load_shared_state_cache(cache_path)
        shared_state_store = load_result.store
        shared_state_store.persist_enabled = config.context.common_ground.persist_cache
        if not load_result.loaded:
            stats = rebuild_shared_state_from_sessions(
                agent_os_dir / "session" / "brain",
                store=shared_state_store,
                scope_resolver=DEFAULT_SCOPE_RESOLVER,
            )
            try:
                shared_state_store.save()
            except Exception as e:
                _emit_pre_tui_message(
                    console,
                    "warning",
                    f"shared_state cache save failed: {e}",
                )
            if debug:
                console.print_debug(
                    "common-ground",
                    "replay rebuild "
                    f"sessions={stats.sessions_scanned} "
                    f"entries={stats.entries_scanned} "
                    f"sends={stats.send_message_successes_replayed}",
                )

    resume_id: str | None = None
    if resume is not None:
        # Resume flow
        if resume == "__continue__":
            sessions = session_mgr.list_recent(user_id=user_id, limit=1)
            if sessions:
                resume_id = sessions[0].session_id
        elif resume == "":
            sessions = session_mgr.list_recent(user_id=user_id)
            selected = pick_session(sessions)
            if not selected:
                return
            resume_id = selected.session_id
        else:
            resume_id = resume

    if resume_id is not None:
        messages = session_mgr.load(resume_id)
        conversation = Conversation(on_message=session_mgr.append_message)
        conversation.replace_messages(messages)
        # A killed process can leave tool calls without results on disk;
        # such history is rejected by provider APIs.
        repaired = conversation.remove_dangling_tool_calls()
        if repaired:
            session_mgr.rewrite_messages(conversation.get_messages())
            console.print_info(
                f"Removed {repaired} interrupted tool-call record(s)"
            )
        console.print_info(
            f"Resumed session {resume_id} ({len(messages)} messages)"
        )
    else:
        session_mgr.create(user_id, display_name)
        conversation = Conversation(on_message=session_mgr.append_message)

    client = wrap_llm_client_with_session_debug(
        client,
        sink=session_mgr,
        client_label="brain",
        provider=getattr(brain_agent_config.llm, "provider", None),
        model=getattr(brain_agent_config.llm, "model", None),
    )
    if memory_sync_client is not None:
        memory_sync_client = wrap_llm_client_with_session_debug(
            memory_sync_client,
            sink=session_mgr,
            client_label="memory_sync",
            provider=getattr(brain_agent_config.llm, "provider", None),
            model=getattr(brain_agent_config.llm, "model", None),
        )

    # Prompt cache: two mechanisms depending on provider.
    # Breakpoint providers use cache_control annotations on content blocks.
    # OpenAI uses automatic prefix caching + request-level prompt_cache_retention.
    brain_cache = brain_agent_config.cache
    brain_provider = brain_agent_config.llm.provider
    cache_ttl = resolve_breakpoint_cache_ttl(
        provider=brain_provider,
        enabled=brain_cache.enabled,
        configured_ttl=brain_cache.ttl,
    )
    builder = ContextBuilder(
        system_prompt=system_prompt,
        agent_os_dir=agent_os_dir,
        boot_files=config.context.boot_files,
        boot_files_as_tool=config.context.boot_files_as_tool,
        preserve_turns=config.context.preserve_turns,
        provider=brain_agent_config.llm.provider,
        cache_ttl=cache_ttl,
        format_reminders=config.features.format_reminders.model_dump(),
        decision_reminder=config.features.decision_reminder.model_dump(),
        send_message_batch_guidance=config.features.send_message_batch_guidance.enabled,
        fingerprint_boot_files=brain_cache.fingerprint.boot_files,
        fingerprint_boot_files_as_tool=brain_cache.fingerprint.boot_files_as_tool,
    )
    builder.reload_boot_files()

    # Restore render cache on resume so prompt cache prefix survives restart.
    if resume_id is not None:
        fp = builder.boot_fingerprint()
        cached = session_mgr.load_render_cache(fp)
        if cached is not None:
            conv_entries = conversation.get_messages()
            if len(cached) <= len(conv_entries):
                restored = builder.import_render_cache(
                    cached, list(conv_entries[: len(cached)])
                )
                if debug:
                    if restored:
                        console.print_debug(
                            "render-cache",
                            f"Restored {len(cached)} cached entries",
                        )
                    else:
                        console.print_debug(
                            "render-cache",
                            "Discarded stale render cache",
                        )
    bm25_search_instance = BM25MemorySearch(
        memory_dir=agent_os_dir / "memory",
        config=config.tools.memory_search.bm25,
    )

    # Vision agent initialization
    # use_own_vision_ability applies per active failover candidate: vision
    # models keep multimodal tools; non-vision fallbacks use the sub-agent path.
    _brain_llm_chain = [brain_agent_config.llm, *brain_agent_config.llm_fallbacks]
    brain_has_vision = brain_agent_config.llm.get_vision()
    _use_own_vision = brain_agent_config.use_own_vision_ability
    _chain_has_non_vision = any(not model.get_vision() for model in _brain_llm_chain)
    _own_vision_active = None
    if _use_own_vision and _chain_has_non_vision:
        from ..llm.failover import (
            llm_failover_key,
            preferred_candidate_supports_vision,
        )

        _vision_chain = [
            (llm_failover_key(model), model.get_vision())
            for model in _brain_llm_chain
        ]

        def _own_vision_active() -> bool:
            return preferred_candidate_supports_vision(_vision_chain)

    vision_agent_instance: VisionAgent | None = None
    _need_vision_agent = (
        not brain_has_vision
        or not _use_own_vision
        or _chain_has_non_vision
    )
    if _need_vision_agent and "vision" in config.agents and config.agents["vision"].enabled:
        vision_config = config.agents["vision"]
        vision_client = create_agent_client(
            vision_config,
            retry_label="vision",
            provider_kwargs_factory=_provider_kwargs_factory(
                dispatch_mode="always_agent",
                cache_enabled=vision_config.cache.enabled,
                cache_ttl=vision_config.cache.ttl,
                cache_namespace="vision",
            ),
        )
        try:
            vision_prompt = workspace.get_system_prompt("vision")
            model_fingerprint = json.dumps(
                vision_config.llm.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
            )
            vision_agent_instance = VisionAgent(
                vision_client,
                vision_prompt,
                cache_dir=agent_os_dir / "cache" / "vision",
                model_fingerprint=model_fingerprint,
            )
        except FileNotFoundError:
            pass

    skill_check_agent_instance: SkillCheckAgent | None = None
    skill_check_config = config.agents.get("skill_checker")
    if skill_check_config and skill_check_config.enabled:
        skill_check_client = create_agent_client(
            skill_check_config,
            retry_label="skill_checker",
            provider_kwargs_factory=_provider_kwargs_factory(
                dispatch_mode="always_agent",
                cache_enabled=skill_check_config.cache.enabled,
                cache_ttl=skill_check_config.cache.ttl,
                cache_namespace="skill_checker",
            ),
        )
        skill_check_client = wrap_llm_client_with_session_debug(
            skill_check_client,
            sink=session_mgr,
            client_label="skill_check",
            provider=getattr(skill_check_config.llm, "provider", None),
            model=getattr(skill_check_config.llm, "model", None),
        )
        try:
            skill_check_prompt = workspace.get_system_prompt("skill_checker")
            skill_check_agent_instance = SkillCheckAgent(
                skill_check_client,
                skill_check_prompt,
            )
        except FileNotFoundError:
            pass

    # Conscience agent initialization (post-turn tool-use auditor)
    from lincy.agent.conscience import ConscienceAgent

    conscience_agent_instance: ConscienceAgent | None = None
    conscience_config = config.agents.get("conscience")
    if conscience_config and conscience_config.enabled:
        conscience_client = create_agent_client(
            conscience_config,
            retry_label="conscience",
            provider_kwargs_factory=_provider_kwargs_factory(
                dispatch_mode="always_agent",
                cache_enabled=conscience_config.cache.enabled,
                cache_ttl=conscience_config.cache.ttl,
                cache_namespace="conscience",
            ),
        )
        conscience_client = wrap_llm_client_with_session_debug(
            conscience_client,
            sink=session_mgr,
            client_label="conscience",
            provider=getattr(conscience_config.llm, "provider", None),
            model=getattr(conscience_config.llm, "model", None),
        )
        conscience_agent_instance = ConscienceAgent(conscience_client)

    # GUI automation agent initialization
    gui_manager_instance: GUIManager | None = None
    gui_worker_instance: GUIWorker | None = None
    if "gui_manager" in config.agents and config.agents["gui_manager"].enabled:
        gm_config = config.agents["gui_manager"]
        gm_client = create_agent_client(
            gm_config,
            retry_label="gui_manager",
            provider_kwargs_factory=_provider_kwargs_factory(
                dispatch_mode="always_agent",
                cache_enabled=gm_config.cache.enabled,
                cache_ttl=gm_config.cache.ttl,
                cache_namespace="gui_manager",
            ),
        )
        from ..gui.ax_runtime import AXRuntimeError, ensure_binary
        from ..gui.mcp_client import MCPStdioClient

        try:
            gm_prompt = workspace.get_system_prompt("gui_manager")
        except FileNotFoundError:
            gm_prompt = ""
        ax_binary: str | None = None
        if gm_prompt:
            try:
                ax_binary = ensure_binary(
                    **{
                        k: v
                        for k, v in {
                            "repo": gm_config.ax.repo,
                            "commit": gm_config.ax.commit,
                        }.items()
                        if v
                    },
                    override_path=gm_config.ax.binary_path,
                )
            except AXRuntimeError as e:
                logger.error("GUI disabled, AX backend unavailable: %s", e)
        if gm_prompt and ax_binary:
            gui_session_store = GUISessionStore(agent_os_dir / "session" / "gui")

            def _gui_step_callback(
                tool_call, result, step, max_steps,
                elapsed_sec, total_elapsed_sec, worker_timing,
            ):
                console.print_gui_step(
                    tool_call, result, step, max_steps,
                    elapsed_sec, total_elapsed_sec,
                    worker_timing=worker_timing,
                    instruction_max_chars=gm_config.gui_instruction_max_chars,
                    text_max_chars=gm_config.gui_text_max_chars,
                    worker_result_max_chars=gm_config.gui_worker_result_max_chars,
                    result_max_chars=gm_config.gui_result_max_chars,
                )

            console.gui_intent_max_chars = gm_config.gui_intent_max_chars
            _ax_bin = ax_binary
            _ax_timeout = gm_config.ax.tool_timeout
            gui_manager_instance = GUIManager(
                gm_client,
                mcp_factory=lambda: MCPStdioClient(
                    [_ax_bin, "mcp"], timeout=_ax_timeout,
                ),
                system_prompt=gm_prompt,
                max_steps=gm_config.max_steps,
                session_store=gui_session_store,
                on_step=_gui_step_callback,
                is_cancel_requested=cancel_controller.is_requested,
                allow_wait_tool=gm_config.allow_wait_tool,
                step_delay_min=gm_config.step_delay_min,
                step_delay_max=gm_config.step_delay_max,
                keep_full_states=gm_config.ax.keep_full_states,
                stale_text_max_chars=gm_config.ax.stale_text_max_chars,
                max_tree_nodes=gm_config.ax.max_tree_nodes,
                max_tree_depth=gm_config.ax.max_tree_depth,
                tool_timeout=gm_config.ax.tool_timeout,
                cache_control=build_cache_control(
                    resolve_breakpoint_cache_ttl(
                        provider=getattr(gm_config.llm, "provider", ""),
                        enabled=gm_config.cache.enabled,
                        configured_ttl=gm_config.cache.ttl,
                    )
                ),
            )

        # Vision worker survives only as the describer behind
        # screenshot_by_subagent; the manager loop no longer uses it.
        gw_config = config.agents.get("gui_worker")
        if gw_config and gw_config.enabled:
            gw_client = create_agent_client(
                gw_config,
                retry_label="gui_worker",
                provider_kwargs_factory=_provider_kwargs_factory(
                    dispatch_mode="always_agent",
                    cache_enabled=gw_config.cache.enabled,
                    cache_ttl=gw_config.cache.ttl,
                    cache_namespace="gui_worker",
                ),
            )
            try:
                gw_prompt = workspace.get_system_prompt("gui_worker")
                gw_layout_prompt = workspace.get_agent_prompt("gui_worker", "layout")
                gw_describe_prompt = ""
                try:
                    gw_describe_prompt = workspace.get_agent_prompt("gui_worker", "describe")
                except FileNotFoundError:
                    pass
                gui_worker_instance = GUIWorker(
                    gw_client, gw_prompt,
                    screenshot_max_width=gm_config.screenshot_max_width,
                    screenshot_quality=gm_config.screenshot_quality,
                    layout_prompt=gw_layout_prompt,
                    describe_prompt=gw_describe_prompt,
                )
            except FileNotFoundError:
                pass

    # Screenshot settings (from gui_manager config if available)
    _gm_cfg = config.agents.get("gui_manager")
    _ss_max_width = _gm_cfg.screenshot_max_width if _gm_cfg else 1280
    _ss_quality = _gm_cfg.screenshot_quality if _gm_cfg else 80

    gui_lock = threading.Lock() if gui_manager_instance is not None else None
    contact_map = ContactMap(state_dir)
    thread_registry = ThreadRegistry(state_dir)
    _env = dotenv_values()

    # === Gmail adapter (optional, requires OAuth credentials in .env) ===
    # Created before setup_tools so attachments_dir can be added to allowed_paths.
    gmail_adapter = None
    _gmail_cfg = config.channels.gmail
    if _gmail_cfg.enabled:
        _gmail_cid = _env.get("GMAIL_CLIENT_ID") or os.environ.get("GMAIL_CLIENT_ID")
        _gmail_sec = _env.get("GMAIL_CLIENT_SECRET") or os.environ.get("GMAIL_CLIENT_SECRET")
        _gmail_tok = _env.get("GMAIL_REFRESH_TOKEN") or os.environ.get("GMAIL_REFRESH_TOKEN")
        if _gmail_cid and _gmail_sec and _gmail_tok:
            from ..agent.adapters.gmail import GmailAdapter

            gmail_adapter = GmailAdapter(
                client_id=_gmail_cid,
                client_secret=_gmail_sec,
                refresh_token=_gmail_tok,
                contact_map=contact_map,
                thread_registry=thread_registry,
                thread_max_age_days=_gmail_cfg.thread_max_age_days,
                poll_interval=_gmail_cfg.poll_interval,
                max_age_minutes=_gmail_cfg.max_age_minutes,
                ignore_senders=_gmail_cfg.ignore_senders,
            )

    # === Discord adapter (optional, requires token) ===
    discord_adapter = None
    discord_history_store = None
    _discord_cfg = config.channels.discord
    if _discord_cfg.enabled:
        _discord_token = _env.get("DISCORD_TOKEN") or os.environ.get("DISCORD_TOKEN")
        if _discord_token:
            from ..agent.adapters.discord import DiscordAdapter
            from ..agent.discord_history import DiscordHistoryStore

            discord_history_store = DiscordHistoryStore(state_dir)
            discord_adapter = DiscordAdapter(
                token=_discord_token,
                contact_map=contact_map,
                thread_registry=thread_registry,
                config=_discord_cfg,
                history_store=discord_history_store,
            )

    extra_allowed_paths: list[str] = []
    if gmail_adapter is not None:
        extra_allowed_paths.append(gmail_adapter.attachments_dir)
    if discord_adapter is not None:
        extra_allowed_paths.extend(discord_adapter.history_store.allowed_paths)

    # web_fetch summarizer (secondary LLM for content extraction)
    _wf_summarizer = None
    if config.tools.web_fetch.enabled and config.tools.web_fetch.summarize_with_llm:
        _wf_agent_key = "web_fetch_summarizer"
        _wf_agent_cfg = config.agents.get(_wf_agent_key)
        if _wf_agent_cfg is not None and _wf_agent_cfg.enabled:
            _wf_summarizer = create_agent_client(
                _wf_agent_cfg,
                retry_label="web_fetch_summarizer",
                provider_kwargs_factory=_provider_kwargs_factory(
                    dispatch_mode="always_agent",
                    cache_enabled=_wf_agent_cfg.cache.enabled,
                    cache_ttl=_wf_agent_cfg.cache.ttl,
                    cache_namespace="web_fetch_summarizer",
                ),
            )
            _wf_summarizer = wrap_llm_client_with_session_debug(
                _wf_summarizer,
                sink=session_mgr,
                client_label="web_fetch_summarizer",
                provider=getattr(_wf_agent_cfg.llm, "provider", None),
                model=getattr(_wf_agent_cfg.llm, "model", None),
            )

    _on_shell_line = console.print_shell_stream_line
    registry, all_allowed_paths, shell_executor = setup_tools(
        config.tools,
        agent_os_dir,
        memory_editor=memory_editor,
        bm25_search=bm25_search_instance,
        brain_has_vision=brain_has_vision,
        use_own_vision_ability=_use_own_vision,
        own_vision_active=_own_vision_active,
        vision_agent=vision_agent_instance,
        gui_manager=gui_manager_instance,
        gui_worker=gui_worker_instance,
        gui_lock=gui_lock,
        screenshot_max_width=_ss_max_width,
        screenshot_quality=_ss_quality,
        contact_map=contact_map,
        extra_allowed_paths=extra_allowed_paths,
        on_shell_stdout_line=_on_shell_line,
        is_shell_cancel_requested=cancel_controller.is_requested,
        web_fetch_summarizer=_wf_summarizer,
    )
    memory_edit_allow_failure = config.tools.memory_edit.allow_failure
    commands = CommandHandler(console)

    if resume is not None:
        console.print_resume_history(
            conversation.get_messages(),
            replay_turns=config.tui.replay_turns,
            show_tool_calls=config.tui.show_tool_calls,
        )

    # Periodic memory backup
    memory_backup_mgr = None
    if config.maintenance.backup.enabled:
        memory_backup_mgr = MemoryBackupManager(agent_os_dir, config.maintenance.backup)

    # === Persistent queue ===
    pqueue = PersistentPriorityQueue(
        agent_os_dir / "queue",
        discard_channels={"cli"},
    )

    # === Build AgentCore ===
    conversation_compaction_client = None
    if (
        getattr(config.features.codex_remote_compaction, "enabled", False)
        and brain_agent_config.llm.provider == "codex"
        and hasattr(client, "compact_messages")
    ):
        conversation_compaction_client = client

    agent = AgentCore(
        client=client,
        conversation=conversation,
        builder=builder,
        registry=registry,
        excluded_tools=frozenset(brain_agent_config.excluded_tools),
        ui_sink=sink_for_agents,
        workspace=workspace,
        config=config,
        agent_os_dir=agent_os_dir,
        user_id=user_id,
        session_mgr=session_mgr,
        display_name=display_name,
        memory_edit_allow_failure=memory_edit_allow_failure,
        memory_backup_mgr=memory_backup_mgr,
        queue=pqueue,
        turn_cancel=cancel_controller,
        shared_state_store=shared_state_store,
        scope_resolver=DEFAULT_SCOPE_RESOLVER,
        memory_sync_client=memory_sync_client,
        conversation_compaction_client=conversation_compaction_client,
        brain_prompt_policy=brain_prompt_policy,
        copilot_runtime=copilot_runtime if brain_agent_config.llm.provider == "copilot" else None,
        ui_debug=debug,
        ui_show_tool_use=config.tui.show_tool_use,
        ui_timezone=timezone,
        ui_gui_intent_max_chars=getattr(console, "gui_intent_max_chars", None),
        task_store=task_store,
        note_store=note_store,
        skill_check_agent=skill_check_agent_instance,
        conscience_agent=conscience_agent_instance,
    )

    def _token_status_text() -> str:
        return agent.get_token_status_text()

    if hasattr(console, "set_ctx_status_provider"):
        console.set_ctx_status_provider(_token_status_text)
    controller.ctx_provider = _token_status_text

    # === CLI adapter ===
    cli_adapter = CLIAdapter(
        ui_sink=sink_for_agents,
        commands=commands,
        session_mgr=session_mgr,
        conversation=conversation,
        builder=builder,
        workspace=workspace,
        agent_os_dir=agent_os_dir,
        user_id=user_id,
        display_name=display_name,
        cancel_controller=cancel_controller,
    )
    agent.register_adapter(cli_adapter)

    web_adapter = None
    if config.channels.web.enabled:
        from ..agent.adapters.web import WebAdapter

        web_adapter = WebAdapter(
            events_path=agent_os_dir / "state" / "web_chat" / "events.jsonl",
            history_limit=config.channels.web.history_limit,
        )
        agent.register_adapter(web_adapter)
        if debug:
            console.print_debug("web", "Web Chat adapter registered")

    if gmail_adapter is not None:
        agent.register_adapter(gmail_adapter)
        if debug:
            console.print_debug("gmail", "Gmail adapter registered")

    if discord_adapter is not None:
        agent.register_adapter(discord_adapter)
        if debug:
            console.print_debug("discord", "Discord adapter registered")

    # === Scheduler adapter (heartbeat, optional) ===
    if config.heartbeat.enabled:
        from ..agent.adapters.scheduler import SchedulerAdapter

        upgrade_msg = migration_result.format_startup_message() if migration_result else ""
        scheduler_adapter = SchedulerAdapter(
            interval=config.heartbeat.interval,
            enqueue_startup=config.heartbeat.enqueue_startup,
            enqueue_upgrade_notice=config.heartbeat.enqueue_upgrade_notice,
            upgrade_message=upgrade_msg,
            quiet_windows=config.heartbeat.parsed_quiet_windows(),
        )
        agent.register_adapter(scheduler_adapter)
        if debug:
            console.print_debug("scheduler", "Scheduler adapter registered")

    # === send_message tool (registered after adapters are available) ===
    from ..agent.turn_context import TurnContext
    from ..tools.builtin.send_message import (
        build_send_message_definition,
        create_send_message,
    )

    turn_context = TurnContext()
    registry.register(
        "send_message",
        create_send_message(
            adapters=agent.adapters,
            turn_context=turn_context,
            contact_map=contact_map,
            allowed_paths=all_allowed_paths,
            agent_os_dir=agent_os_dir,
            shared_state_store=shared_state_store,
            scope_resolver=DEFAULT_SCOPE_RESOLVER,
            pending_scope_check=pqueue.has_ready_pending_inbound_for_scope,
        ),
        build_send_message_definition(
            batch_guidance_enabled=config.features.send_message_batch_guidance.enabled,
        ),
    )
    agent.turn_context = turn_context

    if discord_history_store is not None:
        from ..tools.builtin.get_channel_history import (
            GET_CHANNEL_HISTORY_DEFINITION,
            create_get_channel_history,
        )

        registry.register(
            "get_channel_history",
            create_get_channel_history(
                discord_history_store,
                contact_map,
                turn_context,
            ),
            GET_CHANNEL_HISTORY_DEFINITION,
        )

    # === gui_task tool (registered after queue for background execution) ===
    if gui_manager_instance is not None:
        from ..gui.tool_adapter import GUI_TASK_DEFINITION, create_gui_task

        registry.register(
            "gui_task",
            create_gui_task(
                gui_manager_instance,
                gui_lock=gui_lock,
                agent_os_dir=agent_os_dir,
                queue=pqueue,
            ),
            GUI_TASK_DEFINITION,
        )

    from ..tools.builtin.shell_task import (
        SHELL_TASK_DEFINITION,
        ShellTaskManager,
        create_shell_task,
    )

    shell_task_manager = ShellTaskManager(
        max_concurrent=config.tools.shell.task_max_concurrency,
        ui_sink=sink_for_agents,
    )
    commands.set_shell_task_manager(shell_task_manager)

    registry.register(
        "shell_task",
        create_shell_task(
            queue=pqueue,
            ui_sink=sink_for_agents,
            cwd_provider=lambda: shell_executor.cwd,
            agent_os_dir=agent_os_dir,
            blacklist=config.tools.shell.blacklist,
            timeout=config.tools.shell.timeout,
            export_env=config.tools.shell.export_env,
            handoff=config.tools.shell.handoff,
            manager=shell_task_manager,
        ),
        SHELL_TASK_DEFINITION,
    )

    # === schedule_action tool (always available when queue exists) ===
    from ..tools.builtin.schedule_action import (
        SCHEDULE_ACTION_DEFINITION,
        create_schedule_action,
    )

    registry.register(
        "schedule_action",
        create_schedule_action(pqueue),
        SCHEDULE_ACTION_DEFINITION,
    )

    # === agent_task + agent_note tools ===
    from ..tools.builtin.agent_task import (
        AGENT_TASK_DEFINITION,
        create_agent_task,
    )
    from ..tools.builtin.agent_note import (
        AGENT_NOTE_DEFINITION,
        create_agent_note,
    )

    registry.register(
        "agent_task",
        create_agent_task(task_store, pqueue),
        AGENT_TASK_DEFINITION,
    )
    registry.register(
        "agent_note",
        create_agent_note(
            note_store,
            max_value_chars=config.tools.agent_note.max_value_chars,
            max_notes=config.tools.agent_note.max_notes,
        ),
        AGENT_NOTE_DEFINITION,
    )
    registry.add_side_effect_tools(frozenset({"agent_task", "agent_note"}))

    # === worker subagent tool ===
    worker_config = config.agents.get("worker")
    if worker_config is not None and worker_config.enabled:
        from ..agent.tool_setup import build_worker_file_tools
        from ..llm.agent_factory import create_agent_client as _create_worker_client
        from ..worker import WORKER_TOOL_DEFINITION, WorkerRunner, create_worker_tool
        from ..worker.tool_adapter import WorkerCounter

        _worker_client = _create_worker_client(
            worker_config,
            retry_label="worker",
            provider_kwargs_factory=_provider_kwargs_factory(
                dispatch_mode="always_agent",
                cache_enabled=worker_config.cache.enabled,
                cache_ttl=worker_config.cache.ttl,
                cache_namespace="worker",
            ),
        )
        _worker_prompt = workspace.get_system_prompt("worker")
        _worker_cache_ctrl = build_cache_control(
            resolve_breakpoint_cache_ttl(
                provider=getattr(worker_config.llm, "provider", ""),
                enabled=worker_config.cache.enabled,
                configured_ttl=worker_config.cache.ttl,
            )
        )

        # Always exclude worker itself to prevent recursion.
        _excluded = frozenset(worker_config.excluded_tools) | {"worker"}
        _worker_runner = WorkerRunner(
            _worker_client,
            registry,
            _excluded,
            _worker_prompt,
            max_turns=worker_config.max_turns,
            max_context_tokens=worker_config.max_context_tokens,
            cache_control=_worker_cache_ctrl,
            sink=session_mgr,
            provider=getattr(worker_config.llm, "provider", None),
            model=getattr(worker_config.llm, "model", None),
            ui_console=console,
            tool_overrides=build_worker_file_tools(
                all_allowed_paths, agent_os_dir
            ),
        )
        _worker_counter = WorkerCounter()
        registry.register(
            "worker",
            create_worker_tool(
                _worker_runner,
                agent_os_dir,
                _worker_counter,
                queue=pqueue,
                max_concurrent=worker_config.task_max_concurrency,
            ),
            WORKER_TOOL_DEFINITION,
        )

    # All registrations are done by now, so unknown exclusions are real typos.
    validate_excluded_tools(registry, config.agents)

    app = ChatTextualApp(controller=controller, event_sink=ui_sink)

    # Control API (optional, for supervisor integration)
    if config.app.control.enabled:
        from ..control import ControlServer

        def _shutdown_from_control() -> None:
            # /shutdown must terminate the full chat-cli process, not just the agent thread.
            agent.request_shutdown()
            try:
                app.call_from_thread(app.exit)
            except RuntimeError:
                # If Textual isn't running yet, the queued shutdown sentinel still stops AgentCore.
                pass

        # Remote web TUI: default channel is cli (same as Textual). ``web`` is not
        # a send option; the Agent page mirrors agent UI events, not web bubbles.
        _NON_SEND_CHANNELS = frozenset({"system", "web"})

        def _list_send_channels() -> list[str]:
            names = [
                name
                for name in agent.adapters
                if name not in _NON_SEND_CHANNELS
            ]
            # cli first, then stable alpha for the rest
            names.sort(key=lambda n: (n != "cli", n))
            return names

        def _submit_tui_message(content: str, channel: str) -> dict:
            from ..agent.schema import InboundMessage

            selected = (channel or "cli").strip().lower() or "cli"
            allowed = _list_send_channels()
            if selected not in allowed:
                raise ValueError(
                    f"unsupported channel: {selected}; "
                    f"choose one of: {', '.join(allowed)}"
                )
            if selected == "cli":
                cli_adapter.submit_remote(content)
                return {"status": "accepted", "channel": "cli"}

            text = content.strip()
            if not text:
                raise ValueError("content is required")
            agent.enqueue(
                InboundMessage(
                    channel=selected,
                    content=text,
                    priority=0,
                    sender=user_id,
                    metadata={"source": "web_tui"},
                )
            )
            return {"status": "accepted", "channel": selected}

        control_server = ControlServer(
            host=config.app.control.host,
            port=config.app.control.port,
            shutdown_fn=_shutdown_from_control,
            new_session_fn=agent.request_new_session,
            reload_fn=agent.request_reload,
            tui_submit_fn=_submit_tui_message,
            channels_fn=_list_send_channels,
        )
        control_server.start()

    if resume is None:
        console.print_welcome()

    controller.on_submit = cli_adapter.submit_input
    controller.on_history_request = cli_adapter.select_recent_input
    controller.on_history_options = cli_adapter.list_recent_inputs
    controller.on_history_select = cli_adapter.select_recent_input_by_index
    controller.on_exit_request = lambda: agent.request_shutdown(graceful=False)

    ui_sink.set_on_emit(app.wake_ui_event_drain)
    controller.refresh_ctx_status()
    app.drain_ui_events()

    agent_thread = threading.Thread(target=agent.run, name="agent-core", daemon=True)
    agent_thread.start()
    try:
        app.run()
    finally:
        shell_task_manager.shutdown()
        if agent_thread.is_alive():
            agent.request_shutdown(graceful=False)
            agent_thread.join(timeout=5)
