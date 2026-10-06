"""Assemble the agent from validated inputs.

``build_agent`` only constructs objects: it reads config, prompts, session
history and caches, but must not write workspace state, open network
connections or start threads, so ``lincy check`` can build and discard an
agent safely. Writes that assembly used to do inline are deferred to
``BuiltAgent.start()``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values

from ..brain_prompt_policy import BrainPromptPolicy
from ..context import ContextBuilder, Conversation
from ..context.cache_breakpoints import build_cache_control, resolve_breakpoint_cache_ttl
from ..core.schema import AppConfig, OpenAIConfig
from ..gui import GUIManager, GUISessionStore, GUIWorker
from ..llm import create_agent_client
from ..memory import BM25MemorySearch, MemoryEditor, MemoryEditPlanner, SessionCommitLog
from ..memory.backup import MemoryBackupManager
from ..session import SessionManager
from ..session.debug_client import wrap_llm_client_with_session_debug
from ..tools import VisionAgent
from ..tools.builtin.shell_task import SHELL_KEYS, ShellTaskManager
from ..ui.sink import FanoutUiSink
from ..workspace import WorkspaceManager
from .adapters.console import ConsoleAdapter
from .compactor_agent import CompactorAgent
from .contact_map import ContactMap
from .core import AgentCore
from .handle import (
    AgentHandle,
    AgentState,
    ExitReason,
    InvalidRequest,
    ShellSessionNotFound,
    UnsupportedChannel,
)
from .queue import PersistentPriorityQueue
from .schema import InboundMessage
from .scope import DEFAULT_SCOPE_RESOLVER
from .shared_state import SharedStateStore
from .shared_state import load_or_init as load_shared_state_cache
from .shared_state_replay import rebuild_shared_state_from_sessions
from .thread_registry import ThreadRegistry
from .tool_setup import setup_tools, validate_excluded_tools
from .turn_cancel import TurnCancelController
from .ui_event_console import UiEventConsole
from .ui_event_stream import UiEventExportSink, UiEventStore


logger = logging.getLogger(__name__)


class BuildError(Exception):
    """Agent assembly failed; the message is meant for the operator."""


@dataclass(frozen=True)
class BuildInputs:
    """Everything build needs, produced by the host's validate stage."""

    config: AppConfig
    agent_os_dir: Path
    user_id: str
    display_name: str
    resume_id: str | None
    ax_binary: str | None
    upgrade_message: str


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


class _Handle:
    """AgentHandle over the concrete agent objects; the host never sees them."""

    def __init__(
        self,
        *,
        core: AgentCore,
        console_adapter: ConsoleAdapter,
        cancel_controller: TurnCancelController,
        shell_task_manager: ShellTaskManager,
    ) -> None:
        self._core = core
        self._console_adapter = console_adapter
        self._cancel = cancel_controller
        self._shell = shell_task_manager
        self._ready = threading.Event()
        self._stopping = threading.Event()

    def mark_ready(self) -> None:
        self._ready.set()

    def state(self) -> AgentState:
        if self._stopping.is_set():
            return AgentState.STOPPING
        if not self._ready.is_set():
            return AgentState.STARTING
        if self._core.is_busy():
            return AgentState.BUSY
        return AgentState.READY

    def session_id(self) -> str | None:
        return self._core.session_mgr.current_session_id

    def channels(self) -> list[str]:
        names = [name for name in self._core.adapters if name != "system"]
        names.sort(key=lambda name: (name != "cli", name))
        return names

    def submit(self, content: str, channel: str = "cli") -> None:
        text = content.strip()
        if not text:
            raise InvalidRequest("content is required")
        allowed = self.channels()
        if channel not in allowed:
            raise UnsupportedChannel(
                f"unsupported channel: {channel}; choose one of: {', '.join(allowed)}"
            )
        if channel == "cli":
            self._console_adapter.submit(text)
            return
        self._core.enqueue(
            InboundMessage(
                channel=channel,
                content=text,
                priority=0,
                sender=self._core.user_id,
                metadata={"source": "web_console"},
            )
        )

    def cancel_turn(self) -> None:
        self._cancel.request()

    def request_new_session(self) -> None:
        self._core.request_new_session()

    def request_compact(self) -> None:
        self._core.request_compact()

    def request_clear(self) -> None:
        self._core.request_clear()

    def request_reload(self, target: Literal["all", "system-prompt"]) -> None:
        if target == "all":
            self._core.request_reload()
        elif target == "system-prompt":
            self._core.request_reload_system_prompt()
        else:
            raise InvalidRequest(
                f"unknown reload target: {target}; choose one of: all, system-prompt"
            )

    def request_shutdown(self, *, graceful: bool = True) -> None:
        self._stopping.set()
        self._core.request_shutdown(graceful=graceful)

    def request_restart(self) -> None:
        self._stopping.set()
        self._core.request_restart()

    def token_status(self) -> str:
        return self._core.get_token_status_text()

    def shell_sessions(self) -> list[dict]:
        return self._shell.list_sessions()

    def shell_send(self, session_id: str, *, text: str | None, key: str | None) -> str:
        if (text is None) == (key is None):
            raise InvalidRequest("exactly one of text or key is required")
        if key is not None and key not in SHELL_KEYS:
            raise InvalidRequest(
                f"unsupported key: {key}; choose one of: {', '.join(SHELL_KEYS)}"
            )
        if not self._shell.has_session(session_id):
            raise ShellSessionNotFound(f"shell session {session_id} was not found")
        if text is not None:
            return self._shell.send_input(text, session_id=session_id)
        return self._shell.send_key(key, session_id=session_id)

    def shell_cancel(self, session_id: str) -> str:
        if not self._shell.has_session(session_id):
            raise ShellSessionNotFound(f"shell session {session_id} was not found")
        return self._shell.cancel_session(session_id=session_id)


@dataclass
class _Startup:
    """State build prepared for the writes deferred to BuiltAgent.start()."""

    inputs: BuildInputs
    console: UiEventConsole
    queue: PersistentPriorityQueue
    session_mgr: SessionManager
    conversation: Conversation
    resumed_message_count: int
    repaired_tool_calls: int
    shared_state_store: SharedStateStore | None
    rebuild_shared_state: bool
    handle: _Handle


@dataclass
class BuiltAgent:
    core: AgentCore
    handle: AgentHandle
    ui_event_store: UiEventStore
    shell_task_manager: ShellTaskManager
    _startup: _Startup

    def start(self) -> None:
        """Perform the side effects build deferred, in dependency order."""
        startup = self._startup
        inputs = startup.inputs
        console = startup.console
        # Rotate first so every event emitted below lands in this run's file.
        self.ui_event_store.rotate_on_start()
        startup.queue.recover()

        if startup.rebuild_shared_state:
            store = startup.shared_state_store
            stats = rebuild_shared_state_from_sessions(
                inputs.agent_os_dir / "session" / "brain",
                store=store,
                scope_resolver=DEFAULT_SCOPE_RESOLVER,
            )
            try:
                store.save()
            except Exception as e:
                logger.warning("shared_state cache save failed: %s", e)
                console.print_warning(f"shared_state cache save failed: {e}")
            console.print_debug(
                "common-ground",
                "replay rebuild "
                f"sessions={stats.sessions_scanned} "
                f"entries={stats.entries_scanned} "
                f"sends={stats.send_message_successes_replayed}",
            )

        if inputs.resume_id is None:
            startup.session_mgr.create(inputs.user_id, inputs.display_name)
            return

        startup.session_mgr.mark_active()
        # A killed process can leave tool calls without results on disk;
        # build already dropped them in memory, persist the repaired history.
        if startup.repaired_tool_calls:
            startup.session_mgr.rewrite_messages(startup.conversation.get_messages())
            console.print_info(
                f"Removed {startup.repaired_tool_calls} interrupted tool-call record(s)"
            )
        console.print_info(
            f"Resumed session {inputs.resume_id} ({startup.resumed_message_count} messages)"
        )
        console.print_resume_history(
            startup.conversation.get_messages(),
            replay_turns=inputs.config.ui.replay_turns,
            show_tool_calls=inputs.config.ui.show_tool_calls,
        )

    def run(self) -> ExitReason:
        """Block on the agent loop until shutdown or restart."""
        self._startup.handle.mark_ready()
        return self.core.run()

    def close(self) -> None:
        self.shell_task_manager.shutdown()


def build_agent(inputs: BuildInputs) -> BuiltAgent:
    """Construct the full agent graph; raise BuildError on operator-fixable problems."""
    config = inputs.config
    agent_os_dir = inputs.agent_os_dir
    user_id = inputs.user_id
    display_name = inputs.display_name
    resume_id = inputs.resume_id

    ui_event_store = UiEventStore(agent_os_dir / "state" / "ui_events" / "events.jsonl")
    ui_sink = FanoutUiSink((UiEventExportSink(ui_event_store),))
    cancel_controller = TurnCancelController(ui_sink=ui_sink)

    workspace = WorkspaceManager(agent_os_dir)
    console = UiEventConsole(ui_sink)

    brain_prompt_policy = BrainPromptPolicy(
        kernel_dir=workspace.kernel_dir,
        config=config,
    )
    try:
        system_prompt = workspace.get_system_prompt("brain")
        system_prompt = system_prompt.replace("{agent_os_dir}", str(agent_os_dir))
        system_prompt = brain_prompt_policy.resolve(system_prompt)
    except FileNotFoundError as e:
        raise BuildError(f"Failed to load system prompt: {e}") from e

    debug = config.ui.debug
    console.set_debug(debug)
    console.set_current_user(user_id)
    console.set_show_tool_use(config.ui.show_tool_use)
    # Surface LLM retry attempts in normal UI (not only debug mode).
    _install_llm_retry_ui_handler(console)

    # Session persistence
    session_mgr = SessionManager(agent_os_dir / "session" / "brain")

    def _provider_kwargs_factory(*, cache_retention: str | None = None):
        def _factory(llm_config):
            kwargs: dict[str, object] = {}
            if isinstance(llm_config, OpenAIConfig) and cache_retention:
                kwargs["prompt_cache_retention"] = cache_retention
            return kwargs

        return _factory

    def _build_subagent_client(
        name: str,
        agent_config,
        *,
        cache_retention: str | None = None,
        session_debug_label: str | None = None,
    ):
        """Build an agent client with its cache namespace and optional debug sink."""
        client = create_agent_client(
            agent_config,
            retry_label=name,
            provider_kwargs_factory=_provider_kwargs_factory(
                cache_retention=cache_retention,
            ),
        )
        if session_debug_label is not None:
            client = wrap_llm_client_with_session_debug(
                client,
                sink=session_mgr,
                client_label=session_debug_label,
                provider=getattr(agent_config.llm, "provider", None),
                model=getattr(agent_config.llm, "model", None),
            )
        return client

    def _load_agent_prompt(name: str) -> str | None:
        """Load an optional subagent prompt; a missing file disables that subagent."""
        try:
            return workspace.get_system_prompt(name)
        except FileNotFoundError:
            return None

    brain_agent_config = config.agents["brain"]

    # OpenAI uses request-level prompt_cache_retention instead of cache_control blocks.
    brain_cache_retention: str | None = None
    if (
        brain_agent_config.cache.enabled
        and isinstance(brain_agent_config.llm, OpenAIConfig)
        and brain_agent_config.cache.ttl == "24h"
    ):
        brain_cache_retention = "24h"

    client = _build_subagent_client(
        "brain",
        brain_agent_config,
        cache_retention=brain_cache_retention,
        session_debug_label="brain",
    )
    memory_sync_client = None
    if getattr(brain_agent_config.llm, "provider", "") == "openrouter":
        memory_sync_client = _build_subagent_client(
            "memory_sync",
            brain_agent_config,
            session_debug_label="memory_sync",
        )

    if "memory_editor" not in config.agents:
        raise BuildError("Missing required agent config: agents.memory_editor")

    memory_editor_config = config.agents["memory_editor"]
    if not memory_editor_config.enabled:
        raise BuildError("agents.memory_editor must be enabled.")

    memory_editor_client = _build_subagent_client("memory_editor", memory_editor_config)
    memory_editor_prompt = _load_agent_prompt("memory_editor")
    if memory_editor_prompt is None:
        raise BuildError("Failed to load memory_editor prompt")

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

    if config.maintenance.curate.enabled and config.agents.get("worker") is None:
        logger.warning("Memory curation is enabled but agents.worker is missing")

    timezone = config.app.timezone
    console.set_timezone(timezone)

    state_dir = agent_os_dir / "state"

    from .note_store import NoteStore
    from .task_store import TaskStore

    task_store = TaskStore(state_dir)
    note_store = NoteStore(state_dir)

    shared_state_store = None
    rebuild_shared_state = False
    if config.context.common_ground.enabled:
        load_result = load_shared_state_cache(state_dir / "shared_state.json")
        shared_state_store = load_result.store
        shared_state_store.persist_enabled = config.context.common_ground.persist_cache
        rebuild_shared_state = not load_result.loaded

    repaired_tool_calls = 0
    resumed_message_count = 0
    conversation = Conversation(on_message=session_mgr.append_message)
    if resume_id is not None:
        try:
            messages = session_mgr.load(resume_id)
        except FileNotFoundError as e:
            raise BuildError(str(e)) from e
        resumed_message_count = len(messages)
        conversation.replace_messages(messages)
        # In memory only; start() rewrites the session file. Done here so the
        # render cache below is matched against the repaired history.
        repaired_tool_calls = conversation.remove_dangling_tool_calls()

    # Prompt cache: two mechanisms depending on provider.
    # Breakpoint providers use cache_control annotations on content blocks.
    # OpenAI uses automatic prefix caching + request-level prompt_cache_retention.
    brain_cache = brain_agent_config.cache
    cache_ttl = resolve_breakpoint_cache_ttl(
        provider=brain_agent_config.llm.provider,
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
        fingerprint_boot_files=brain_cache.fingerprint.boot_files,
        fingerprint_boot_files_as_tool=brain_cache.fingerprint.boot_files_as_tool,
    )
    builder.reload_boot_files()

    # Restore render cache on resume so prompt cache prefix survives restart.
    if resume_id is not None:
        cached = session_mgr.load_render_cache(builder.boot_fingerprint())
        if cached is not None:
            conv_entries = conversation.get_messages()
            if len(cached) <= len(conv_entries):
                restored = builder.import_render_cache(
                    cached, list(conv_entries[: len(cached)])
                )
                if restored:
                    logger.debug("Restored %d cached render entries", len(cached))
                else:
                    logger.debug("Discarded stale render cache")

    bm25_search_instance = BM25MemorySearch(
        memory_dir=agent_os_dir / "memory",
        config=config.tools.memory_search.bm25,
    )

    # Vision agent initialization
    # use_own_vision_ability applies per active failover candidate: vision
    # models keep multimodal tools; non-vision fallbacks use the sub-agent path.
    brain_llm_chain = [brain_agent_config.llm, *brain_agent_config.llm_fallbacks]
    brain_has_vision = brain_agent_config.llm.get_vision()
    use_own_vision = brain_agent_config.use_own_vision_ability
    chain_has_non_vision = any(not model.get_vision() for model in brain_llm_chain)
    own_vision_active = None
    if use_own_vision and chain_has_non_vision:
        from ..llm.failover import (
            llm_failover_key,
            preferred_candidate_supports_vision,
        )

        vision_chain = [
            (llm_failover_key(model), model.get_vision())
            for model in brain_llm_chain
        ]

        def own_vision_active() -> bool:
            return preferred_candidate_supports_vision(vision_chain)

    vision_agent_instance: VisionAgent | None = None
    need_vision_agent = (
        not brain_has_vision
        or not use_own_vision
        or chain_has_non_vision
    )
    if need_vision_agent and "vision" in config.agents and config.agents["vision"].enabled:
        vision_config = config.agents["vision"]
        vision_client = _build_subagent_client("vision", vision_config)
        vision_prompt = _load_agent_prompt("vision")
        if vision_prompt is not None:
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

    # GUI automation agent initialization
    gui_manager_instance: GUIManager | None = None
    gui_worker_instance: GUIWorker | None = None
    if "gui_manager" in config.agents and config.agents["gui_manager"].enabled:
        gm_config = config.agents["gui_manager"]
        gm_client = _build_subagent_client("gui_manager", gm_config)
        from ..gui.mcp_client import MCPStdioClient

        gm_prompt = _load_agent_prompt("gui_manager") or ""
        ax_binary = inputs.ax_binary
        if gm_prompt and ax_binary is None:
            logger.error("GUI disabled, AX backend unavailable")
        if gm_prompt and ax_binary is not None:
            gui_session_store = GUISessionStore(agent_os_dir / "session" / "gui")

            def _gui_step_callback(
                tool_call, result, step, max_steps,
                elapsed_sec, total_elapsed_sec, worker_timing,
            ):
                console.print_gui_step(
                    tool_call, result, step, max_steps,
                    elapsed_sec, total_elapsed_sec,
                    worker_timing=worker_timing,
                )

            ax_timeout = gm_config.ax.tool_timeout
            gui_manager_instance = GUIManager(
                gm_client,
                mcp_factory=lambda: MCPStdioClient(
                    [ax_binary, "mcp"], timeout=ax_timeout,
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
            gw_client = _build_subagent_client("gui_worker", gw_config)
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
    gm_cfg = config.agents.get("gui_manager")
    ss_max_width = gm_cfg.screenshot_max_width if gm_cfg else 1280
    ss_quality = gm_cfg.screenshot_quality if gm_cfg else 80

    gui_lock = threading.Lock() if gui_manager_instance is not None else None
    contact_map = ContactMap(state_dir)
    thread_registry = ThreadRegistry(state_dir)
    env = dotenv_values()

    # === Gmail adapter (optional, requires OAuth credentials in .env) ===
    # Created before setup_tools so attachments_dir can be added to allowed_paths.
    gmail_adapter = None
    gmail_cfg = config.channels.gmail
    if gmail_cfg.enabled:
        gmail_cid = env.get("GMAIL_CLIENT_ID") or os.environ.get("GMAIL_CLIENT_ID")
        gmail_sec = env.get("GMAIL_CLIENT_SECRET") or os.environ.get("GMAIL_CLIENT_SECRET")
        gmail_tok = env.get("GMAIL_REFRESH_TOKEN") or os.environ.get("GMAIL_REFRESH_TOKEN")
        if gmail_cid and gmail_sec and gmail_tok:
            from .adapters.gmail import GmailAdapter

            gmail_adapter = GmailAdapter(
                client_id=gmail_cid,
                client_secret=gmail_sec,
                refresh_token=gmail_tok,
                contact_map=contact_map,
                thread_registry=thread_registry,
                thread_max_age_days=gmail_cfg.thread_max_age_days,
                poll_interval=gmail_cfg.poll_interval,
                max_age_minutes=gmail_cfg.max_age_minutes,
                ignore_senders=gmail_cfg.ignore_senders,
            )

    # === Discord adapter (optional, requires token) ===
    discord_adapter = None
    discord_history_store = None
    discord_cfg = config.channels.discord
    if discord_cfg.enabled:
        discord_token = env.get("DISCORD_TOKEN") or os.environ.get("DISCORD_TOKEN")
        if discord_token:
            from .adapters.discord import DiscordAdapter
            from .discord_history import DiscordHistoryStore

            discord_history_store = DiscordHistoryStore(state_dir)
            discord_adapter = DiscordAdapter(
                token=discord_token,
                contact_map=contact_map,
                thread_registry=thread_registry,
                config=discord_cfg,
                history_store=discord_history_store,
            )

    extra_allowed_paths: list[str] = []
    if gmail_adapter is not None:
        extra_allowed_paths.append(gmail_adapter.attachments_dir)
    if discord_adapter is not None:
        extra_allowed_paths.extend(discord_adapter.history_store.allowed_paths)

    # web_fetch summarizer (secondary LLM for content extraction)
    wf_summarizer = None
    if config.tools.web_fetch.enabled and config.tools.web_fetch.summarize_with_llm:
        wf_agent_cfg = config.agents.get("web_fetch_summarizer")
        if wf_agent_cfg is not None and wf_agent_cfg.enabled:
            wf_summarizer = _build_subagent_client(
                "web_fetch_summarizer",
                wf_agent_cfg,
                session_debug_label="web_fetch_summarizer",
            )

    registry, all_allowed_paths, shell_executor = setup_tools(
        config.tools,
        agent_os_dir,
        memory_editor=memory_editor,
        bm25_search=bm25_search_instance,
        brain_has_vision=brain_has_vision,
        use_own_vision_ability=use_own_vision,
        own_vision_active=own_vision_active,
        vision_agent=vision_agent_instance,
        gui_manager=gui_manager_instance,
        gui_worker=gui_worker_instance,
        gui_lock=gui_lock,
        screenshot_max_width=ss_max_width,
        screenshot_quality=ss_quality,
        contact_map=contact_map,
        extra_allowed_paths=extra_allowed_paths,
        on_shell_stdout_line=console.print_shell_stream_line,
        is_shell_cancel_requested=cancel_controller.is_requested,
        web_fetch_summarizer=wf_summarizer,
    )

    # === compactor client ===
    # Conversation compaction: summarizing sub-agent used before the local
    # deterministic fallback. Built before the worker runner because worker
    # notes compression reuses the same client.
    compactor_agent_instance: CompactorAgent | None = None
    compactor_client = None
    compactor_config = config.agents.get("compactor")
    if compactor_config and compactor_config.enabled:
        compactor_client = _build_subagent_client(
            "compactor", compactor_config, session_debug_label="compactor"
        )
        compactor_prompt = _load_agent_prompt("compactor")
        if compactor_prompt is not None:
            compactor_agent_instance = CompactorAgent(compactor_client, compactor_prompt)

    # === worker subagent runner ===
    # Built before AgentCore because maintenance-driven memory curation calls
    # the runner directly from AgentCore, not only via the brain-facing
    # "worker" tool registered further below.
    worker_config = config.agents.get("worker")
    worker_runner = None
    worker_counter = None
    worker_extra_tools = {}
    if worker_config is not None and worker_config.enabled:
        from ..worker import WORKER_TOOL_DEFINITION, WorkerRunner, create_worker_tool
        from ..worker.tool_adapter import WorkerCounter
        from .tool_setup import build_worker_file_tools

        worker_client = _build_subagent_client("worker", worker_config)
        worker_prompt = _load_agent_prompt("worker")
        if worker_prompt is None:
            raise BuildError("Failed to load worker prompt")
        worker_cache_ctrl = build_cache_control(
            resolve_breakpoint_cache_ttl(
                provider=getattr(worker_config.llm, "provider", ""),
                enabled=worker_config.cache.enabled,
                configured_ttl=worker_config.cache.ttl,
            )
        )

        worker_overrides = build_worker_file_tools(all_allowed_paths, agent_os_dir)

        worker_notes = None
        worker_notes_summarizer = None
        if worker_config.notes.enabled:
            from functools import partial

            from ..worker.notes import (
                WORKER_NOTE_DEFINITION,
                WorkerNotes,
                compress_worker_notes,
                create_worker_note,
            )

            worker_notes = WorkerNotes(
                agent_os_dir / worker_config.notes.path,
                threshold_chars=worker_config.notes.compress_threshold_chars,
                max_chars=worker_config.notes.max_chars,
            )
            worker_extra_tools["worker_note"] = (
                create_worker_note(worker_notes),
                WORKER_NOTE_DEFINITION,
            )
            if compactor_client is not None:
                worker_notes_summarizer = partial(
                    compress_worker_notes,
                    compactor_client,
                    max_chars=worker_config.notes.max_chars,
                )
            else:
                logger.info("Compactor disabled; worker notes compression is off")

        # Always exclude worker itself to prevent recursion.
        excluded = frozenset(worker_config.excluded_tools) | {"worker"}
        worker_runner = WorkerRunner(
            worker_client,
            registry,
            excluded,
            worker_prompt,
            max_turns=worker_config.max_turns,
            max_context_tokens=worker_config.max_context_tokens,
            cache_control=worker_cache_ctrl,
            sink=session_mgr,
            provider=getattr(worker_config.llm, "provider", None),
            model=getattr(worker_config.llm, "model", None),
            ui_console=console,
            tool_overrides=worker_overrides,
            extra_tools=worker_extra_tools,
            notes=worker_notes,
            notes_summarizer=worker_notes_summarizer,
        )
        worker_counter = WorkerCounter()

    # Periodic memory backup
    memory_backup_mgr = None
    if config.maintenance.backup.enabled:
        memory_backup_mgr = MemoryBackupManager(agent_os_dir, config.maintenance.backup)

    # === Persistent queue ===
    # Console input is interactive; replaying it after a restart would answer
    # a question nobody is waiting on.
    pqueue = PersistentPriorityQueue(
        agent_os_dir / "queue",
        discard_channels={"cli"},
    )

    # === Build AgentCore ===
    agent = AgentCore(
        client=client,
        conversation=conversation,
        builder=builder,
        registry=registry,
        excluded_tools=frozenset(brain_agent_config.excluded_tools),
        ui_sink=ui_sink,
        workspace=workspace,
        config=config,
        agent_os_dir=agent_os_dir,
        user_id=user_id,
        session_mgr=session_mgr,
        display_name=display_name,
        memory_edit_allow_failure=config.tools.memory_edit.allow_failure,
        memory_backup_mgr=memory_backup_mgr,
        queue=pqueue,
        turn_cancel=cancel_controller,
        shared_state_store=shared_state_store,
        scope_resolver=DEFAULT_SCOPE_RESOLVER,
        memory_sync_client=memory_sync_client,
        worker_runner=worker_runner,
        compactor_agent=compactor_agent_instance,
        brain_prompt_policy=brain_prompt_policy,
        ui_debug=debug,
        ui_show_tool_use=config.ui.show_tool_use,
        ui_timezone=timezone,
        task_store=task_store,
        note_store=note_store,
    )

    # === Console adapter ===
    console_adapter = ConsoleAdapter(
        ui_sink=ui_sink,
        user_id=user_id,
        cancel_controller=cancel_controller,
    )
    agent.register_adapter(console_adapter)

    if gmail_adapter is not None:
        agent.register_adapter(gmail_adapter)
        logger.debug("Gmail adapter registered")

    if discord_adapter is not None:
        agent.register_adapter(discord_adapter)
        logger.debug("Discord adapter registered")

    # === Scheduler adapter (heartbeat, optional) ===
    if config.heartbeat.enabled:
        from .adapters.scheduler import SchedulerAdapter

        scheduler_adapter = SchedulerAdapter(
            interval=config.heartbeat.interval,
            enqueue_startup=config.heartbeat.enqueue_startup,
            enqueue_upgrade_notice=config.heartbeat.enqueue_upgrade_notice,
            upgrade_message=inputs.upgrade_message,
            quiet_windows=config.heartbeat.parsed_quiet_windows(),
        )
        agent.register_adapter(scheduler_adapter)
        logger.debug("Scheduler adapter registered")

    # === send_message tool (registered after adapters are available) ===
    from ..tools.builtin.send_message import (
        build_send_message_definition,
        create_send_message,
    )
    from .turn_context import TurnContext

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

    # === gui_task tool (synchronous; serialized by gui_lock) ===
    if gui_manager_instance is not None:
        from ..gui.tool_adapter import GUI_TASK_DEFINITION, create_gui_task

        registry.register(
            "gui_task",
            create_gui_task(
                gui_manager_instance,
                gui_lock=gui_lock,
                agent_os_dir=agent_os_dir,
                lock_wait_seconds=gm_cfg.lock_wait_seconds,
            ),
            GUI_TASK_DEFINITION,
        )

    from ..tools.builtin.shell_task import SHELL_TASK_DEFINITION, create_shell_task

    shell_task_manager = ShellTaskManager(
        max_concurrent=config.tools.shell.task_max_concurrency,
        ui_sink=ui_sink,
    )

    registry.register(
        "shell_task",
        create_shell_task(
            queue=pqueue,
            ui_sink=ui_sink,
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
    from ..tools.builtin.agent_note import AGENT_NOTE_DEFINITION, create_agent_note
    from ..tools.builtin.agent_task import AGENT_TASK_DEFINITION, create_agent_task

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
    # Runner itself was built earlier (before AgentCore); only register the
    # brain-facing async tool here, once pqueue exists.
    if worker_runner is not None:
        registry.register(
            "worker",
            create_worker_tool(
                worker_runner,
                agent_os_dir,
                worker_counter,
                queue=pqueue,
                max_concurrent=worker_config.task_max_concurrency,
            ),
            WORKER_TOOL_DEFINITION,
        )

    # All registrations are done by now, so unknown exclusions are real typos.
    validate_excluded_tools(
        registry, config.agents, extra_tools_by_agent={"worker": worker_extra_tools},
    )

    handle = _Handle(
        core=agent,
        console_adapter=console_adapter,
        cancel_controller=cancel_controller,
        shell_task_manager=shell_task_manager,
    )
    return BuiltAgent(
        core=agent,
        handle=handle,
        ui_event_store=ui_event_store,
        shell_task_manager=shell_task_manager,
        _startup=_Startup(
            inputs=inputs,
            console=console,
            queue=pqueue,
            session_mgr=session_mgr,
            conversation=conversation,
            resumed_message_count=resumed_message_count,
            repaired_tool_calls=repaired_tool_calls,
            shared_state_store=shared_state_store,
            rebuild_shared_state=rebuild_shared_state,
            handle=handle,
        ),
    )
