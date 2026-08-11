"""Memory editor service — instruction planning + deterministic apply."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re

from ...core.schema import MemoryEditWarningsConfig
from ...workspace.people import sync_people_index_entry
from ..index_kind import IndexKind, classify_memory_index_path, is_registry_index_path
from .apply import (
    ApplyOutcome,
    apply_operation,
    delete_index_for_cleanup,
    remove_index_link,
    resolve_memory_path,
    _ensure_index_link,
)
from .planner import MemoryEditPlanner
from .schema import (
    AppliedItem,
    ErrorItem,
    MemoryEditBatch,
    MemoryEditOperation,
    MemoryEditPlan,
    MemoryEditRequest,
    MemoryEditResult,
    WarningItem,
)
from .session_log import SessionCommitLog

logger = logging.getLogger(__name__)

_MAX_PARALLEL_TARGET_FILES = max(1, min(8, os.cpu_count() or 1))

_WARNING_DUPLICATE_THRESHOLD = 0.7
_LONG_TERM_REL_PATH = "memory/agent/long-term.md"
_TEMP_MEMORY_REL_PATH = "memory/agent/temp-memory.md"
_LONG_TERM_REQUIRED_SECTIONS = ("## 核心價值", "## 約定", "## 清單", "## 重要記錄")
_LONG_TERM_CORE_VALUE_MAX = 5
# Core values: free-text bullets (no date prefix required)
_LONG_TERM_CORE_VALUE_LINE = re.compile(r"^-\s+.+$")
_LONG_TERM_RULE_LINE = re.compile(
    r"^-\s*\[[ xX]\]\s*\[\d{4}-\d{2}-\d{2}\]\s+[^:\n]+:\s+.+$"
)
_LONG_TERM_LIST_LINE = re.compile(r"^-\s*\[\d{4}-\d{2}-\d{2}\]\s+.+$")
_LONG_TERM_RECORD_LINE = re.compile(r"^-\s*\[\d{4}-\d{2}-\d{2}\]\s+.+$")
_TEMP_MEMORY_ENTRY_LINE = re.compile(
    r"^-\s*\[\d{4}-\d{2}-\d{2}(?:\s+\([^)]+\))?\s+\d{2}:\d{2}\]\s+.+$"
)


@dataclass
class _IndexedRequest:
    """One request plus its original batch index."""

    index: int
    request: MemoryEditRequest


@dataclass
class _IndexedOutcome:
    """Apply result keyed by original request index."""

    index: int
    applied: AppliedItem | None = None
    error: ErrorItem | None = None


class MemoryEditor:
    """Plan and apply memory operations with idempotency tracking."""

    def __init__(
        self,
        *,
        commit_log: SessionCommitLog,
        planner: MemoryEditPlanner,
        warnings_config: MemoryEditWarningsConfig | None = None,
    ) -> None:
        self.commit_log = commit_log
        self.planner = planner
        self.warnings_config = warnings_config or MemoryEditWarningsConfig()

    def apply_batch(
        self,
        batch: MemoryEditBatch,
        *,
        allowed_paths: list[str],
        base_dir: Path,
    ) -> MemoryEditResult:
        """Apply all requests, parallelized across distinct target files."""
        indexed_outcomes: dict[int, _IndexedOutcome] = {}
        requests_by_target: dict[Path, list[_IndexedRequest]] = {}

        for index, req in enumerate(batch.requests):
            try:
                target = resolve_memory_path(
                    req.target_path,
                    allowed_paths=allowed_paths,
                    base_dir=base_dir,
                )
            except ValueError as e:
                indexed_outcomes[index] = _IndexedOutcome(
                    index=index,
                    error=ErrorItem(
                        request_id=req.request_id,
                        code="path_invalid",
                        detail=str(e),
                    ),
                )
                continue

            if target.exists() and not target.is_file():
                indexed_outcomes[index] = _IndexedOutcome(
                    index=index,
                    error=ErrorItem(
                        request_id=req.request_id,
                        code="not_a_file",
                        detail=str(target),
                    ),
                )
                continue

            requests_by_target.setdefault(target, []).append(
                _IndexedRequest(index=index, request=req)
            )

        grouped_outcomes = self._apply_grouped_requests(
            batch=batch,
            requests_by_target=requests_by_target,
            base_dir=base_dir,
        )
        indexed_outcomes.update(grouped_outcomes)

        applied: list[AppliedItem] = []
        errors: list[ErrorItem] = []
        all_warnings: list[WarningItem] = []
        for index, req in enumerate(batch.requests):
            outcome = indexed_outcomes.get(index)
            if outcome is None:
                errors.append(
                    ErrorItem(
                        request_id=req.request_id,
                        code="internal_error",
                        detail="missing_request_outcome",
                    )
                )
                continue
            if outcome.applied is not None:
                applied.append(outcome.applied)
                continue
            if outcome.error is not None:
                errors.append(outcome.error)
                continue
            errors.append(
                ErrorItem(
                    request_id=req.request_id,
                    code="internal_error",
                    detail="invalid_request_outcome",
                )
            )

        # Post-batch warnings: check applied targets for file health
        warned_paths: set[str] = set()
        for item in applied:
            if item.status != "applied" or item.path in warned_paths:
                continue
            try:
                target = resolve_memory_path(
                    item.path,
                    allowed_paths=allowed_paths,
                    base_dir=base_dir,
                )
            except ValueError:
                continue
            # Find what operations were planned for this target
            target_reqs = requests_by_target.get(target, [])
            had_append = any(req.request.instruction for req in target_reqs)
            if had_append:
                ws = _check_file_warnings(
                    target,
                    item.path,
                    self.warnings_config,
                    agent_os_dir=base_dir,
                )
                all_warnings.extend(ws)
                warned_paths.add(item.path)

        _sync_people_registry_after_batch(
            applied=applied,
            base_dir=base_dir,
            as_of=batch.as_of,
        )

        return MemoryEditResult(
            status="ok" if not errors else "failed",
            turn_id=batch.turn_id,
            applied=applied,
            errors=errors,
            warnings=all_warnings,
        )

    def _apply_grouped_requests(
        self,
        *,
        batch: MemoryEditBatch,
        requests_by_target: dict[Path, list[_IndexedRequest]],
        base_dir: Path,
    ) -> dict[int, _IndexedOutcome]:
        """Apply grouped requests; each target file is processed in isolation."""
        if not requests_by_target:
            return {}

        groups = list(requests_by_target.items())
        max_workers = min(len(groups), _MAX_PARALLEL_TARGET_FILES)
        if max_workers <= 1:
            outcomes: dict[int, _IndexedOutcome] = {}
            for target, indexed_requests in groups:
                for outcome in self._apply_target_requests(
                    batch=batch,
                    target=target,
                    indexed_requests=indexed_requests,
                    base_dir=base_dir,
                ):
                    outcomes[outcome.index] = outcome
            return outcomes

        outcomes = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    self._apply_target_requests,
                    batch=batch,
                    target=target,
                    indexed_requests=indexed_requests,
                    base_dir=base_dir,
                )
                for target, indexed_requests in groups
            ]
            for future in futures:
                for outcome in future.result():
                    outcomes[outcome.index] = outcome
        return outcomes

    def _apply_target_requests(
        self,
        *,
        batch: MemoryEditBatch,
        target: Path,
        indexed_requests: list[_IndexedRequest],
        base_dir: Path,
    ) -> list[_IndexedOutcome]:
        """Apply one target file's requests sequentially to preserve file ordering."""
        outcomes: list[_IndexedOutcome] = []
        for indexed in indexed_requests:
            request_outcome = self._apply_one_request(
                batch=batch,
                target=target,
                req=indexed.request,
                base_dir=base_dir,
            )
            outcomes.append(
                _IndexedOutcome(
                    index=indexed.index,
                    applied=request_outcome
                    if isinstance(request_outcome, AppliedItem)
                    else None,
                    error=request_outcome
                    if isinstance(request_outcome, ErrorItem)
                    else None,
                )
            )
        return outcomes

    def _apply_one_request(
        self,
        *,
        batch: MemoryEditBatch,
        target: Path,
        req: MemoryEditRequest,
        base_dir: Path,
    ) -> AppliedItem | ErrorItem:
        """Apply one request end-to-end with request-level rollback on failure."""
        if target.exists() and not target.is_file():
            return ErrorItem(
                request_id=req.request_id,
                code="not_a_file",
                detail=str(target),
            )

        file_exists = target.exists()
        is_temp_memory = _is_temp_memory_path(req.target_path)
        file_content = (
            ""
            if is_temp_memory
            else target.read_text(encoding="utf-8")
            if file_exists
            else ""
        )
        plan = self.planner.plan(
            request=req,
            as_of=batch.as_of,
            turn_id=batch.turn_id,
            file_exists=file_exists,
            file_content=file_content,
            file_content_available=not is_temp_memory,
        )
        if plan.status != "ok":
            return ErrorItem(
                request_id=req.request_id,
                code=plan.error_code or "instruction_not_actionable",
                detail=plan.error_detail or req.instruction,
            )

        payload_hash = _operations_hash(plan.operations)
        if self.commit_log.is_applied(batch.turn_id, req.request_id, payload_hash):
            return AppliedItem(
                request_id=req.request_id,
                status="already_applied",
                path=req.target_path,
            )

        if is_temp_memory:
            original_size = target.stat().st_size if file_exists else None
            temp_outcome = _apply_temp_memory_append_only(target, plan)
            if temp_outcome.status == "error":
                _rollback_temp_memory_append(
                    target=target,
                    existed=file_exists,
                    original_size=original_size,
                )
                return ErrorItem(
                    request_id=req.request_id,
                    code=temp_outcome.code or "apply_failed",
                    detail=temp_outcome.detail or "unknown_failure",
                )
            self.commit_log.mark_applied(batch.turn_id, req.request_id, payload_hash)
            return AppliedItem(
                request_id=req.request_id,
                status="applied" if temp_outcome.status == "applied" else "noop",
                path=req.target_path,
            )

        request_changed = False
        failed_outcome = None
        for operation in plan.operations:
            outcome = apply_operation(
                target,
                operation,
                base_dir=base_dir,
            )
            if outcome.status == "error":
                failed_outcome = outcome
                break
            if outcome.status == "applied":
                request_changed = True

        if failed_outcome is not None:
            _rollback_request_file(
                target=target,
                existed=file_exists,
                original_content=file_content,
            )
            return ErrorItem(
                request_id=req.request_id,
                code=failed_outcome.code or "apply_failed",
                detail=failed_outcome.detail or "unknown_failure",
            )

        if request_changed:
            validation_outcome = _validate_target_file_after_apply(
                target=target,
                rel_path=req.target_path,
            )
            if validation_outcome is not None:
                _rollback_request_file(
                    target=target,
                    existed=file_exists,
                    original_content=file_content,
                )
                return ErrorItem(
                    request_id=req.request_id,
                    code=validation_outcome.code or "post_apply_validation_failed",
                    detail=validation_outcome.detail or "invalid file structure",
                )

        self.commit_log.mark_applied(batch.turn_id, req.request_id, payload_hash)

        # Post-apply: auto-maintain parent index.md
        if request_changed:
            _auto_maintain_index(
                target=target,
                plan=plan,
                base_dir=base_dir,
            )

        return AppliedItem(
            request_id=req.request_id,
            status="applied" if request_changed else "noop",
            path=req.target_path,
        )


def _is_temp_memory_path(rel_path: str) -> bool:
    return rel_path.strip().replace("\\", "/") == _TEMP_MEMORY_REL_PATH


def _apply_temp_memory_append_only(target: Path, plan: MemoryEditPlan) -> ApplyOutcome:
    """Append temp-memory payloads without reading existing file content."""
    payloads: list[str] = []
    for operation in plan.operations:
        if operation.kind != "append_entry":
            return ApplyOutcome(
                status="error",
                code="temp_memory_append_only",
                detail="temp-memory.md only supports append_entry operations",
            )
        normalized = _normalize_temp_memory_payload(operation.payload_text or "")
        validation = _validate_temp_memory_payload(normalized)
        if validation is not None:
            return validation
        payloads.append(normalized)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            for payload in payloads:
                f.write(payload)
    except Exception as e:
        return ApplyOutcome(status="error", code="apply_exception", detail=str(e))

    return ApplyOutcome(status="applied")


def _normalize_temp_memory_payload(payload: str) -> str:
    text = payload.strip()
    if not text:
        return ""
    return text + "\n"


def _validate_temp_memory_payload(payload: str) -> ApplyOutcome | None:
    lines = [line.strip() for line in payload.splitlines() if line.strip()]
    if not lines:
        return ApplyOutcome(
            status="error",
            code="temp_memory_format_invalid",
            detail="append_entry payload_text must contain at least one entry",
        )
    for line in lines:
        if not _TEMP_MEMORY_ENTRY_LINE.match(line):
            return ApplyOutcome(
                status="error",
                code="temp_memory_format_invalid",
                detail=(
                    "append_entry for temp-memory.md must use "
                    "'- [YYYY-MM-DD HH:MM] ...' lines"
                ),
            )
    return None


def _rollback_temp_memory_append(
    *,
    target: Path,
    existed: bool,
    original_size: int | None,
) -> None:
    if existed:
        with target.open("r+b") as f:
            f.truncate(original_size or 0)
        return
    if target.exists() and target.is_file():
        target.unlink()


def _sync_people_registry_after_batch(
    *,
    applied: list[AppliedItem],
    base_dir: Path,
    as_of: str,
) -> None:
    """Sync memory/people/index.md for any user directories touched in this batch."""
    user_ids: set[str] = set()
    seen_date = _coerce_as_of_date(as_of)

    for item in applied:
        if item.status != "applied":
            continue
        if is_registry_index_path(item.path):
            continue
        user_id = _people_user_id_from_memory_path(item.path)
        if user_id is not None:
            user_ids.add(user_id)

    if not user_ids:
        return

    memory_dir = base_dir / "memory"
    for user_id in sorted(user_ids):
        try:
            sync_people_index_entry(memory_dir, user_id, seen_date=seen_date)
        except Exception:
            logger.warning(
                "Failed to sync people index for user_id=%s", user_id, exc_info=True
            )


def _people_user_id_from_memory_path(path: str) -> str | None:
    normalized = str(path).strip().replace("\\", "/")
    parts = normalized.split("/")
    if len(parts) < 4:
        return None
    if parts[0] != "memory" or parts[1] != "people":
        return None
    user_id = parts[2].strip()
    if not user_id or user_id == "index.md":
        return None
    return user_id


def _coerce_as_of_date(as_of: str) -> str:
    candidate = (as_of or "")[:10]
    try:
        date.fromisoformat(candidate)
        return candidate
    except ValueError:
        return date.today().isoformat()


def _to_memory_rel_path(path: Path, *, base_dir: Path) -> str | None:
    try:
        rel = path.resolve().relative_to(base_dir.resolve())
    except ValueError:
        return None
    return rel.as_posix()


def _validate_target_file_after_apply(
    *,
    target: Path,
    rel_path: str,
) -> ApplyOutcome | None:
    normalized = str(rel_path).strip().replace("\\", "/")
    if normalized == _LONG_TERM_REL_PATH:
        return _validate_long_term_file(target)
    return None


def _validate_long_term_file(target: Path) -> ApplyOutcome | None:
    if not target.exists() or not target.is_file():
        return None

    content = target.read_text(encoding="utf-8")
    for header in _LONG_TERM_REQUIRED_SECTIONS:
        if header not in content:
            return ApplyOutcome(
                status="error",
                code="long_term_structure_invalid",
                detail=f"missing required section: {header}",
            )

    header_positions = [
        content.index(header) for header in _LONG_TERM_REQUIRED_SECTIONS
    ]
    if header_positions != sorted(header_positions):
        return ApplyOutcome(
            status="error",
            code="long_term_structure_invalid",
            detail="required sections are out of order",
        )

    current_section: str | None = None
    core_value_count = 0
    for lineno, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if stripped in _LONG_TERM_REQUIRED_SECTIONS:
            current_section = stripped
            continue
        if stripped.startswith("## "):
            current_section = None
            continue
        if current_section is None:
            continue
        if (
            not stripped
            or stripped.startswith("<!--")
            or stripped.startswith("### ")
            or line.startswith("  ")
            or line.startswith("\t")
        ):
            continue
        if current_section == "## 核心價值":
            if not _LONG_TERM_CORE_VALUE_LINE.match(stripped):
                return ApplyOutcome(
                    status="error",
                    code="long_term_structure_invalid",
                    detail=f"line {lineno} is not a valid 核心價值 item",
                )
            core_value_count += 1
        if current_section == "## 約定" and not _LONG_TERM_RULE_LINE.match(stripped):
            return ApplyOutcome(
                status="error",
                code="long_term_structure_invalid",
                detail=f"line {lineno} is not a valid 約定 item",
            )
        if current_section == "## 清單" and not _LONG_TERM_LIST_LINE.match(stripped):
            return ApplyOutcome(
                status="error",
                code="long_term_structure_invalid",
                detail=f"line {lineno} is not a valid 清單 item",
            )
        if current_section == "## 重要記錄" and not _LONG_TERM_RECORD_LINE.match(
            stripped
        ):
            return ApplyOutcome(
                status="error",
                code="long_term_structure_invalid",
                detail=f"line {lineno} is not a valid 重要記錄 item",
            )

    if core_value_count > _LONG_TERM_CORE_VALUE_MAX:
        return ApplyOutcome(
            status="error",
            code="long_term_structure_invalid",
            detail=f"核心價值 has {core_value_count} items (max {_LONG_TERM_CORE_VALUE_MAX})",
        )

    return None


# -- Index auto-maintenance ----------------------------------------------------


def _auto_maintain_index(
    *,
    target: Path,
    plan: MemoryEditPlan,
    base_dir: Path,
) -> None:
    """Auto-add/remove index links after create/delete operations."""
    if target.name == "index.md":
        return

    parent_index = target.parent / "index.md"
    parent_rel = _to_memory_rel_path(parent_index, base_dir=base_dir)
    if (
        parent_rel is not None
        and classify_memory_index_path(parent_rel) == IndexKind.REGISTRY
    ):
        # Registry indexes are domain-owned (e.g. people/index.md table).
        return

    for op in plan.operations:
        if op.kind == "create_if_missing":
            # Use planner-provided description; fall back to filename only
            title = (
                f"{target.name} — {plan.index_description}"
                if plan.index_description
                else target.name
            )
            _ensure_index_link(
                parent_index,
                link_path=target.name,
                link_title=title,
                base_dir=base_dir,
            )

            # Upward propagation: ensure new directories are linked
            # in ancestor indexes
            _propagate_new_directory_upward(target.parent, base_dir)

        elif op.kind == "delete_file":
            # Remove link from parent index
            remove_index_link(parent_index, target.name)

            # Check if directory is now empty (only index.md remains)
            _cleanup_empty_directory(target.parent)


def _propagate_new_directory_upward(directory: Path, base_dir: Path) -> None:
    """Ensure each ancestor index links to newly created child directories.

    Walks from *directory* up toward *base_dir*.  For each level, if the
    parent has (or should have) an ``index.md``, ensure it contains a link
    to the child directory.  Stops at *base_dir* or when the parent index
    already references the child (idempotent via ``_ensure_index_link``).
    """
    current = directory.resolve()
    root = base_dir.resolve()

    while current != root and current.parent != current:
        parent = current.parent
        if parent.resolve() == root.parent.resolve():
            break

        parent_index = parent / "index.md"
        parent_rel = _to_memory_rel_path(parent_index, base_dir=base_dir)
        if (
            parent_rel is not None
            and classify_memory_index_path(parent_rel) == IndexKind.REGISTRY
        ):
            break

        dir_name = current.name
        outcome = _ensure_index_link(
            parent_index,
            link_path=f"{dir_name}/",
            link_title=f"{dir_name}/",
            base_dir=base_dir,
        )
        # If the link already existed, ancestors are already correct
        if outcome.status == "noop":
            break

        current = parent


def _cleanup_empty_directory(directory: Path) -> None:
    """Remove an empty directory plus its index artifacts."""
    if not directory.is_dir():
        return

    remaining_entries = [f for f in directory.iterdir() if f.name != "index.md"]
    if remaining_entries:
        return

    index_file = directory / "index.md"
    if index_file.exists() and not delete_index_for_cleanup(index_file):
        return

    grandparent_index = directory.parent / "index.md"
    dir_name = directory.name
    remove_index_link(grandparent_index, f"{dir_name}/")
    remove_index_link(grandparent_index, dir_name)

    try:
        directory.rmdir()
    except OSError:
        logger.warning("Failed to remove empty directory: %s", directory)
        return

    logger.info("Removed empty directory: %s", directory)


# -- File health warnings ------------------------------------------------------


def _check_file_warnings(
    target: Path,
    rel_path: str,
    config: MemoryEditWarningsConfig,
    *,
    agent_os_dir: Path | None = None,
) -> list[WarningItem]:
    """Check file state and return non-blocking warnings."""
    if not target.is_file() or _matches_warning_patterns(
        target, rel_path, config.ignore
    ):
        return []

    content = target.read_text(encoding="utf-8")
    lines = content.splitlines()
    warnings: list[WarningItem] = []
    budget = _resolve_warning_budget(target, rel_path, config)
    chars = len(content)

    if chars > budget:
        if agent_os_dir is not None:
            _upsert_curation_queue(
                agent_os_dir=agent_os_dir,
                rel_path=rel_path,
                chars=chars,
                budget=budget,
            )
        warnings.append(
            WarningItem(
                path=rel_path,
                code="file_too_long",
                detail=(
                    f"{chars} chars (budget: {budget}); queued for curation. "
                    "see kernel/builtin-skills/memory-maintenance/"
                ),
            )
        )

    dupes = _find_duplicate_lines(lines)
    if dupes:
        near = ", ".join(str(n) for n in dupes[:3])
        warnings.append(
            WarningItem(
                path=rel_path,
                code="possible_duplicates",
                detail=(
                    f"similar lines near lines {near}, "
                    "see kernel/builtin-skills/memory-maintenance/"
                ),
            )
        )

    return warnings


def _matches_warning_patterns(
    target: Path,
    rel_path: str,
    patterns: list[str],
) -> bool:
    """Match warning patterns against workspace-relative paths."""
    for pattern in patterns:
        if pattern.endswith("/"):
            if f"/{pattern}" in f"/{rel_path}" or rel_path.startswith(pattern):
                return True
        elif target.name == pattern:
            return True
    return False


def _resolve_warning_budget(
    target: Path,
    rel_path: str,
    config: MemoryEditWarningsConfig,
) -> int:
    """Return the first matching budget override or the default budget."""
    for override in config.budgets:
        if _matches_warning_patterns(target, rel_path, [override.pattern]):
            return override.max_chars
    return config.max_chars


def _upsert_curation_queue(
    *,
    agent_os_dir: Path,
    rel_path: str,
    chars: int,
    budget: int,
) -> None:
    """Record an oversized file for the deterministic curation consumer."""
    queue_path = agent_os_dir / "state" / "memory-curation-queue.json"
    now = datetime.now(timezone.utc).isoformat()
    entries: list[dict[str, object]] = []
    try:
        data = json.loads(queue_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            entries = [entry for entry in data if isinstance(entry, dict)]
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to load memory curation queue: %s", queue_path)

    for entry in entries:
        if entry.get("path") != rel_path:
            continue
        entry["chars"] = chars
        entry["last_seen"] = now
        break
    else:
        entries.append(
            {
                "path": rel_path,
                "chars": chars,
                "budget": budget,
                "first_seen": now,
                "last_seen": now,
            }
        )

    temporary = queue_path.with_name(f".{queue_path.name}.tmp")
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        temporary.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(queue_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _find_duplicate_lines(lines: list[str]) -> list[int]:
    """Find line numbers with high token overlap with their neighbors."""
    duplicates: list[int] = []
    prev_tokens: set[str] | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("<!--"):
            prev_tokens = None
            continue

        tokens = set(stripped.split())
        if len(tokens) < 3:
            prev_tokens = tokens
            continue

        if prev_tokens and len(prev_tokens) >= 3:
            intersection = tokens & prev_tokens
            union = tokens | prev_tokens
            if union and len(intersection) / len(union) > _WARNING_DUPLICATE_THRESHOLD:
                duplicates.append(i + 1)  # 1-indexed

        prev_tokens = tokens

    return duplicates


# -- Helpers -------------------------------------------------------------------


def _operations_hash(operations: list[MemoryEditOperation]) -> str:
    """Build stable hash from planner-produced operations."""
    payload = json.dumps(
        [op.model_dump(mode="json", exclude_none=True) for op in operations],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _rollback_request_file(
    *,
    target: Path,
    existed: bool,
    original_content: str,
) -> None:
    """Rollback one request's target file to its original state."""
    if existed:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(original_content, encoding="utf-8")
        return

    if target.exists() and target.is_file():
        target.unlink()
