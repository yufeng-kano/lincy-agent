"""lincy CLI: parser, HTTP subcommands against a fake server, error exit codes."""

import httpx
import pytest

from lincy.host import cli
from lincy.host.errors import WorkspaceNotReady


def _health(**overrides):
    body = {
        "status": "ok",
        "state": "ready",
        "pid": 42,
        "session_id": "s-1",
        "started_at": "2026-10-06T00:00:00+00:00",
        "git_sha": "aaaaaaa",
        "upgrade": {"state": "idle", "from_sha": None, "to_sha": None, "error": None,
                    "started_at": None},
        "web": "ready",
    }
    body.update(overrides)
    return body


class FakeServer:
    """Scripted responses per (method, path); a list is consumed one item per call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, str]] = []

    def _respond(self, method, url, **kwargs):
        path = url.removeprefix("http://127.0.0.1:9002")
        self.calls.append((method, path))
        answer = self.routes[(method, path)]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return httpx.Response(status, json=body)

    def get(self, url, **kwargs):
        return self._respond("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._respond("POST", url, **kwargs)


@pytest.fixture
def server(monkeypatch):
    def install(routes):
        fake = FakeServer(routes)
        monkeypatch.setattr(httpx, "get", fake.get)
        monkeypatch.setattr(httpx, "post", fake.post)
        return fake

    monkeypatch.setattr(cli, "load_raw_agent_config", lambda: {"app": {}})
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    return install


def test_bare_lincy_exits_2(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
    assert "usage: lincy" in capsys.readouterr().err


def test_start_flags_are_mutually_exclusive():
    with pytest.raises(SystemExit) as exc:
        cli.main(["start", "--new", "--resume", "abc"])
    assert exc.value.code == 2


def test_resume_requires_value():
    with pytest.raises(SystemExit) as exc:
        cli.main(["start", "--resume"])
    assert exc.value.code == 2


def test_start_passes_session_choice(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "run_start", lambda **kw: seen.append(kw) or 0)
    assert cli.main(["start"]) == 0
    assert cli.main(["start", "--new"]) == 0
    assert cli.main(["start", "--resume", "abc"]) == 0
    assert seen == [
        {"new_session": False, "resume_id": None},
        {"new_session": True, "resume_id": None},
        {"new_session": False, "resume_id": "abc"},
    ]


def test_host_error_prints_and_exits_1(monkeypatch, capsys):
    def failing():
        raise WorkspaceNotReady("Workspace not initialized at /x. Run: uv run lincy init")

    monkeypatch.setattr(cli, "run_check", failing)
    assert cli.main(["check"]) == 1
    assert capsys.readouterr().err == (
        "Error: Workspace not initialized at /x. Run: uv run lincy init\n"
    )


def test_status_prints_health(server, capsys):
    server({("GET", "/api/agent/health"): (200, _health())})
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "state         ready" in out
    assert "git sha       aaaaaaa" in out
    assert "upgrade       idle" in out


def test_status_when_not_running(server, capsys):
    server({("GET", "/api/agent/health"): httpx.ConnectError("refused")})
    assert cli.main(["status"]) == 1
    assert "lincy is not running at http://127.0.0.1:9002" in capsys.readouterr().err


def test_stop_posts_shutdown(server):
    fake = server({("POST", "/api/agent/shutdown"): (202, {"status": "shutting_down"})})
    assert cli.main(["stop"]) == 0
    assert fake.calls == [("POST", "/api/agent/shutdown")]


def _upgrade_state(state, error=None):
    upgrade = {"state": state, "from_sha": "a" * 40, "to_sha": None, "error": error,
               "started_at": "t"}
    return (200, _health(upgrade=upgrade))


def test_upgrade_follows_restart_to_new_sha(server, capsys):
    server({
        ("POST", "/api/agent/upgrade"): (202, {"status": "started", "from_sha": "a" * 40}),
        ("GET", "/api/agent/health"): [
            (200, _health()),
            _upgrade_state("syncing"),
            _upgrade_state("restart_pending"),
            httpx.ConnectError("refused"),
            (200, _health(git_sha="bbbbbbb")),
        ],
    })
    assert cli.main(["upgrade"]) == 0
    assert "upgraded aaaaaaa -> bbbbbbb" in capsys.readouterr().out


def test_upgrade_up_to_date(server, capsys):
    server({
        ("POST", "/api/agent/upgrade"): (202, {"status": "started", "from_sha": "a" * 40}),
        ("GET", "/api/agent/health"): [(200, _health()), _upgrade_state("up_to_date")],
    })
    assert cli.main(["upgrade"]) == 0
    assert "already up to date" in capsys.readouterr().out


def test_upgrade_failed_exits_1(server, capsys):
    server({
        ("POST", "/api/agent/upgrade"): (202, {"status": "started", "from_sha": "a" * 40}),
        ("GET", "/api/agent/health"): [
            (200, _health()),
            _upgrade_state("failed", error="lincy check failed (exit 1): boom"),
        ],
    })
    assert cli.main(["upgrade"]) == 1
    assert "boom" in capsys.readouterr().err


def test_upgrade_conflict_is_error(server, capsys):
    server({
        ("POST", "/api/agent/upgrade"): (409, {"error": "upgrade already in progress"}),
        ("GET", "/api/agent/health"): (200, _health()),
    })
    assert cli.main(["upgrade"]) == 1
    assert "Error: upgrade already in progress" in capsys.readouterr().err


def test_service_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.service, "install", lambda: calls.append("install"))
    assert cli.main(["service", "install"]) == 0
    assert calls == ["install"]
