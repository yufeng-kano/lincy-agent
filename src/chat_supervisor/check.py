"""Environment and configuration preflight checks for chat-supervisor."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from lincy.core.config import load_raw_agent_config

from .config import CFGS_DIR, load_supervisor_config
from .process import topological_sort
from .schema import SupervisorConfig


def _enriched_path() -> str:
    """Return PATH with common tool directories prepended."""
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
    for p in extra:
        if p not in current:
            merged = f"{p}:{merged}"
    return merged


def _check_binary(name: str) -> tuple[bool, str]:
    """Check if a binary is available and return its version."""
    search_path = _enriched_path()
    path = shutil.which(name, path=search_path)
    if path is None:
        return False, "not found"
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        version = result.stdout.strip().split("\n")[0] if result.stdout else "unknown"
        return True, version
    except Exception:
        return True, "installed (version unknown)"


# Map command[0] to the binary and a human-readable label
_KNOWN_TOOLS: dict[str, str] = {
    "uv": "uv",
    "bun": "bun",
    "python": "python3",
    "python3": "python3",
    "node": "node",
    "npm": "npm",
}


def _infer_required_tools(config: SupervisorConfig) -> dict[str, list[str]]:
    """Infer which tools each process needs from its command."""
    result: dict[str, list[str]] = {}
    for name, proc in config.processes.items():
        if not proc.enabled or not proc.command:
            continue
        cmd = proc.command[0]
        tool = _KNOWN_TOOLS.get(cmd, cmd)
        result.setdefault(tool, []).append(name)
        # bun run executes package bins via their shebang. vue-tsc is
        # `#!/usr/bin/env node`; if node is missing, bun silently falls back
        # to its own runtime and vue-tsc then fails with bogus "Cannot find
        # module '*.vue'" errors.
        if tool == "bun":
            result.setdefault("node", []).append(name)
    return result


def run_check(config_path: str = "supervisor.yaml") -> int:
    """Run preflight checks and print results. Returns 0 if all pass, 1 otherwise."""
    config = load_supervisor_config(config_path)
    ok = True

    # --- Tools ---
    required_tools = _infer_required_tools(config)
    print("Environment")
    for tool, procs in sorted(required_tools.items()):
        found, version = _check_binary(tool)
        status = f"ok ({version})" if found else "MISSING"
        if not found:
            ok = False
        print(f"  {tool:<10} {status:<30} used by: {', '.join(procs)}")

    # --- Processes ---
    print()
    print("Processes")
    try:
        startup_order = topological_sort(config.processes)
    except Exception as exc:
        print(f"  ERROR: dependency resolution failed: {exc}")
        return 1

    for name in startup_order:
        proc = config.processes[name]
        deps = f" (depends_on: {', '.join(proc.depends_on)})" if proc.depends_on else ""
        restart = "oneshot" if not proc.auto_restart else "daemon"
        print(f"  {name:<25} {restart:<10}{deps}")

    # --- Paths ---
    print()
    print("Paths")

    agent_yaml = CFGS_DIR / "agent.yaml"
    if agent_yaml.exists():
        override_yaml = CFGS_DIR / "agent.override.yaml"
        if override_yaml.exists():
            print(f"  override      {override_yaml}  (applied)")
        agent_cfg = load_raw_agent_config()
        agent_os_dir = Path(agent_cfg["app"]["agent_os_dir"]).expanduser().resolve()
        sessions_dir = agent_os_dir / "session" / "brain"
        if sessions_dir.exists():
            count = sum(1 for d in sessions_dir.iterdir() if d.is_dir())
            print(f"  sessions_dir  {sessions_dir}  ({count} sessions)")
        else:
            print(f"  sessions_dir  {sessions_dir}  (does not exist)")
            ok = False
    else:
        print(f"  agent.yaml    {agent_yaml}  (not found)")
        ok = False

    ui_dist = Path(__file__).parent.parent / "chat_web_ui" / "dist"
    if ui_dist.exists():
        print(f"  static_dir    {ui_dist}  (built)")
    else:
        print(f"  static_dir    {ui_dist}  (not built yet)")

    # --- Result ---
    print()
    if ok:
        print("All checks passed.")
    else:
        print("Some checks FAILED. Fix the issues above before starting.")
    return 0 if ok else 1
