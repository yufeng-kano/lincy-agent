"""Manual upgrade: pull, sync, build, gate on `lincy check`, roll back on failure."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..agent.handle import AgentHandle
from .check import enriched_path
from .errors import HostError, UpgradeInProgress

logger = logging.getLogger(__name__)

RunCmd = Callable[[list[str], Path, dict[str, str]], subprocess.CompletedProcess]

_IDLE_STATES = frozenset({"idle", "failed", "up_to_date"})
_STDERR_TAIL = 2000


@dataclass(frozen=True)
class UpgradeStatus:
    state: str = "idle"
    from_sha: str | None = None
    to_sha: str | None = None
    error: str | None = None
    started_at: str | None = None


class _StepFailed(HostError):
    pass


def _subprocess_run(cmd: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)


class UpgradeManager:
    """One upgrade at a time, run in a daemon thread; state is read by /health."""

    def __init__(self, repo_root: Path, *, run_cmd: RunCmd = _subprocess_run) -> None:
        self._repo_root = repo_root
        self._web_ui_dir = repo_root / "src" / "web_ui"
        self._run_cmd = run_cmd
        self._lock = threading.Lock()
        self._status = UpgradeStatus()
        self._thread: threading.Thread | None = None
        # launchd gives the service a bare PATH; uv and bun live in user dirs.
        self._env = {**os.environ, "PATH": enriched_path()}

    def status(self) -> UpgradeStatus:
        with self._lock:
            return self._status

    def start(self, handle: AgentHandle) -> UpgradeStatus:
        with self._lock:
            if self._status.state not in _IDLE_STATES:
                raise UpgradeInProgress(f"upgrade already in progress ({self._status.state})")
            # from_sha is resolved up front because the 202 response carries it.
            from_sha = self._git("rev-parse", "HEAD")
            self._status = UpgradeStatus(
                state="fetching",
                from_sha=from_sha,
                started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            started = self._status
        self._thread = threading.Thread(
            target=self._run, args=(handle, from_sha), name="lincy-upgrade", daemon=True
        )
        self._thread.start()
        return started

    def _set(self, **changes) -> None:
        with self._lock:
            self._status = replace(self._status, **changes)
        logger.info("upgrade state: %s", self._status.state)

    def _step(self, name: str, cmd: list[str], cwd: Path | None = None) -> str:
        result = self._run_cmd(cmd, cwd or self._repo_root, self._env)
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "")[-_STDERR_TAIL:]
            raise _StepFailed(f"{name} failed (exit {result.returncode}): {tail}")
        return result.stdout.strip()

    def _git(self, *args: str) -> str:
        return self._step(f"git {args[0]}", ["git", *args])

    def _run(self, handle: AgentHandle, from_sha: str) -> None:
        # Catch everything: a crashed worker would leave the state stuck
        # in a busy value and every later upgrade request would get 409.
        try:
            self._upgrade(handle, from_sha)
        except Exception as e:
            logger.exception("upgrade failed")
            self._set(state="failed", error=str(e))

    def _upgrade(self, handle: AgentHandle, from_sha: str) -> None:
        try:
            if self._git("status", "--porcelain"):
                # Rollback uses reset --hard, which would destroy local edits.
                raise _StepFailed("working tree is dirty; commit or discard local changes")
            branch = self._git("rev-parse", "--abbrev-ref", "HEAD")
            if branch == "HEAD":
                raise _StepFailed("HEAD is detached; check out a branch first")
            self._git("fetch", "origin", branch)
            remote_sha = self._git("rev-parse", f"origin/{branch}")
            if remote_sha == from_sha:
                self._set(state="up_to_date", to_sha=from_sha)
                return
            self._set(state="pulling", to_sha=remote_sha)
            self._git("pull", "--ff-only")
        except _StepFailed as e:
            self._set(state="failed", error=str(e))
            return

        try:
            self._set(state="syncing")
            self._step("uv sync", ["uv", "sync"])
            self._set(state="building")
            self._step("bun run build", ["bun", "run", "build"], cwd=self._web_ui_dir)
            self._set(state="checking")
            self._step("lincy check", [sys.executable, "-m", "lincy", "check"])
        except _StepFailed as e:
            self._rollback(from_sha, str(e))
            return

        handle.request_restart()
        self._set(state="restart_pending")

    def _rollback(self, from_sha: str, error: str) -> None:
        # Disk must match the code in memory, or a launchd restart would
        # boot an unverified version.
        self._set(state="rolling_back", error=error)
        try:
            self._git("reset", "--hard", from_sha)
            self._step("uv sync", ["uv", "sync"])
            self._step("bun run build", ["bun", "run", "build"], cwd=self._web_ui_dir)
        except _StepFailed as e:
            error = f"{error}\nrollback: {e}"
        self._set(state="failed", error=error)
