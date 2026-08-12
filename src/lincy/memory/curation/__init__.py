"""Memory file curation: deterministic scaffolding + worker-driven execution.

Queue, budget rules, and archive-snapshot handling here are pure filesystem
bookkeeping with no LLM calls -- they are the zero-loss guarantee described
in docs/dev/memory-curation.md. The actual rewrite/split of an over-budget
file, and the temp-memory digest text, are produced by the worker subagent
(worker_dispatch.py); there is no dedicated curator agent anymore.
"""

from .queue import load_queue, remove_queue_entry, upsert_queue_entry
from .scan import scan_over_budget_files
from .snapshot import resolve_curation_target, snapshot_path, write_verified_snapshot
from .worker_dispatch import curate_queue_via_worker, digest_day_via_worker

__all__ = [
    "load_queue",
    "remove_queue_entry",
    "upsert_queue_entry",
    "scan_over_budget_files",
    "resolve_curation_target",
    "snapshot_path",
    "write_verified_snapshot",
    "curate_queue_via_worker",
    "digest_day_via_worker",
]
