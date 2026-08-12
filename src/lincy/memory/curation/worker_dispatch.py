"""Maintenance-driven file curation via the worker subagent (design section 3).

The retired memory_curator agent applied its LLM output directly, with no
archive-snapshot guarantee, and ran alongside brain's own ad-hoc worker
dispatches against the same files (kernel/builtin-skills/memory-maintenance)
-- two systems rewriting the same file. Maintenance now drives the worker
directly: the deterministic scaffolding in this package only snapshots
before rewrite and tracks queue membership. The worker performs the actual
rewrite/split using its own file tools, per the memory-maintenance skill
rules attached as context.

Every call here goes through the same WorkerRunner instance brain's async
`worker` tool uses, so it inherits that runner's session debug logging
(docs/dev/session-debug-logs.md) -- curation LLM calls are no longer
invisible there.
"""

from __future__ import annotations

from datetime import date
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ...timezone_utils import now as tz_now
from .queue import load_queue, remove_queue_entry
from .snapshot import resolve_curation_target, snapshot_path, write_verified_snapshot

if TYPE_CHECKING:
    from ...worker.runner import WorkerRunner

logger = logging.getLogger(__name__)

_MAINTENANCE_RULES_REL_PATH = "kernel/builtin-skills/memory-maintenance/references/rules.md"


def digest_day_via_worker(
    runner: "WorkerRunner",
    day: date,
    content: str,
    max_chars: int,
) -> str:
    """Ask the worker to distill one dated temp-memory partition into a digest.

    Read-only task: this only produces the digest body text. Code (see
    hooks._format_digest) assembles and writes the final digest line, and
    is responsible for stripping any marker prefix the model echoes back.
    """
    prompt = (
        "Summarize the memory entries below from "
        f"{day.isoformat()} into a single digest of at most {max_chars} "
        "characters. Preserve lessons learned, agreements, emotional "
        "context, and open follow-ups -- do not just compress into a flat "
        "log. Respond with ONLY the digest body text: no markers, no "
        "headers, no leading date/bracket prefix, no file writes.\n\n"
        f"{content}"
    )
    result = runner.run(prompt, worker_label="maintenance-digest")
    if not result.success or not result.text.strip():
        raise ValueError(f"worker digest failed: {result.error or 'empty output'}")
    return result.text.strip()


def curate_queue_via_worker(runner: "WorkerRunner", agent_os_dir: Path) -> None:
    """Snapshot and dispatch a worker rewrite for each queued over-budget file.

    Success removes the queue entry. Failure leaves the queue entry
    untouched for retry on the next maintenance run; the only thing
    guaranteed to have already happened is the verified archive snapshot,
    which is the actual zero-loss guarantee (fail = no slimming today,
    never memory loss).
    """
    entries = load_queue(agent_os_dir)
    for entry in entries:
        rel_path = entry.get("path")
        if not isinstance(rel_path, str):
            logger.warning("Ignoring malformed memory curation queue entry")
            continue
        try:
            _curate_one_file(runner, agent_os_dir, rel_path)
        except Exception as exc:
            logger.warning("Memory curation failed for %s: %s", rel_path, exc)
            continue
        remove_queue_entry(agent_os_dir, rel_path)


def _curate_one_file(runner: "WorkerRunner", agent_os_dir: Path, rel_path: str) -> None:
    target = resolve_curation_target(agent_os_dir, rel_path)
    source = target.read_text(encoding="utf-8")
    archive_rel_path = snapshot_path(rel_path, tz_now().date())
    write_verified_snapshot(agent_os_dir / archive_rel_path, source)

    result = runner.run(
        _build_curation_prompt(
            rel_path=rel_path, archive_rel_path=archive_rel_path.as_posix()
        ),
        context_files=[_MAINTENANCE_RULES_REL_PATH],
        agent_os_dir=agent_os_dir,
        worker_label="maintenance-curation",
    )
    if not result.success:
        raise RuntimeError(f"worker curation failed: {result.error or 'truncated/incomplete'}")


def _build_curation_prompt(*, rel_path: str, archive_rel_path: str) -> str:
    return (
        "This is a memory-maintenance task: you have permission to edit "
        f"memory/ files, scoped to {rel_path} and, if you split it, new "
        "topic files in the same directory plus that directory's "
        "index.md. Follow the attached maintenance rules exactly.\n\n"
        f"Target file (relative to workspace root): {rel_path}\n"
        f"The full original text is already safely archived at "
        f"{archive_rel_path} -- do not modify anything under "
        "memory/archive/, it is reference only.\n\n"
        "The file is over its character budget. Reorganize it: either "
        "condense it in place (current state + condensed history), or "
        "split it by topic into new files in the same directory. If you "
        "split it, register every new file in that directory's "
        "index.md.\n\n"
        "Completion requires: zero content loss (everything either kept "
        "or safely represented in the new organization), format "
        "compliant with the attached rules, and correct index.md links. "
        "Edit with read_file / edit_file / write_file only.\n\n"
        "Report: files changed, a one-line summary of each change, and "
        "any item you could not complete."
    )
