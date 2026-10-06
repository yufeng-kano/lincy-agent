"""Environment checks run by the validate stage."""

from __future__ import annotations

import logging
import os
import shutil
import socket
from pathlib import Path

from ..core.schema import AppConfig
from ..gui.input_source import list_input_sources
from ..gui.permissions import check_gui_permissions
from .errors import EnvironmentCheckFailed
from .web_ui import WebUIBuildFailed, build_web_ui, web_ui_is_current

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]

# git/uv for upgrade, bun for the web UI build, node because vue-tsc's shebang
# is `#!/usr/bin/env node` (bun silently falls back to its own runtime and the
# build fails with bogus "cannot find module '*.vue'" errors without it).
REQUIRED_BINARIES = ("git", "uv", "bun", "node")


def enriched_path() -> str:
    """Return PATH with common tool directories prepended.

    launchd starts processes with a minimal PATH, so uv/bun/node would
    otherwise be missing for the service and for upgrade subprocesses.
    """
    home = Path.home()
    extra = [
        str(home / ".local" / "bin"),
        str(home / ".bun" / "bin"),
        "/opt/homebrew/bin",
        str(home / ".cargo" / "bin"),
        "/usr/local/bin",
    ]
    current = os.environ.get("PATH", "")
    merged = current
    for path in extra:
        if path not in current.split(":"):
            merged = f"{path}:{merged}"
    return merged


def port_is_available(host: str, port: int) -> bool:
    """Return False when the bind address is already occupied."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    bind_host = host
    if host == "localhost":
        bind_host = "127.0.0.1"
        family = socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((bind_host, port))
        except OSError:
            return False
    return True


def check_environment(
    config: AppConfig, repo_root: Path, agent_os_dir: Path, *, probe_port: bool,
) -> None:
    """Fail fast on environment problems; rebuild the web UI when its sources changed.

    ``probe_port`` is off for ``lincy check``: upgrade runs it as a gate while
    the live server still holds the port.
    """
    server = config.app.server
    if probe_port and not port_is_available(server.host, server.port):
        raise EnvironmentCheckFailed(
            f"Server address {server.host}:{server.port} is already in use "
            "(is lincy already running? try: uv run lincy status)"
        )

    search_path = enriched_path()
    missing = [name for name in REQUIRED_BINARIES if shutil.which(name, path=search_path) is None]
    if missing:
        raise EnvironmentCheckFailed(
            f"Required binaries not found on PATH: {', '.join(missing)}"
        )

    # Building here (not only in upgrade) covers a fresh clone and a manual
    # git pull; the fingerprint keeps a normal restart from rebuilding.
    web_ui_dir = repo_root / "src" / "web_ui"
    if not web_ui_is_current(web_ui_dir):
        logger.info("Web UI dist is missing or stale; running bun install + bun run build")
        try:
            build_web_ui(web_ui_dir, {**os.environ, "PATH": search_path})
        except WebUIBuildFailed as e:
            raise EnvironmentCheckFailed(
                f"Web UI build failed. {e}\n"
                "Fix it, or run by hand: cd src/web_ui && bun install && bun run build"
            ) from e

    # A GUI agent without Accessibility / Screen Recording is blind, so a
    # missing permission stops startup instead of degrading at task time.
    gui_manager = config.agents.get("gui_manager")
    if gui_manager is not None and gui_manager.enabled:
        problems = check_gui_permissions(agent_os_dir / "state")
        if problems:
            raise EnvironmentCheckFailed("\n".join(problems))
        # Every GUI task starts by selecting this source; an unknown id would
        # fail each task at runtime instead of here.
        default_source = gui_manager.desktop.default_input_source
        available = list_input_sources()
        if default_source not in available:
            raise EnvironmentCheckFailed(
                f"gui_manager.desktop.default_input_source {default_source!r} is not "
                f"an enabled input source; available: {', '.join(available)}"
            )
