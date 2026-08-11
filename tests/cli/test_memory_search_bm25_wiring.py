"""Tests for BM25 memory_search wiring in CLI app."""

from pathlib import Path

import pytest

from lincy.core.schema import AppConfig, BM25SearchConfig


def _make_app_config(agent_os_dir: Path) -> AppConfig:
    return AppConfig.model_validate({
        "app": {
            "agent_os_dir": str(agent_os_dir),
            "warn_on_failure": False,
        },
        "tools": {
            "allowed_paths": [],
            "shell": {"blacklist": [], "timeout": 30},
            "memory_search": {
                "bm25": {
                    "top_k": 3,
                    "snippet_lines": 2,
                    "max_snippets_per_file": 1,
                    "max_response_chars": 4321,
                    "date_normalization": False,
                    "exclude": ["memory/agent/temp-memory.md"],
                },
            },
        },
        "agents": {
            "brain": {
                "enabled": True,
                "llm": {"provider": "openrouter", "model": "dummy"},
            },
            "memory_editor": {
                "enabled": True,
                "llm": {"provider": "openrouter", "model": "dummy"},
                "post_parse_retries": 0,
            },
        },
    })


def test_main_wires_bm25_memory_search(monkeypatch, tmp_path: Path):
    from lincy.cli import app as app_module

    captured: dict[str, object] = {}
    sentinel = RuntimeError("stop after bm25 setup")

    class _DummyBM25MemorySearch:
        def __init__(self, memory_dir: Path, config: BM25SearchConfig):
            captured["memory_dir"] = memory_dir
            captured["config"] = config

    class _DummyWorkspace:
        def __init__(self, agent_os_dir: Path):
            self.agent_os_dir = agent_os_dir
            self.kernel_dir = agent_os_dir / "kernel"
            self.memory_dir = agent_os_dir / "memory"

        def is_initialized(self) -> bool:
            return True

        def get_system_prompt(self, _agent: str) -> str:
            return "prompt"

        def get_agent_prompt(self, *args, **kwargs) -> str:
            return "parse-retry"

    class _DummyInitializer:
        def __init__(self, workspace):
            self.workspace = workspace

        def needs_upgrade(self) -> bool:
            return False

        def upgrade_kernel(self):
            return []

    class _DummyConsole:
        def set_debug(self, debug: bool) -> None:
            self.debug = debug

        def set_show_tool_use(self, show: bool) -> None:
            self.show_tool_use = show

        def set_current_user(self, user_id: str) -> None:
            pass

        def set_timezone(self, timezone: str) -> None:
            pass

        def print_welcome(self) -> None:
            pass

        def print_goodbye(self) -> None:
            pass

        def print_error(self, _message: str) -> None:
            pass

        def print_info(self, _message: str) -> None:
            pass

        def print_shell_stream_line(self, _line: str) -> None:
            pass

        def set_ctx_status_provider(self, _provider) -> None:
            pass

    class _DummyTextualApp:
        def __init__(self, *args, **kwargs):
            pass

        def post_ui_event(self, _event) -> None:
            pass

        def wake_ui_event_drain(self, _event) -> None:
            pass

        def drain_ui_events(self) -> None:
            pass

        def run(self) -> None:
            pass

    monkeypatch.setattr(app_module, "load_config", lambda: _make_app_config(tmp_path))
    monkeypatch.setattr(app_module, "WorkspaceManager", _DummyWorkspace)
    monkeypatch.setattr(app_module, "WorkspaceInitializer", _DummyInitializer)
    monkeypatch.setattr(app_module, "UiEventConsole", lambda *a, **kw: _DummyConsole())
    monkeypatch.setattr(app_module, "ChatTextualApp", _DummyTextualApp)
    monkeypatch.setattr(
        app_module,
        "create_agent_client",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(app_module, "BM25MemorySearch", _DummyBM25MemorySearch)
    monkeypatch.setattr(
        app_module,
        "resolve_user_selector",
        lambda memory_dir, user_selector: ("yufeng", "Yufeng"),
    )
    monkeypatch.setattr(
        app_module,
        "ensure_user_memory_file",
        lambda memory_dir, user_id, display_name: memory_dir / f"user-{user_id}.md",
    )

    # Mock components added after MQ Phase 2 to prevent blocking.
    class _DummyRegistry:
        def register(self, *a, **kw):
            pass

    class _DummyAgent:
        adapters = {}
        turn_context = None
        def __init__(self, **kwargs):
            pass
        def get_token_status_text(self) -> str:
            return "tok --/128,000 (--.-%)"
        def register_adapter(self, adapter):
            pass
        def run(self):
            pass
        def request_shutdown(self, graceful=False):
            pass

    monkeypatch.setattr(
        app_module,
        "setup_tools",
        lambda *a, **kw: (_ for _ in ()).throw(sentinel),
    )

    class _DummyCliAdapter:
        channel_name = "cli"
        priority = 0
        def __init__(self, **kw):
            pass
        def start(self, agent):
            pass
        def send(self, message):
            pass
        def on_turn_start(self, channel):
            pass
        def on_turn_complete(self):
            pass
        def stop(self):
            pass
        def submit_input(self, text: str) -> bool:
            return False
        def select_recent_input(self):
            return None
        def list_recent_inputs(self, limit: int = 10):
            return []
        def select_recent_input_by_index(self, choice: int, limit: int = 10):
            return None

    monkeypatch.setattr(app_module, "CLIAdapter", _DummyCliAdapter)
    monkeypatch.setattr(app_module, "PersistentPriorityQueue", lambda *a, **kw: None)
    monkeypatch.setattr(app_module, "ContactMap", lambda *a, **kw: None)
    monkeypatch.setattr(app_module, "CommandHandler", lambda *a, **kw: None)

    with pytest.raises(RuntimeError, match="stop after bm25 setup"):
        app_module.main("yufeng")

    assert captured["memory_dir"] == tmp_path / "memory"
    assert captured["config"].model_dump() == {
        "top_k": 3,
        "snippet_lines": 2,
        "max_snippets_per_file": 1,
        "max_response_chars": 4321,
        "date_normalization": False,
        "exclude": ["memory/agent/temp-memory.md"],
    }
