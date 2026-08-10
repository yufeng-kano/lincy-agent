"""Security utilities for path validation and shell command guards."""

from functools import lru_cache
from pathlib import Path
import re


def resolve_allowed_path(
    path: str,
    allowed_paths: list[str],
    base_dir: Path,
) -> Path | None:
    """Resolve a path and return it only when it is allowed."""
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = base_dir / target
    target = target.resolve()
    base_dir = base_dir.expanduser().resolve()

    allowed = allowed_paths or [str(base_dir)]
    for allowed_path_text in allowed:
        allowed_path = Path(allowed_path_text).expanduser().resolve()
        try:
            target.relative_to(allowed_path)
            return target
        except ValueError:
            continue
    return None


def is_path_allowed(path: str, allowed_paths: list[str], base_dir: Path) -> bool:
    """Check whether a path resolves within an allowed directory."""
    return resolve_allowed_path(path, allowed_paths, base_dir) is not None


_BLACKLIST_HINTS: dict[str, str] = {
    "python": "Use 'uv run python ...' or 'uv run script.py' instead",
    "pip": "Use 'uv add <package>' or 'uv run --with <package>' instead",
}


def check_shell_command(command: str, blacklist: list[str] | tuple[re.Pattern[str], ...]) -> str | None:
    """Return a standard error for a blacklisted command, if any."""
    for pattern in blacklist:
        compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
        if compiled.search(command):
            hint = next(
                (hint for keyword, hint in _BLACKLIST_HINTS.items() if keyword in compiled.pattern),
                None,
            )
            message = f"Error: Command blocked by pattern '{compiled.pattern}'"
            return f"{message}. {hint}" if hint else message
    return None


def clamp_timeout(requested: int | None, default: int) -> int:
    """Use the requested timeout without allowing it below the default."""
    return max(requested if requested is not None else default, default)


MAX_OUTPUT_SIZE = 100 * 1024
_OUTPUT_TRUNCATED_SUFFIX = "\n... (output truncated)"


def truncate_output(text: str) -> str:
    """Bound shell output to the shared maximum size."""
    if len(text) <= MAX_OUTPUT_SIZE:
        return text
    return text[:MAX_OUTPUT_SIZE] + _OUTPUT_TRUNCATED_SUFFIX


@lru_cache(maxsize=None)
def build_memory_shell_write_patterns(
    agent_os_dir: Path,
) -> tuple[re.Pattern[str], ...]:
    """Build shell patterns that indicate direct memory writes."""
    memory_abs = re.escape(str((agent_os_dir / "memory").resolve()))
    memory_rel = r"(?:\./)?(?:\.agent/)?memory/"
    memory_target = rf"(?:['\"])?(?:{memory_rel}|{memory_abs}/)"
    return (
        re.compile(rf">>?\s*{memory_target}"),
        re.compile(rf"\btee(?:\s+-a)?\b[^\n]*\s{memory_target}"),
        re.compile(rf"\bsed\s+-i(?:\S*)?\b[^\n]*\s{memory_target}"),
        re.compile(rf"\brm\s[^\n]*{memory_target}"),
        re.compile(rf"\bmv\s[^\n]*{memory_target}"),
    )


def is_memory_write_shell_command(command: str, *, agent_os_dir: Path) -> bool:
    """Check if a shell command writes directly under memory/."""
    return any(
        pattern.search(command) is not None
        for pattern in build_memory_shell_write_patterns(agent_os_dir)
    )
