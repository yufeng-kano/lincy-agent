"""launchd LaunchAgent install / control for `lincy service ...`."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from typing import Callable

from .check import REPO_ROOT, enriched_path
from .errors import HostError

LABEL = "com.lincy.agent"

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_plist(repo_root: Path) -> str:
    log_path = str(repo_root / "logs" / "lincy.log")
    plist = {
        "Label": LABEL,
        # Not `uv run`: it would sync dependencies on every launch.
        "ProgramArguments": [str(repo_root / ".venv" / "bin" / "python"), "-m", "lincy", "start"],
        "WorkingDirectory": str(repo_root),
        "RunAtLoad": True,
        # `lincy stop` exits 0 and stays down; crashes exit non-zero and restart.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
        "EnvironmentVariables": {"PATH": enriched_path()},
        # GUI computer use needs the user's session, hence Interactive.
        "ProcessType": "Interactive",
    }
    return plistlib.dumps(plist).decode()


def _launchctl(run: Runner, *args: str) -> str:
    result = run(["launchctl", *args])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise HostError(f"launchctl {args[0]} failed (exit {result.returncode}): {detail}")
    return result.stdout


def _service_target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def install(*, run: Runner = _run, repo_root: Path = REPO_ROOT) -> None:
    (repo_root / "logs").mkdir(exist_ok=True)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_plist(repo_root))
    _launchctl(run, "bootstrap", f"gui/{os.getuid()}", str(path))
    print(f"Installed {path}")


def uninstall(*, run: Runner = _run) -> None:
    _launchctl(run, "bootout", _service_target())
    plist_path().unlink()
    print(f"Removed {plist_path()}")


def start(*, run: Runner = _run) -> None:
    _launchctl(run, "kickstart", "-k", _service_target())


def status(*, run: Runner = _run) -> None:
    output = _launchctl(run, "print", _service_target())
    for line in output.splitlines():
        key = line.strip().split(" = ", 1)[0]
        if key in ("state", "pid"):
            print(line.strip())
