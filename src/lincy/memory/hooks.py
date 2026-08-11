"""Auto-archive temp-memory.md rolling buffer entries older than retain_days."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
import logging
import re

from ..core.schema import MemoryArchiveConfig, MaintenanceCurateConfig
from ..timezone_utils import now as tz_now

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2})")
_DIGEST_RE = re.compile(r"^\s*- \[digest (\d{4}-\d{2}-\d{2})\].*\n?", re.MULTILINE)

_RECENT_REL_PATH = "memory/agent/temp-memory.md"
_RECENT_ARCHIVE_SUBDIR = "memory/archive/temp-memory"


@dataclass
class ArchivedFile:
    """One date-partition written to the archive directory."""

    date: date
    path: Path
    lines: int


@dataclass
class ArchiveResult:
    """Summary of a single archive run."""

    archived: list[ArchivedFile] = field(default_factory=list)

    @property
    def total_lines(self) -> int:
        return sum(f.lines for f in self.archived)

    @property
    def summary(self) -> str:
        if not self.archived:
            return ""
        dates = sorted({f.date for f in self.archived})
        return f"{self.total_lines} lines archived ({dates[0]} ~ {dates[-1]})"


# -- Parser -------------------------------------------------------------------


def _parse_recent_by_date(content: str) -> tuple[str, dict[date, str]]:
    """Parse the rolling buffer into preamble + date-grouped entries.

    Returns (preamble, {date: content}).
    Preamble = everything before the first dated entry (title, blank lines).
    """
    preamble_lines: list[str] = []
    groups: dict[date, list[str]] = {}
    current_date: date | None = None

    for line in content.splitlines(keepends=True):
        m = _DATE_RE.search(line)
        if m:
            current_date = date.fromisoformat(m.group(1))

        if current_date is None:
            preamble_lines.append(line)
        else:
            groups.setdefault(current_date, []).append(line)

    preamble = "".join(preamble_lines)
    return preamble, {d: "".join(lines) for d, lines in groups.items()}


# -- Archive logic -------------------------------------------------------------


def check_and_archive_buffers(
    agent_os_dir: Path,
    config: MemoryArchiveConfig,
    *,
    curate_config: MaintenanceCurateConfig | None = None,
    digest_day: Callable[[date, str, int], str] | None = None,
) -> ArchiveResult:
    """Archive aged buffer entries, retaining digests when curation is enabled."""
    buf_path = agent_os_dir / _RECENT_REL_PATH
    result = ArchiveResult()

    if not buf_path.is_file():
        return result

    content = buf_path.read_text(encoding="utf-8")
    if curate_config is not None and curate_config.enabled:
        content_without_digests = _DIGEST_RE.sub("", content)
        preamble, dated = _parse_recent_by_date(content_without_digests)
    else:
        preamble, dated = _parse_recent_by_date(content)
    if not dated:
        if curate_config is not None and curate_config.enabled:
            _remove_expired_digests(buf_path, content, curate_config.digest_retain_days)
        return result

    today = tz_now().date()
    cutoff = today - timedelta(days=config.retain_days)
    old_dates = sorted(d for d in dated if d < cutoff)
    if not old_dates:
        if curate_config is not None and curate_config.enabled:
            _remove_expired_digests(buf_path, content, curate_config.digest_retain_days)
        return result

    if curate_config is None or not curate_config.enabled:
        return _archive_legacy(buf_path, preamble, dated, old_dates, cutoff, agent_os_dir)
    if digest_day is None:
        # Non-maintenance archive hooks must not drop full text while curation
        # is enabled; maintenance supplies the curator callback.
        return result

    archive_dir = agent_os_dir / _RECENT_ARCHIVE_SUBDIR
    archive_dir.mkdir(parents=True, exist_ok=True)
    retained_dates = sorted(d for d in dated if d >= cutoff)
    retained = preamble + "".join(dated[d] for d in retained_dates)
    retained_digests = _retain_unexpired_digests(
        content,
        today,
        curate_config.digest_retain_days,
    )
    failed_content: list[str] = []
    new_digests: list[str] = []

    for d in old_dates:
        try:
            digest = digest_day(d, dated[d], curate_config.digest_max_chars).strip()
            if not digest:
                raise ValueError("empty digest")
            archived = _write_archive_file(archive_dir, d, dated[d])
        except Exception as exc:
            logger.warning("Temp-memory curation failed for %s: %s", d, exc)
            failed_content.append(dated[d])
            continue
        result.archived.append(archived)
        new_digests.append(_format_digest(d, digest))

    if result.archived:
        # Do not remove any successful source text until every resulting digest
        # can be committed together; a write failure leaves the original intact.
        _atomic_write_text(
            buf_path,
            retained + "".join(failed_content) + "".join(retained_digests + new_digests),
        )
    else:
        _remove_expired_digests(buf_path, content, curate_config.digest_retain_days)

    _update_archive_index(archive_dir)
    if result.archived:
        logger.info("Archived %s: %d dates moved", _RECENT_REL_PATH, len(result.archived))
    return result


def _archive_legacy(
    buf_path: Path,
    preamble: str,
    dated: dict[date, str],
    old_dates: list[date],
    cutoff: date,
    agent_os_dir: Path,
) -> ArchiveResult:
    """Preserve the original archive-and-drop behavior when curation is disabled."""
    result = ArchiveResult()
    archive_dir = agent_os_dir / _RECENT_ARCHIVE_SUBDIR
    archive_dir.mkdir(parents=True, exist_ok=True)
    for d in old_dates:
        result.archived.append(_write_archive_file(archive_dir, d, dated[d]))
    retained = preamble + "".join(dated[d] for d in sorted(dated) if d >= cutoff)
    buf_path.write_text(retained, encoding="utf-8")
    _update_archive_index(archive_dir)
    logger.info("Archived %s: %d dates moved", _RECENT_REL_PATH, len(old_dates))
    return result


def _format_digest(day: date, digest: str) -> str:
    compact = " ".join(digest.splitlines()).strip()
    return (
        f"- [digest {day.isoformat()}] {compact}"
        f"（全文：memory/archive/temp-memory/{day.isoformat()}.md）\n"
    )


def _retain_unexpired_digests(content: str, today: date, retain_days: int) -> list[str]:
    cutoff = today - timedelta(days=retain_days)
    kept: list[str] = []
    for match in _DIGEST_RE.finditer(content):
        if date.fromisoformat(match.group(1)) >= cutoff:
            kept.append(match.group(0))
    return kept


def _remove_expired_digests(buf_path: Path, content: str, retain_days: int) -> None:
    retained = _DIGEST_RE.sub(
        lambda match: (
            match.group(0)
            if date.fromisoformat(match.group(1)) >= tz_now().date() - timedelta(days=retain_days)
            else ""
        ),
        content,
    )
    if retained != content:
        _atomic_write_text(buf_path, retained)


def _atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_archive_file(archive_dir: Path, d: date, content: str) -> ArchivedFile:
    """Write (or append to) a date-partitioned archive file."""
    path = archive_dir / f"{d.isoformat()}.md"
    lines = content.count("\n")
    if path.exists():
        with path.open("a", encoding="utf-8") as f:
            f.write(content)
    else:
        # New archive file gets a date title header
        path.write_text(f"# {d.isoformat()}\n\n{content}", encoding="utf-8")
    return ArchivedFile(date=d, path=path, lines=lines)


def _update_archive_index(archive_dir: Path) -> None:
    """Rebuild index.md listing all date files in the archive directory."""
    md_files = sorted(
        f for f in archive_dir.iterdir()
        if f.suffix == ".md" and f.name != "index.md"
    )
    lines = [f"# {archive_dir.name} archive\n", "\n"]
    for f in md_files:
        lines.append(f"- [{f.stem}]({f.name})\n")
    (archive_dir / "index.md").write_text("".join(lines), encoding="utf-8")
