"""Environment checks run by the validate stage."""

from __future__ import annotations

import os
import socket
from pathlib import Path

from ..core.schema import AppConfig
from ..gui.ax_runtime import AXRuntimeError, ensure_binary, resolve_build_params
from .errors import EnvironmentCheckFailed

REPO_ROOT = Path(__file__).resolve().parents[3]


def enriched_path() -> str:
    """Return PATH with common tool directories prepended.

    launchd starts processes with a minimal PATH, so uv/bun/swift would
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


def check_environment(config: AppConfig, repo_root: Path, *, probe_port: bool) -> str | None:
    """Fail fast on environment problems; return the AX binary path when GUI is enabled.

    ``probe_port`` is off for ``lincy check``: upgrade runs it as a gate while
    the live server still holds the port.
    """
    server = config.app.server
    if probe_port and not port_is_available(server.host, server.port):
        raise EnvironmentCheckFailed(
            f"Server address {server.host}:{server.port} is already in use "
            "(is lincy already running? try: uv run lincy status)"
        )

    if not (repo_root / "src" / "web_ui" / "dist" / "index.html").is_file():
        raise EnvironmentCheckFailed("Web UI is not built. Run: cd src/web_ui && bun run build")

    params = resolve_build_params(config)
    if params is None:
        return None
    try:
        return ensure_binary(**params)
    except AXRuntimeError as e:
        raise EnvironmentCheckFailed(f"GUI backend unavailable: {e}") from e
