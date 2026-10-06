"""UpgradeManager state machine with a recording command runner."""

import subprocess
import sys
import threading

import pytest

from lincy.host.errors import UpgradeInProgress
from lincy.host.upgrade import UpgradeManager
from lincy.host.web_ui import web_ui_is_current

FROM = "a" * 40
TO = "b" * 40


class FakeRunner:
    """Answers commands from a table; records (cmd, cwd) in order."""

    def __init__(self, *, dirty: str = "", remote: str = TO, failing: str | None = None):
        self.calls: list[tuple[list[str], object]] = []
        self.answers = {
            ("git", "rev-parse", "HEAD"): FROM,
            ("git", "status", "--porcelain"): dirty,
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): "main",
            ("git", "rev-parse", "origin/main"): remote,
        }
        self.failing = failing

    def __call__(self, cmd, cwd, env):
        self.calls.append((cmd, cwd))
        if self.failing is not None and " ".join(cmd).endswith(self.failing):
            return subprocess.CompletedProcess(cmd, 1, "", "x" * 3000 + "boom: check failed")
        return subprocess.CompletedProcess(cmd, 0, self.answers.get(tuple(cmd), ""), "")

    def commands(self) -> list[str]:
        return [" ".join(cmd) for cmd, _ in self.calls]


@pytest.fixture
def repo(tmp_path):
    # The fake bun never writes dist/, so create the build output up front.
    dist = tmp_path / "src" / "web_ui" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>")
    return tmp_path


class FakeHandle:
    def __init__(self) -> None:
        self.restarts = 0

    def request_restart(self) -> None:
        self.restarts += 1


def _run(manager: UpgradeManager, handle: FakeHandle):
    started = manager.start(handle)
    manager._thread.join(5)
    return started, manager.status()


def test_dirty_tree_fails_without_touching_anything(repo):
    runner = FakeRunner(dirty=" M src/x.py")
    manager = UpgradeManager(repo, run_cmd=runner)

    started, status = _run(manager, FakeHandle())

    assert started.state == "fetching"
    assert started.from_sha == FROM
    assert status.state == "failed"
    assert "working tree is dirty" in status.error
    assert runner.commands() == ["git rev-parse HEAD", "git status --porcelain"]


def test_up_to_date_stops_after_fetch(repo):
    runner = FakeRunner(remote=FROM)
    manager = UpgradeManager(repo, run_cmd=runner)

    _, status = _run(manager, FakeHandle())

    assert status.state == "up_to_date"
    assert runner.commands()[-1] == "git rev-parse origin/main"
    assert "git fetch origin main" in runner.commands()


def test_happy_path_requests_restart(repo):
    runner = FakeRunner()
    handle = FakeHandle()
    manager = UpgradeManager(repo, run_cmd=runner)

    _, status = _run(manager, handle)

    assert status.state == "restart_pending"
    assert (status.from_sha, status.to_sha, status.error) == (FROM, TO, None)
    assert handle.restarts == 1
    assert runner.commands() == [
        "git rev-parse HEAD",
        "git status --porcelain",
        "git rev-parse --abbrev-ref HEAD",
        "git fetch origin main",
        "git rev-parse origin/main",
        "git pull --ff-only",
        "uv sync",
        "bun install --frozen-lockfile",
        "bun run build",
        f"{sys.executable} -m lincy check",
    ]
    cwds = {" ".join(cmd): cwd for cmd, cwd in runner.calls}
    assert cwds["bun install --frozen-lockfile"] == repo / "src" / "web_ui"
    assert cwds["bun run build"] == repo / "src" / "web_ui"
    assert cwds["uv sync"] == repo
    # The stamp lets the `lincy check` gate (and the restart) skip a second build.
    assert web_ui_is_current(repo / "src" / "web_ui")


def test_check_failure_rolls_back_and_reports_stderr_tail(repo):
    runner = FakeRunner(failing="-m lincy check")
    handle = FakeHandle()
    manager = UpgradeManager(repo, run_cmd=runner)

    _, status = _run(manager, handle)

    assert status.state == "failed"
    assert handle.restarts == 0
    assert status.error.startswith("lincy check failed (exit 1): ")
    assert status.error.endswith("boom: check failed")
    assert len(status.error) < 2100
    assert runner.commands()[-5:] == [
        f"{sys.executable} -m lincy check",
        f"git reset --hard {FROM}",
        "uv sync",
        "bun install --frozen-lockfile",
        "bun run build",
    ]


def test_runner_exception_after_pull_rolls_back(repo):
    runner = FakeRunner()

    def exploding(cmd, cwd, env):
        if cmd[0] == "bun":
            # subprocess raises instead of returning non-zero for a missing binary
            raise FileNotFoundError("bun")
        return runner(cmd, cwd, env)

    manager = UpgradeManager(repo, run_cmd=exploding)
    handle = FakeHandle()

    _, status = _run(manager, handle)

    assert status.state == "failed"
    assert "bun" in status.error
    assert f"git reset --hard {FROM}" in runner.commands()
    assert handle.restarts == 0


def test_runner_exception_before_pull_fails_without_rollback(repo):
    runner = FakeRunner()

    def exploding(cmd, cwd, env):
        if cmd[:2] == ["git", "fetch"]:
            raise OSError("network down")
        return runner(cmd, cwd, env)

    manager = UpgradeManager(repo, run_cmd=exploding)

    _, status = _run(manager, FakeHandle())

    assert status.state == "failed"
    assert "network down" in status.error
    assert not any(c.startswith("git reset") for c in runner.commands())


def test_concurrent_start_raises_in_progress(repo):
    release = threading.Event()
    runner = FakeRunner()

    def blocking(cmd, cwd, env):
        if cmd[:2] == ["git", "status"]:
            release.wait(5)
        return runner(cmd, cwd, env)

    manager = UpgradeManager(repo, run_cmd=blocking)
    handle = FakeHandle()
    manager.start(handle)
    with pytest.raises(UpgradeInProgress):
        manager.start(handle)
    release.set()
    manager._thread.join(5)
    assert manager.status().state == "restart_pending"
