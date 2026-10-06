"""Control API routes over a fake AgentHandle."""

from datetime import UTC, datetime

import httpx
import pytest

from lincy.agent.handle import (
    AgentBusy,
    AgentState,
    InvalidRequest,
    ShellSessionNotFound,
    UnsupportedChannel,
)
from lincy.agent.ui_event_stream import UiEventRecord, UiEventStore
from lincy.host.app import create_app
from lincy.host.control_api import RuntimeInfo
from lincy.host.errors import UpgradeInProgress
from lincy.host.upgrade import UpgradeStatus


class FakeHandle:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.submit_error: Exception | None = None

    def state(self) -> AgentState:
        return AgentState.READY

    def session_id(self) -> str | None:
        return "s-1"

    def channels(self) -> list[str]:
        return ["cli", "gmail"]

    def submit(self, content: str, channel: str = "cli") -> None:
        if self.submit_error is not None:
            raise self.submit_error
        self.calls.append(("submit", content, channel))

    def cancel_turn(self) -> None:
        self.calls.append(("cancel_turn",))

    def request_new_session(self) -> None:
        self.calls.append(("new_session",))

    def request_compact(self) -> None:
        self.calls.append(("compact",))

    def request_clear(self) -> None:
        self.calls.append(("clear",))

    def request_reload(self, target) -> None:
        self.calls.append(("reload", target))

    def request_shutdown(self, *, graceful: bool = True) -> None:
        self.calls.append(("shutdown", graceful))

    def request_restart(self) -> None:
        self.calls.append(("restart",))

    def token_status(self) -> str:
        return ""

    def shell_sessions(self) -> list[dict]:
        return [{"id": "sh-1"}]

    def shell_send(self, session_id: str, *, text: str | None, key: str | None) -> str:
        if session_id != "sh-1":
            raise ShellSessionNotFound(f"shell session {session_id} was not found")
        self.calls.append(("shell_send", session_id, text, key))
        return "sent"

    def shell_cancel(self, session_id: str) -> str:
        if session_id != "sh-1":
            raise ShellSessionNotFound(f"shell session {session_id} was not found")
        return "cancelled"


class FakeUpgrade:
    def __init__(self) -> None:
        self.in_progress = False

    def status(self) -> UpgradeStatus:
        return UpgradeStatus()

    def start(self, handle) -> UpgradeStatus:
        if self.in_progress:
            raise UpgradeInProgress("upgrade already in progress (syncing)")
        self.in_progress = True
        return UpgradeStatus(state="fetching", from_sha="abc123")


@pytest.fixture
def setup(tmp_path):
    handle = FakeHandle()
    upgrade = FakeUpgrade()
    store = UiEventStore(tmp_path / "events.jsonl")
    app = create_app(
        handle=handle,
        event_store=store,
        info=RuntimeInfo(started_at="2026-10-06T00:00:00+00:00", git_sha="deadbee"),
        upgrade=upgrade,
        config=None,
        web=False,
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return client, handle, upgrade, app


@pytest.mark.asyncio
async def test_health_shape(setup):
    client, _, _, app = setup
    response = await client.get("/api/agent/health")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "status", "state", "pid", "session_id", "started_at", "git_sha", "upgrade", "web",
    }
    assert body["status"] == "ok"
    assert body["state"] == "ready"
    assert body["session_id"] == "s-1"
    assert body["git_sha"] == "deadbee"
    assert body["web"] == "disabled"
    assert body["upgrade"] == {
        "state": "idle", "from_sha": None, "to_sha": None, "error": None, "started_at": None,
    }

    app.state.web_status = "ready"
    assert (await client.get("/api/agent/health")).json()["web"] == "ready"


@pytest.mark.asyncio
async def test_action_routes_return_202_and_reach_handle(setup):
    client, handle, _, _ = setup
    routes = {
        "/api/agent/shutdown": ("shutdown", True),
        "/api/agent/turn/cancel": ("cancel_turn",),
        "/api/agent/session/new": ("new_session",),
        "/api/agent/session/compact": ("compact",),
        "/api/agent/session/clear": ("clear",),
    }
    for path, call in routes.items():
        response = await client.post(path)
        assert response.status_code == 202, path
        assert handle.calls[-1] == call
    assert (await client.post("/api/agent/shutdown")).json() == {"status": "shutting_down"}


@pytest.mark.asyncio
async def test_reload_defaults_to_all_and_accepts_system_prompt(setup):
    client, handle, _, _ = setup
    assert (await client.post("/api/agent/reload")).status_code == 202
    assert handle.calls[-1] == ("reload", "all")
    response = await client.post("/api/agent/reload", json={"target": "system-prompt"})
    assert response.status_code == 202
    assert handle.calls[-1] == ("reload", "system-prompt")
    assert (await client.post("/api/agent/reload", json={"target": "x"})).status_code == 422


@pytest.mark.asyncio
async def test_channels_and_messages(setup):
    client, handle, _, _ = setup
    assert (await client.get("/api/agent/channels")).json() == {"channels": ["cli", "gmail"]}

    response = await client.post("/api/agent/messages", json={"content": "hi"})
    assert response.status_code == 202
    assert response.json() == {"status": "accepted", "channel": "cli"}
    response = await client.post("/api/agent/messages", json={"content": "x", "channel": "gmail"})
    assert handle.calls[-1] == ("submit", "x", "gmail")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status"),
    [
        (AgentBusy("agent is busy"), 409),
        (UnsupportedChannel("unsupported channel: x"), 400),
        (InvalidRequest("content is required"), 400),
    ],
)
async def test_agent_errors_map_to_status(setup, error, status):
    client, handle, _, _ = setup
    handle.submit_error = error
    response = await client.post("/api/agent/messages", json={"content": "hi"})
    assert response.status_code == status
    assert response.json() == {"error": str(error)}


@pytest.mark.asyncio
async def test_upgrade_started_then_conflict(setup):
    client, _, _, _ = setup
    response = await client.post("/api/agent/upgrade")
    assert response.status_code == 202
    assert response.json() == {"status": "started", "from_sha": "abc123"}
    response = await client.post("/api/agent/upgrade")
    assert response.status_code == 409
    assert "in progress" in response.json()["error"]


@pytest.mark.asyncio
async def test_events(setup, tmp_path):
    client, _, _, _ = setup
    store = UiEventStore(tmp_path / "events.jsonl")
    for seq in range(3):
        store.append(
            UiEventRecord(seq=seq, ts=datetime(2026, 1, 1, tzinfo=UTC), type="info", data={})
        )
    body = (await client.get("/api/agent/events", params={"limit": 2})).json()
    assert [event["seq"] for event in body["events"]] == [1, 2]
    assert (await client.get("/api/agent/events", params={"limit": 0})).status_code == 422
    assert (await client.get("/api/agent/events", params={"limit": 2001})).status_code == 422


@pytest.mark.asyncio
async def test_shell_routes(setup):
    client, handle, _, _ = setup
    assert (await client.get("/api/agent/shell/sessions")).json() == {"sessions": [{"id": "sh-1"}]}

    response = await client.post("/api/agent/shell/sessions/sh-1/input", json={"key": "enter"})
    assert response.status_code == 200
    assert response.json() == {"result": "sent"}
    assert handle.calls[-1] == ("shell_send", "sh-1", None, "enter")

    response = await client.post("/api/agent/shell/sessions/sh-1/cancel")
    assert response.json() == {"result": "cancelled"}

    response = await client.post("/api/agent/shell/sessions/nope/input", json={"text": "y"})
    assert response.status_code == 404
    assert (await client.post("/api/agent/shell/sessions/nope/cancel")).status_code == 404
