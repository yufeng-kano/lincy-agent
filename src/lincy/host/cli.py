"""`lincy` command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
import time

import httpx

from ..core.config import load_raw_agent_config
from ..core.schema import ServerConfig
from . import service
from .errors import HostError
from .init import init_command
from .stages import run_check, run_start

_HTTP_TIMEOUT = 10.0
_UPGRADE_POLL_INTERVAL = 1.0
_UPGRADE_TIMEOUT = 15 * 60


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lincy")
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="Run the agent in the foreground")
    session = start.add_mutually_exclusive_group()
    session.add_argument("--new", action="store_true", help="Start a new session")
    session.add_argument("--resume", metavar="ID", help="Resume a specific session id")

    commands.add_parser("check", help="Validate config/workspace and build the agent, then exit")
    commands.add_parser("init", help="Initialize the workspace")
    commands.add_parser("status", help="Show the running agent's health")
    commands.add_parser("stop", help="Ask the running agent to shut down gracefully")
    commands.add_parser("upgrade", help="Pull, verify and restart into the latest code")

    svc = commands.add_parser("service", help="Manage the launchd LaunchAgent")
    svc.add_argument("action", choices=["install", "uninstall", "start", "status"])
    return parser


def _base_url() -> str:
    raw = load_raw_agent_config()
    server = ServerConfig.model_validate(raw.get("app", {}).get("server", {}))
    host = server.host
    if host == "0.0.0.0":
        host = "127.0.0.1"
    elif host == "::":
        host = "[::1]"
    elif ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{server.port}"


def _request(method: str, base: str, path: str) -> dict:
    call = httpx.get if method == "GET" else httpx.post
    try:
        response = call(f"{base}{path}", timeout=_HTTP_TIMEOUT)
    except httpx.TransportError as e:
        raise HostError(f"lincy is not running at {base} ({e})") from e
    payload = response.json()
    if response.status_code >= 400:
        raise HostError(payload.get("error") or f"HTTP {response.status_code}")
    return payload


def _status() -> int:
    health = _request("GET", _base_url(), "/api/agent/health")
    upgrade = health["upgrade"]
    rows = [
        ("state", health["state"]),
        ("pid", health["pid"]),
        ("session", health["session_id"] or "-"),
        ("started", health["started_at"]),
        ("git sha", health["git_sha"]),
        ("upgrade", upgrade["state"]),
        ("web", health["web"]),
    ]
    if upgrade["error"]:
        rows.append(("upgrade error", upgrade["error"]))
    for key, value in rows:
        print(f"{key:<14}{value}")
    return 0


def _stop() -> int:
    _request("POST", _base_url(), "/api/agent/shutdown")
    print("shutdown requested")
    return 0


def _upgrade() -> int:
    base = _base_url()
    before_sha = _request("GET", base, "/api/agent/health")["git_sha"]
    started = _request("POST", base, "/api/agent/upgrade")
    print(f"upgrade started from {started['from_sha']}")

    deadline = time.monotonic() + _UPGRADE_TIMEOUT
    last_state = None
    while time.monotonic() < deadline:
        time.sleep(_UPGRADE_POLL_INTERVAL)
        try:
            health = _request("GET", base, "/api/agent/health")
        except HostError:
            # Expected while the process execs into the new version.
            continue
        if health["git_sha"] != before_sha:
            print(f"upgraded {before_sha} -> {health['git_sha']}")
            return 0
        upgrade = health["upgrade"]
        if upgrade["state"] != last_state:
            last_state = upgrade["state"]
            print(f"  {last_state}")
        if upgrade["state"] == "up_to_date":
            print("already up to date")
            return 0
        if upgrade["state"] == "failed":
            print(f"upgrade failed: {upgrade['error']}", file=sys.stderr)
            return 1
    raise HostError("upgrade did not finish within 15 minutes")


def _service(action: str) -> int:
    {
        "install": service.install,
        "uninstall": service.uninstall,
        "start": service.start,
        "status": service.status,
    }[action]()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command in ("start", "check"):
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            stream=sys.stderr,
        )
    try:
        match args.command:
            case "start":
                return run_start(new_session=args.new, resume_id=args.resume)
            case "check":
                return run_check()
            case "init":
                init_command()
                return 0
            case "status":
                return _status()
            case "stop":
                return _stop()
            case "upgrade":
                return _upgrade()
            case "service":
                return _service(args.action)
    except HostError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled command: {args.command}")
