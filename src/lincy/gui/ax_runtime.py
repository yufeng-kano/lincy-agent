"""Build and locate the vendored OpenComputerUse MCP server binary.

The AX-first GUI stack drives a local MCP server (open-codex-computer-use,
MIT) built from a pinned commit. The binary is cached per-commit under the
user cache dir so `lincy start` / `lincy check` only pay the build cost once.

Run as a module (``python -m lincy.gui.ax_runtime``) to pre-build the binary
by hand; the validate stage calls ensure_binary() itself.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)

DEFAULT_REPO = "https://github.com/iFurySt/open-codex-computer-use.git"
# Audited at this commit: no network APIs, no external Swift dependencies.
# Git commit sha, not a credential.
DEFAULT_COMMIT = "c8a7758d50e1230f34161fb1867f6f0df5776db3"  # pragma: allowlist secret
_PRODUCT = "OpenComputerUse"


class AXRuntimeError(RuntimeError):
    """Raised when the MCP server binary cannot be provisioned."""


def default_cache_root() -> str:
    return os.path.join(
        os.path.expanduser("~"), ".cache", "lincy", "ocu",
    )


def binary_path(commit: str = DEFAULT_COMMIT, cache_root: str | None = None) -> str:
    root = cache_root or default_cache_root()
    return os.path.join(root, commit[:12], _PRODUCT)


def _run(cmd: list[str], cwd: str | None = None, env: dict | None = None) -> None:
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    result = subprocess.run(
        cmd, cwd=cwd, env=merged_env,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-2000:]
        raise AXRuntimeError(
            f"command failed ({' '.join(cmd[:3])}...): {tail}"
        )


def _fetch_pinned_source(repo: str, commit: str, src_dir: str) -> None:
    """Shallow-fetch exactly one commit; skip LFS (upstream LFS budget is
    exhausted and LFS objects are reverse-engineering assets we never need)."""
    lfs_env = {"GIT_LFS_SKIP_SMUDGE": "1"}
    if not os.path.isdir(os.path.join(src_dir, ".git")):
        os.makedirs(src_dir, exist_ok=True)
        _run(["git", "init", "-q"], cwd=src_dir)
        _run(["git", "remote", "add", "origin", repo], cwd=src_dir)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=src_dir, capture_output=True, text=True,
    )
    if head.returncode == 0 and head.stdout.strip() == commit:
        return
    _run(
        ["git", "fetch", "-q", "--depth", "1", "origin", commit],
        cwd=src_dir, env=lfs_env,
    )
    _run(
        ["git", "checkout", "-q", "-f", "FETCH_HEAD"],
        cwd=src_dir, env=lfs_env,
    )


def ensure_binary(
    *,
    repo: str = DEFAULT_REPO,
    commit: str = DEFAULT_COMMIT,
    cache_root: str | None = None,
    override_path: str | None = None,
) -> str:
    """Return path to a ready MCP server binary, building it if needed.

    Fails fast with an actionable message when prerequisites are missing,
    so problems surface in the validate stage instead of the first GUI task.
    """
    if override_path:
        expanded = os.path.expanduser(override_path)
        if not os.path.isfile(expanded):
            raise AXRuntimeError(
                f"gui_manager.ax.binary_path points to a missing file: {expanded}"
            )
        return expanded

    target = binary_path(commit, cache_root)
    if os.path.isfile(target):
        return target

    if shutil.which("swift") is None:
        raise AXRuntimeError(
            "swift toolchain not found; install Xcode Command Line Tools "
            "(xcode-select --install) so the OpenComputerUse MCP server "
            "can be built"
        )
    if shutil.which("git") is None:
        raise AXRuntimeError("git not found; required to fetch pinned source")

    root = cache_root or default_cache_root()
    src_dir = os.path.join(root, f"src-{commit[:12]}")
    logger.info("Fetching OpenComputerUse source (%s)", commit[:12])
    _fetch_pinned_source(repo, commit, src_dir)

    logger.info("Building OpenComputerUse (first run; takes a few minutes)")
    _run(
        ["swift", "build", "-c", "release", "--product", _PRODUCT],
        cwd=src_dir,
    )

    built = os.path.join(src_dir, ".build", "release", _PRODUCT)
    if not os.path.isfile(built):
        raise AXRuntimeError(f"build succeeded but binary missing: {built}")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(built, target)
    return target


def resolve_build_params(config: object | None) -> dict | None:
    """Map an AppConfig to ensure_binary kwargs; None means skip the build.

    Skips when gui_manager is absent or disabled. Config overrides
    (repo/commit/binary_path) are honored so a manual pre-build produces
    the same binary the agent will use.
    """
    if config is None:
        return {}
    gm = getattr(config, "agents", {}).get("gui_manager")
    if gm is None or not gm.enabled:
        return None
    params: dict = {"override_path": gm.ax.binary_path}
    if gm.ax.repo:
        params["repo"] = gm.ax.repo
    if gm.ax.commit:
        params["commit"] = gm.ax.commit
    return params


def _load_app_config() -> object | None:
    try:
        from ..core.config import load_config

        return load_config("cfgs/agent.yaml")
    except Exception as e:
        logger.warning("agent.yaml unreadable (%s); building defaults", e)
        return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    params = resolve_build_params(_load_app_config())
    if params is None:
        print("[ax-server-build] gui_manager disabled; skipping build")
        return 0
    try:
        path = ensure_binary(**params)
    except AXRuntimeError as e:
        print(f"[ax-server-build] {e}", file=sys.stderr)
        return 1
    print(f"[ax-server-build] ready: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
