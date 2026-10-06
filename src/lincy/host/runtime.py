"""HostRuntime: HTTP server thread + agent loop on the main thread + exit/exec."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

import uvicorn

from ..agent.build import BuiltAgent
from ..agent.handle import ExitReason
from .app import create_app
from .check import REPO_ROOT
from .control_api import RuntimeInfo
from .errors import HostError
from .upgrade import UpgradeManager

if TYPE_CHECKING:
    from .stages import ValidatedEnv

logger = logging.getLogger(__name__)

_SERVER_START_TIMEOUT = 5.0
_SERVER_STOP_TIMEOUT = 5.0


class HostRuntime:
    def __init__(
        self,
        env: ValidatedEnv,
        built: BuiltAgent,
        *,
        app_factory: Callable[..., object] = create_app,
        exec_fn: Callable[[str, list[str]], None] = os.execv,
    ) -> None:
        self._env = env
        self._built = built
        self._app_factory = app_factory
        self._exec_fn = exec_fn

    def run(self) -> int:
        env, built = self._env, self._built
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        info = RuntimeInfo(
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            git_sha=sha.stdout.strip() if sha.returncode == 0 else "unknown",
        )
        app = self._app_factory(
            handle=built.handle,
            event_store=built.ui_event_store,
            info=info,
            upgrade=UpgradeManager(REPO_ROOT),
            config=env.config,
        )

        server_cfg = env.config.app.server
        # log_config=None: uvicorn loggers propagate to the root stderr handler.
        server = uvicorn.Server(
            uvicorn.Config(app, host=server_cfg.host, port=server_cfg.port, log_config=None)
        )
        thread = threading.Thread(target=server.run, name="lincy-http", daemon=True)
        thread.start()
        deadline = time.monotonic() + _SERVER_START_TIMEOUT
        while not server.started:
            # uvicorn exits its thread on bind failure instead of raising here.
            if not thread.is_alive() or time.monotonic() > deadline:
                server.should_exit = True
                raise HostError(
                    f"HTTP server failed to start on {server_cfg.host}:{server_cfg.port}"
                )
            time.sleep(0.05)
        logger.info("HTTP server listening on %s:%s", server_cfg.host, server_cfg.port)

        built.start()

        def _on_signal(signum, _frame) -> None:
            logger.info("received %s, shutting down", signal.Signals(signum).name)
            built.handle.request_shutdown(graceful=True)

        previous = {
            sig: signal.signal(sig, _on_signal) for sig in (signal.SIGTERM, signal.SIGINT)
        }
        try:
            reason = built.run()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            built.close()
            server.should_exit = True
            thread.join(_SERVER_STOP_TIMEOUT)

        if reason is ExitReason.RESTART:
            logger.info("restarting: exec %s -m lincy start", sys.executable)
            # execv replaces the process without flushing Python's buffers.
            sys.stdout.flush()
            sys.stderr.flush()
            self._exec_fn(sys.executable, [sys.executable, "-m", "lincy", "start"])
        return 0
