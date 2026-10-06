"""HostRuntime.run(): server lifecycle around the agent loop, exit vs exec."""

import signal
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from lincy.agent.handle import ExitReason
from lincy.host.errors import HostError
from lincy.host.runtime import HostRuntime


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeBuilt:
    def __init__(self, port: int, reason: ExitReason) -> None:
        self.port = port
        self.reason = reason
        self.events: list[str] = []
        self.handle = SimpleNamespace(request_shutdown=lambda graceful: None)
        self.ui_event_store = object()

    def start(self) -> None:
        # The server must already answer when the agent starts.
        response = httpx.get(f"http://127.0.0.1:{self.port}/ping", timeout=2)
        self.events.append(f"start:{response.status_code}")

    def run(self) -> ExitReason:
        self.events.append("run")
        return self.reason

    def close(self) -> None:
        self.events.append("close")


def _runtime(reason: ExitReason, exec_calls: list):
    port = _free_port()
    env = SimpleNamespace(
        config=SimpleNamespace(app=SimpleNamespace(server=SimpleNamespace(host="127.0.0.1", port=port))),
        agent_os_dir=Path(tempfile.mkdtemp()),
    )
    built = FakeBuilt(port, reason)

    def app_factory(**kwargs):
        app = FastAPI()
        app.get("/ping")(lambda: {"ok": True})
        return app

    runtime = HostRuntime(
        env, built, app_factory=app_factory, exec_fn=lambda *args: exec_calls.append(args)
    )
    return runtime, built, port


def _port_is_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def test_shutdown_returns_0_and_stops_server():
    exec_calls: list = []
    previous = signal.getsignal(signal.SIGTERM)
    runtime, built, port = _runtime(ExitReason.SHUTDOWN, exec_calls)

    assert runtime.run() == 0

    assert built.events == ["start:200", "run", "close"]
    assert exec_calls == []
    assert _port_is_free(port)
    assert signal.getsignal(signal.SIGTERM) is previous


def test_restart_execs_lincy_start():
    exec_calls: list = []
    runtime, built, _ = _runtime(ExitReason.RESTART, exec_calls)

    runtime.run()

    assert built.events == ["start:200", "run", "close"]
    assert exec_calls == [(sys.executable, [sys.executable, "-m", "lincy", "start"])]


# uvicorn calls sys.exit(1) inside its thread on bind failure.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_bind_failure_raises_host_error_before_agent_starts():
    runtime, built, port = _runtime(ExitReason.SHUTDOWN, [])
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))
        sock.listen()
        with pytest.raises(HostError, match="failed to start"):
            runtime.run()
    assert built.events == []
