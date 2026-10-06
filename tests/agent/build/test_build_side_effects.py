"""build_agent leaves workspace state alone; BuiltAgent.start() applies the deferred writes."""

import json
from pathlib import Path

from lincy.agent import build as build_module
from lincy.agent.build import BuildInputs, build_agent
from lincy.agent.handle import AgentState
from lincy.agent.queue import _serialize
from lincy.agent.schema import InboundMessage
from lincy.core.schema import AppConfig


class _DummyWorkspace:
    def __init__(self, agent_os_dir: Path):
        self.kernel_dir = agent_os_dir / "kernel"
        self.memory_dir = agent_os_dir / "memory"

    def get_system_prompt(self, _agent: str) -> str:
        return "prompt"

    def get_agent_prompt(self, *args, **kwargs) -> str:
        raise FileNotFoundError("optional prompt")


def _inputs(tmp_path: Path) -> BuildInputs:
    llm = {"provider": "openrouter", "model": "dummy"}
    config = AppConfig.model_validate({
        "app": {"agent_os_dir": str(tmp_path)},
        "heartbeat": {"enabled": False},
        "channels": {"gmail": {"enabled": False}, "discord": {"enabled": False}},
        "maintenance": {"enabled": False},
        "agents": {
            "brain": {"enabled": True, "llm": llm},
            "memory_editor": {"enabled": True, "llm": llm, "post_parse_retries": 0},
        },
    })
    return BuildInputs(
        config=config,
        agent_os_dir=tmp_path,
        user_id="yufeng",
        display_name="Yufeng",
        resume_id=None,
        ax_binary=None,
        upgrade_message="",
    )


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_build_defers_writes_until_start(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(build_module, "WorkspaceManager", _DummyWorkspace)
    monkeypatch.setattr(build_module, "create_agent_client", lambda *a, **kw: object())
    events = tmp_path / "state" / "ui_events" / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text("{}\n")
    stale = InboundMessage(channel="discord", content="left over", priority=1, sender="u")
    (tmp_path / "queue" / "active").mkdir(parents=True)
    (tmp_path / "queue" / "active" / "0001_00000001.json").write_text(
        json.dumps(_serialize(stale))
    )
    before = _files(tmp_path)

    built = build_agent(_inputs(tmp_path))

    assert _files(tmp_path) == before
    assert built.handle.state() is AgentState.STARTING
    assert built.handle.session_id() is None
    assert built.handle.channels() == ["cli"]

    built.start()

    assert (tmp_path / "state" / "ui_events" / "events.prev.jsonl").exists()
    assert built.core._queue.pending_count() == 1
    session_id = built.handle.session_id()
    assert session_id is not None
    assert (tmp_path / "session" / "brain" / session_id / "meta.json").exists()
    built.close()
