"""launchd plist content and launchctl command lines."""

import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from lincy.host import service
from lincy.host.errors import HostError


class FakeRunner:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.result = (returncode, stdout, stderr)

    def __call__(self, cmd):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, *self.result)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    return tmp_path / "home"


def test_plist_content(home):
    repo = Path("/repo")
    plist = plistlib.loads(service.build_plist(repo).encode())

    assert plist["Label"] == "com.lincy.agent"
    assert plist["ProgramArguments"] == ["/repo/.venv/bin/python", "-m", "lincy", "start"]
    assert plist["WorkingDirectory"] == "/repo"
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["ThrottleInterval"] == 10
    assert plist["StandardOutPath"] == "/repo/logs/lincy.log"
    assert plist["StandardErrorPath"] == "/repo/logs/lincy.log"
    assert "/opt/homebrew/bin" in plist["EnvironmentVariables"]["PATH"]
    assert plist["ProcessType"] == "Interactive"


def test_install_writes_plist_and_bootstraps(home, tmp_path):
    runner = FakeRunner()
    repo = tmp_path / "repo"
    repo.mkdir()

    service.install(run=runner, repo_root=repo)

    path = home / "Library" / "LaunchAgents" / "com.lincy.agent.plist"
    assert plistlib.loads(path.read_bytes())["WorkingDirectory"] == str(repo)
    assert (repo / "logs").is_dir()
    assert runner.calls == [["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)]]


def test_uninstall_boots_out_then_removes(home):
    path = service.plist_path()
    path.parent.mkdir(parents=True)
    path.write_text("x")
    runner = FakeRunner()

    service.uninstall(run=runner)

    assert runner.calls == [["launchctl", "bootout", f"gui/{os.getuid()}/com.lincy.agent"]]
    assert not path.exists()


def test_start_kickstarts():
    runner = FakeRunner()
    service.start(run=runner)
    assert runner.calls == [["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.lincy.agent"]]


def test_status_prints_state_and_pid(capsys):
    output = "gui/501/com.lincy.agent = {\n\tstate = running\n\tpid = 123\n\tprogram = x\n}\n"
    service.status(run=FakeRunner(stdout=output))
    assert capsys.readouterr().out == "state = running\npid = 123\n"


def test_launchctl_failure_raises_with_stderr(home):
    runner = FakeRunner(returncode=5, stderr="Input/output error")
    with pytest.raises(HostError, match="Input/output error"):
        service.start(run=runner)
