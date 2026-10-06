"""validate(): each failure raises the right HostError; session choice."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from lincy.core.schema import AppConfig
from lincy.host import stages
from lincy.host.errors import BuildFailed, ConfigInvalid, WorkspaceNotReady
from lincy.agent.build import BuildError
from lincy.session import SessionManager
from lincy.workspace import WorkspaceInitializer, WorkspaceManager


def _config(agent_os_dir):
    return SimpleNamespace(
        app=SimpleNamespace(timezone="UTC+8"),
        get_agent_os_dir=lambda: agent_os_dir,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Initialized workspace, user from .env, environment checks stubbed."""
    agent_os_dir = tmp_path / "agent"
    WorkspaceInitializer(WorkspaceManager(agent_os_dir)).create_structure()
    dotenv = tmp_path / ".env"
    dotenv.write_text("CHAT_AGENT_USER=yufeng\n")
    monkeypatch.setattr(stages, "load_config", lambda: _config(agent_os_dir))
    monkeypatch.setattr(stages, "configure_runtime_timezone", lambda spec: spec)
    monkeypatch.setattr(stages, "find_dotenv", lambda usecwd: str(dotenv))
    monkeypatch.setattr(stages, "check_environment", lambda *a, **k: None)
    monkeypatch.delenv("CHAT_AGENT_USER", raising=False)
    return SimpleNamespace(agent_os_dir=agent_os_dir, dotenv=dotenv)


def _raise(exc):
    def _load():
        raise exc

    return _load


def test_pydantic_error_is_config_invalid(env, monkeypatch):
    try:
        AppConfig.model_validate({"bogus": 1})
    except ValidationError as e:
        error = e
    monkeypatch.setattr(stages, "load_config", _raise(error))
    with pytest.raises(ConfigInvalid, match="bogus"):
        stages.validate(new_session=False, resume_id=None)


def test_system_exit_from_config_loader_is_config_invalid(env, monkeypatch):
    monkeypatch.setattr(stages, "load_config", _raise(SystemExit("Config error: no such llm")))
    with pytest.raises(ConfigInvalid, match="^Config error: no such llm$"):
        stages.validate(new_session=False, resume_id=None)


def test_missing_user(env, monkeypatch):
    env.dotenv.write_text("")
    with pytest.raises(ConfigInvalid, match="CHAT_AGENT_USER"):
        stages.validate(new_session=False, resume_id=None)


def test_dotenv_user_wins_over_environment(env, monkeypatch):
    monkeypatch.setenv("CHAT_AGENT_USER", "someone-else")
    assert stages.validate(new_session=True, resume_id=None).user_id == "yufeng"

    env.dotenv.write_text("")
    assert stages.validate(new_session=True, resume_id=None).user_id == "someone-else"


def test_uninitialized_workspace(env, monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    monkeypatch.setattr(stages, "load_config", lambda: _config(empty))
    with pytest.raises(WorkspaceNotReady) as exc:
        stages.validate(new_session=False, resume_id=None)
    assert str(exc.value) == f"Workspace not initialized at {empty}. Run: uv run lincy init"


def test_valid_env_creates_user_memory(env):
    result = stages.validate(new_session=False, resume_id=None)
    assert (result.user_id, result.display_name) == ("yufeng", "yufeng")
    assert (env.agent_os_dir / "memory" / "people" / "yufeng").is_dir()
    assert result.upgrade_message == ""
    assert result.timezone == "UTC+8"


def test_session_choice(env):
    sessions = SessionManager(env.agent_os_dir / "session" / "brain")
    assert stages.validate(new_session=False, resume_id=None).resume_id is None

    sessions.create("someone-else", "Other")
    latest = sessions.create("yufeng", "yufeng")

    assert stages.validate(new_session=False, resume_id=None).resume_id == latest
    assert stages.validate(new_session=True, resume_id=None).resume_id is None
    assert stages.validate(new_session=False, resume_id="given").resume_id == "given"


def test_build_error_is_build_failed(env, monkeypatch):
    validated = stages.validate(new_session=True, resume_id=None)

    def failing(inputs):
        raise BuildError("session nope not found")

    monkeypatch.setattr(stages, "build_agent", failing)
    with pytest.raises(BuildFailed, match="session nope not found"):
        stages.build(validated)


def test_check_skips_port_probe_and_session(env, monkeypatch, capsys):
    seen = {}

    def fake_check(config, repo_root, *, probe_port):
        seen["probe_port"] = probe_port

    def fake_build(inputs):
        seen["resume_id"] = inputs.resume_id

    SessionManager(env.agent_os_dir / "session" / "brain").create("yufeng", "yufeng")
    monkeypatch.setattr(stages, "check_environment", fake_check)
    monkeypatch.setattr(stages, "build_agent", fake_build)

    assert stages.run_check() == 0
    assert seen == {"probe_port": False, "resume_id": None}
    assert capsys.readouterr().out.splitlines()[-1] == "OK build"
