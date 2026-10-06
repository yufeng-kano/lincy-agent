"""build_agent wires gui_manager to DesktopBackend with session debug logging."""

from pathlib import Path

from lincy.agent import build as build_module
from lincy.agent.build import BuildInputs, build_agent
from lincy.core.schema import AppConfig


class _DummyWorkspace:
    def __init__(self, agent_os_dir: Path):
        self.kernel_dir = agent_os_dir / "kernel"
        self.memory_dir = agent_os_dir / "memory"

    def get_system_prompt(self, _agent: str) -> str:
        return "prompt"

    def get_agent_prompt(self, *args, **kwargs) -> str:
        raise FileNotFoundError("optional prompt")


class _RecordingBackend:
    instances: list["_RecordingBackend"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _RecordingBackend.instances.append(self)


def test_gui_manager_wiring(monkeypatch, tmp_path: Path):
    llm = {"provider": "openrouter", "model": "dummy"}
    config = AppConfig.model_validate({
        "app": {"agent_os_dir": str(tmp_path)},
        "heartbeat": {"enabled": False},
        "channels": {"gmail": {"enabled": False}, "discord": {"enabled": False}},
        "maintenance": {"enabled": False},
        "agents": {
            "brain": {"enabled": True, "llm": llm},
            "memory_editor": {"enabled": True, "llm": llm, "post_parse_retries": 0},
            "gui_manager": {
                "enabled": True,
                "llm": llm,
                "max_steps": 12,
                "screenshot_max_width": 1000,
                "screenshot_quality": 70,
                "desktop": {"max_tree_nodes": 300, "repeat_limit": 4, "settle_seconds": 0.2},
            },
        },
    })
    labels = []

    def fake_wrap(client, *, client_label, **kwargs):
        labels.append(client_label)
        return client

    monkeypatch.setattr(build_module, "WorkspaceManager", _DummyWorkspace)
    monkeypatch.setattr(build_module, "create_agent_client", lambda *a, **kw: object())
    monkeypatch.setattr(build_module, "wrap_llm_client_with_session_debug", fake_wrap)
    monkeypatch.setattr(build_module, "DesktopBackend", _RecordingBackend)
    _RecordingBackend.instances.clear()

    built = build_agent(BuildInputs(
        config=config,
        agent_os_dir=tmp_path,
        user_id="yufeng",
        display_name="Yufeng",
        resume_id=None,
        upgrade_message="",
    ))

    assert "gui_manager" in labels
    [backend] = _RecordingBackend.instances
    assert backend.kwargs == {
        "screenshot_max_width": 1000,
        "screenshot_quality": 70,
        "max_tree_nodes": 300,
        "text_limit": 200,
        "set_marks": False,
        "default_input_source": "com.apple.keylayout.ABC",
        "settle_seconds": 0.2,
    }
    assert built.core.registry.has_tool("gui_task")
    built.close()
