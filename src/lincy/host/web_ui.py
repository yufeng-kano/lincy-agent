"""Web UI build: bun install + bun run build, stamped with a source fingerprint."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Callable

from .errors import HostError

RunCmd = Callable[[list[str], Path, dict[str, str]], subprocess.CompletedProcess]

# The stamp lives in dist/ because vite empties dist/ before building: a failed
# or interrupted build leaves no stamp, so the next start rebuilds.
_STAMP = Path("dist") / ".lincy-source-fingerprint"
_SKIP_DIRS = frozenset({"node_modules", "dist"})
_SKIP_FILES = frozenset({".DS_Store"})
_STDERR_TAIL = 2000

# --frozen-lockfile: a pull that changes package.json without bun.lock should
# fail the build instead of silently resolving new versions on the host.
_BUILD_STEPS = (
    ("bun install", ["bun", "install", "--frozen-lockfile"]),
    ("bun run build", ["bun", "run", "build"]),
)


class WebUIBuildFailed(HostError):
    pass


def run_subprocess(cmd: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)


def source_fingerprint(web_ui_dir: Path) -> str:
    """Hash every source file (path + content), including uncommitted edits."""
    digest = hashlib.sha256()
    files = []
    for root, dirs, names in os.walk(web_ui_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        files.extend(Path(root, name) for name in names if name not in _SKIP_FILES)
    for path in sorted(files):
        digest.update(path.relative_to(web_ui_dir).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def web_ui_is_current(web_ui_dir: Path) -> bool:
    stamp = web_ui_dir / _STAMP
    if not (web_ui_dir / "dist" / "index.html").is_file() or not stamp.is_file():
        return False
    return stamp.read_text() == source_fingerprint(web_ui_dir)


def build_web_ui(web_ui_dir: Path, env: dict[str, str], run_cmd: RunCmd = run_subprocess) -> None:
    """Install dependencies, build dist/, then record what it was built from."""
    for name, cmd in _BUILD_STEPS:
        result = run_cmd(cmd, web_ui_dir, env)
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "")[-_STDERR_TAIL:]
            raise WebUIBuildFailed(f"{name} failed (exit {result.returncode}): {tail}")
    (web_ui_dir / _STAMP).write_text(source_fingerprint(web_ui_dir))
