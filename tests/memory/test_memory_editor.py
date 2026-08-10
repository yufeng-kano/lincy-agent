"""Tests for memory editor v2 (instruction -> planned operations)."""

from __future__ import annotations

from pathlib import Path
from threading import Barrier, BrokenBarrierError

from lincy.memory.editor.apply import apply_operation
from lincy.memory.editor.schema import (
    MemoryEditBatch,
    MemoryEditOperation,
    MemoryEditPlan,
    MemoryEditRequest,
)
from lincy.memory.editor.service import MemoryEditor
from lincy.memory.editor.session_log import SessionCommitLog
from lincy.workspace.people import load_people_index, save_people_index, PersonEntry


def _allowed(base_dir: Path) -> list[str]:
    return [str(base_dir)]


class _StaticPlanner:
    """Planner stub returning predefined plans by request_id."""

    def __init__(self, plans: dict[str, MemoryEditPlan]):
        self._plans = plans

    def plan(  # noqa: ANN001,ARG002
        self,
        *,
        request,
        as_of,
        turn_id,
        file_exists,
        file_content,
        file_content_available=True,
    ):
        return self._plans[request.request_id]


class _BarrierPlanner:
    """Planner stub that requires concurrent calls to proceed."""

    def __init__(self, plans: dict[str, MemoryEditPlan], parties: int):
        self._plans = plans
        self._barrier = Barrier(parties)

    def plan(  # noqa: ANN001,ARG002
        self,
        *,
        request,
        as_of,
        turn_id,
        file_exists,
        file_content,
        file_content_available=True,
    ):
        try:
            self._barrier.wait(timeout=1.0)
        except BrokenBarrierError as e:
            raise AssertionError(
                "expected planner calls to run in parallel across target files"
            ) from e
        return self._plans[request.request_id]


class _SameFileOrderPlanner:
    """Planner stub asserting same-file requests observe sequential state."""

    def plan(  # noqa: ANN001,ARG002
        self,
        *,
        request,
        as_of,
        turn_id,
        file_exists,
        file_content,
        file_content_available=True,
    ):
        if request.request_id == "r1":
            assert file_exists is False
            return MemoryEditPlan(
                status="ok",
                operations=[MemoryEditOperation(kind="create_if_missing", payload_text="# notes")],
            )
        if request.request_id == "r2":
            assert file_exists is True
            assert "# notes" in file_content
            return MemoryEditPlan(
                status="ok",
                operations=[MemoryEditOperation(kind="append_entry", payload_text="- second")],
            )
        raise AssertionError(f"unexpected request_id: {request.request_id}")


class _RecordingPlanner:
    """Planner stub that records target-file context."""

    def __init__(self, plans: dict[str, MemoryEditPlan]):
        self._plans = plans
        self.calls: list[dict[str, object]] = []

    def plan(  # noqa: ANN001,ARG002
        self,
        *,
        request,
        as_of,
        turn_id,
        file_exists,
        file_content,
        file_content_available=True,
    ):
        self.calls.append(
            {
                "request_id": request.request_id,
                "file_exists": file_exists,
                "file_content": file_content,
                "file_content_available": file_content_available,
            }
        )
        return self._plans[request.request_id]


def test_apply_delete_file_removes_existing(tmp_path: Path):
    target = tmp_path / "memory" / "agent" / "knowledge" / "old-topic.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Old Topic\n", encoding="utf-8")

    operation = MemoryEditOperation(kind="delete_file")
    result = apply_operation(target, operation, base_dir=tmp_path)
    assert result.status == "applied"
    assert not target.exists()


def test_apply_delete_file_noop_when_missing(tmp_path: Path):
    target = tmp_path / "memory" / "agent" / "knowledge" / "nonexistent.md"
    target.parent.mkdir(parents=True, exist_ok=True)

    operation = MemoryEditOperation(kind="delete_file")
    result = apply_operation(target, operation, base_dir=tmp_path)
    assert result.status == "noop"


def test_apply_delete_file_rejects_index_md(tmp_path: Path):
    target = tmp_path / "memory" / "agent" / "knowledge" / "index.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Index\n", encoding="utf-8")

    operation = MemoryEditOperation(kind="delete_file")
    result = apply_operation(target, operation, base_dir=tmp_path)
    assert result.status == "error"
    assert result.code == "delete_index_forbidden"
    assert target.exists()


def test_apply_delete_file_rejects_directory(tmp_path: Path):
    target = tmp_path / "memory" / "agent" / "knowledge"
    target.mkdir(parents=True, exist_ok=True)

    operation = MemoryEditOperation(kind="delete_file")
    result = apply_operation(target, operation, base_dir=tmp_path)
    assert result.status == "error"
    assert result.code == "not_a_file"


def test_apply_toggle_checkbox_apply_all_matches(tmp_path: Path):
    target = tmp_path / "memory" / "agent" / "pending-thoughts.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("- [ ] task one\n- [ ] task one\n", encoding="utf-8")

    operation = MemoryEditOperation(
        kind="toggle_checkbox",
        item_text="task one",
        checked=True,
        apply_all_matches=True,
    )
    result = apply_operation(target, operation, base_dir=tmp_path)
    assert result.status == "applied"
    content = target.read_text(encoding="utf-8")
    assert content.count("- [x] task one") == 2


def test_apply_prune_checked_checkboxes(tmp_path: Path):
    target = tmp_path / "memory" / "agent" / "pending-thoughts.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "- [x] done one\n- [ ] todo one\n- [X] done two\n",
        encoding="utf-8",
    )

    operation = MemoryEditOperation(kind="prune_checked_checkboxes")
    result = apply_operation(target, operation, base_dir=tmp_path)
    assert result.status == "applied"
    content = target.read_text(encoding="utf-8")
    assert "- [x] done one" not in content
    assert "- [X] done two" not in content
    assert "- [ ] todo one" in content


def test_apply_toggle_checkbox_rest_reminder_regression(tmp_path: Path):
    target = tmp_path / "memory" / "agent" / "pending-thoughts.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# 2026-02-10 待分享念頭\n\n"
        "## 生活關懷\n"
        "- [ ] **休息提醒**: 如果昨晚太晚睡，提醒他今天下午找時間補眠。\n",
        encoding="utf-8",
    )

    operation = MemoryEditOperation(
        kind="toggle_checkbox",
        item_text="**休息提醒**: 如果昨晚太晚睡，提醒他今天下午找時間補眠。",
        checked=True,
        apply_all_matches=True,
    )
    result = apply_operation(target, operation, base_dir=tmp_path)
    assert result.status == "applied"
    content = target.read_text(encoding="utf-8")
    assert "- [x] **休息提醒**: 如果昨晚太晚睡，提醒他今天下午找時間補眠。" in content


def test_memory_editor_applies_instruction_plan(tmp_path: Path):
    target = tmp_path / "memory" / "agent" / "recent.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# short-term\n", encoding="utf-8")

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/recent.md",
        instruction="追加今天摘要",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[
            MemoryEditOperation(
                kind="append_entry",
                payload_text="- [2026-02-11 00:46] append",
            )
        ],
    )
    batch = MemoryEditBatch(
        as_of="2026-02-11T00:46:32+08:00",
        turn_id="turn-1",
        requests=[request],
    )

    editor = MemoryEditor(
        commit_log=SessionCommitLog(),
        planner=_StaticPlanner({"r1": plan}),
    )
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "ok"
    assert result.applied[0].status == "applied"
    assert "- [2026-02-11 00:46] append" in target.read_text(encoding="utf-8")


def test_temp_memory_append_does_not_read_existing_file(tmp_path: Path, monkeypatch):
    target = tmp_path / "memory" / "agent" / "temp-memory.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("- [2026-05-17 10:00] 毓峰 existing entry。\n", encoding="utf-8")

    original_read_text = Path.read_text

    def guarded_read_text(self, *args, **kwargs):  # noqa: ANN001
        if self == target:
            raise AssertionError("temp-memory.md must not be read for append")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/temp-memory.md",
        instruction="追加條目：毓峰要求 temp-memory append-only。",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[
            MemoryEditOperation(
                kind="append_entry",
                payload_text=(
                    "- [2026-05-17 20:05] 毓峰要求 temp-memory 寫入不要讀全文。"
                ),
            )
        ],
    )
    batch = MemoryEditBatch(
        as_of="2026-05-17T20:05:00+08:00",
        turn_id="turn-1",
        requests=[request],
    )
    planner = _RecordingPlanner({"r1": plan})

    editor = MemoryEditor(commit_log=SessionCommitLog(), planner=planner)
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "ok"
    assert result.applied[0].status == "applied"
    assert planner.calls == [
        {
            "request_id": "r1",
            "file_exists": True,
            "file_content": "",
            "file_content_available": False,
        }
    ]
    with target.open(encoding="utf-8") as f:
        content = f.read()
    assert content == (
        "- [2026-05-17 10:00] 毓峰 existing entry。\n"
        "- [2026-05-17 20:05] 毓峰要求 temp-memory 寫入不要讀全文。\n"
    )


def test_temp_memory_rejects_non_append_plan_without_reading_file(tmp_path: Path, monkeypatch):
    target = tmp_path / "memory" / "agent" / "temp-memory.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    original = "- [2026-05-17 10:00] 毓峰 existing entry。\n"
    target.write_text(original, encoding="utf-8")

    original_read_text = Path.read_text

    def guarded_read_text(self, *args, **kwargs):  # noqa: ANN001
        if self == target:
            raise AssertionError("temp-memory.md must not be read for append-only rejection")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/temp-memory.md",
        instruction="修改舊條目",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[
            MemoryEditOperation(
                kind="replace_block",
                old_block="existing",
                new_block="changed",
            )
        ],
    )
    batch = MemoryEditBatch(
        as_of="2026-05-17T20:05:00+08:00",
        turn_id="turn-1",
        requests=[request],
    )

    editor = MemoryEditor(
        commit_log=SessionCommitLog(),
        planner=_RecordingPlanner({"r1": plan}),
    )
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "failed"
    assert result.errors[0].code == "temp_memory_append_only"
    with target.open(encoding="utf-8") as f:
        assert f.read() == original


def test_memory_editor_idempotent_replay_with_same_planned_ops(tmp_path: Path):
    target = tmp_path / "memory" / "agent" / "recent.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# short-term\n", encoding="utf-8")

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/recent.md",
        instruction="追加今天摘要",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[
            MemoryEditOperation(
                kind="append_entry",
                payload_text="- [2026-02-11 00:46] append",
            )
        ],
    )
    batch = MemoryEditBatch(
        as_of="2026-02-11T00:46:32+08:00",
        turn_id="turn-1",
        requests=[request],
    )

    editor = MemoryEditor(
        commit_log=SessionCommitLog(),
        planner=_StaticPlanner({"r1": plan}),
    )
    first = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)
    second = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert first.status == "ok"
    assert first.applied[0].status == "applied"
    assert second.status == "ok"
    assert second.applied[0].status == "already_applied"


def test_memory_editor_rolls_back_request_on_operation_failure(tmp_path: Path):
    target = tmp_path / "memory" / "agent" / "recent.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# short-term\n", encoding="utf-8")

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/recent.md",
        instruction="先加一行再做錯誤替換",
    )
    failing_plan = MemoryEditPlan(
        status="ok",
        operations=[
            MemoryEditOperation(
                kind="append_entry",
                payload_text="- [2026-02-11 00:46] temp",
            ),
            MemoryEditOperation(
                kind="replace_block",
                old_block="does-not-exist",
                new_block="replacement",
            ),
        ],
    )
    batch = MemoryEditBatch(
        as_of="2026-02-11T00:46:32+08:00",
        turn_id="turn-1",
        requests=[request],
    )

    editor = MemoryEditor(
        commit_log=SessionCommitLog(),
        planner=_StaticPlanner({"r1": failing_plan}),
    )
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "failed"
    assert result.errors[0].code == "block_not_found"
    # request-level atomicity: appended temp line must be rolled back
    assert target.read_text(encoding="utf-8") == "# short-term\n"


def test_memory_editor_returns_instruction_not_actionable_error(tmp_path: Path):
    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/pending-thoughts.md",
        instruction="這句話沒有可執行的編輯語意",
    )
    plan = MemoryEditPlan(
        status="error",
        error_code="instruction_not_actionable",
        error_detail="planner cannot map instruction to operations",
    )
    batch = MemoryEditBatch(
        as_of="2026-02-11T00:46:32+08:00",
        turn_id="turn-1",
        requests=[request],
    )

    editor = MemoryEditor(
        commit_log=SessionCommitLog(),
        planner=_StaticPlanner({"r1": plan}),
    )
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "failed"
    assert result.errors[0].code == "instruction_not_actionable"


def test_memory_editor_parallelizes_different_target_files(tmp_path: Path):
    req1 = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/a.md",
        instruction="create file a",
    )
    req2 = MemoryEditRequest(
        request_id="r2",
        target_path="memory/agent/b.md",
        instruction="create file b",
    )
    plans = {
        "r1": MemoryEditPlan(
            status="ok",
            operations=[MemoryEditOperation(kind="create_if_missing", payload_text="# a")],
        ),
        "r2": MemoryEditPlan(
            status="ok",
            operations=[MemoryEditOperation(kind="create_if_missing", payload_text="# b")],
        ),
    }
    batch = MemoryEditBatch(
        as_of="2026-02-11T00:46:32+08:00",
        turn_id="turn-1",
        requests=[req1, req2],
    )

    editor = MemoryEditor(
        commit_log=SessionCommitLog(),
        planner=_BarrierPlanner(plans, parties=2),
    )
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "ok"
    assert [item.request_id for item in result.applied] == ["r1", "r2"]
    assert (tmp_path / "memory" / "agent" / "a.md").read_text(encoding="utf-8") == "# a"
    assert (tmp_path / "memory" / "agent" / "b.md").read_text(encoding="utf-8") == "# b"


def test_memory_editor_same_file_requests_stay_sequential(tmp_path: Path):
    req1 = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/notes.md",
        instruction="create notes",
    )
    req2 = MemoryEditRequest(
        request_id="r2",
        target_path="memory/agent/notes.md",
        instruction="append notes",
    )
    batch = MemoryEditBatch(
        as_of="2026-02-11T00:46:32+08:00",
        turn_id="turn-1",
        requests=[req1, req2],
    )

    editor = MemoryEditor(
        commit_log=SessionCommitLog(),
        planner=_SameFileOrderPlanner(),
    )
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "ok"
    assert [item.request_id for item in result.applied] == ["r1", "r2"]
    content = (tmp_path / "memory" / "agent" / "notes.md").read_text(encoding="utf-8")
    assert "# notes" in content
    assert "- second" in content


def test_memory_editor_rejects_appending_checkbox_rule_to_long_term_tail(tmp_path: Path):
    target = tmp_path / "memory" / "agent" / "long-term.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    original = (
        "# 長期重要事項\n\n"
        "## 核心價值\n\n"
        "- 主動想著老公這個人\n\n"
        "## 約定\n\n"
        "- [ ] [2026-03-01] 毓峰: 既有規則。\n\n"
        "## 清單\n\n"
        "- [2026-03-01] 既有清單項目。\n\n"
        "## 重要記錄\n\n"
        "- [2026-03-01] 既有背景記錄。\n"
    )
    target.write_text(original, encoding="utf-8")

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/long-term.md",
        instruction="add a new active rule",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[
            MemoryEditOperation(
                kind="append_entry",
                payload_text="- [ ] [2026-03-21] 毓峰: 新規則不能直接加在檔尾。\n",
            )
        ],
    )
    batch = MemoryEditBatch(
        as_of="2026-03-21T12:00:00+08:00",
        turn_id="turn-1",
        requests=[request],
    )

    editor = MemoryEditor(
        commit_log=SessionCommitLog(),
        planner=_StaticPlanner({"r1": plan}),
    )
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "failed"
    assert result.errors[0].code == "long_term_structure_invalid"
    assert target.read_text(encoding="utf-8") == original


def test_memory_editor_allows_replace_block_to_insert_long_term_agreement(tmp_path: Path):
    target = tmp_path / "memory" / "agent" / "long-term.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    original = (
        "# 長期重要事項\n\n"
        "## 核心價值\n\n"
        "- 主動想著老公這個人\n\n"
        "## 約定\n\n"
        "- [ ] [2026-03-01] 毓峰: 既有規則。\n\n"
        "## 清單\n\n"
        "- [2026-03-01] 既有清單項目。\n\n"
        "## 重要記錄\n\n"
        "- [2026-03-01] 既有背景記錄。\n"
    )
    target.write_text(original, encoding="utf-8")

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/long-term.md",
        instruction="insert a new agreement into 約定",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[
            MemoryEditOperation(
                kind="replace_block",
                old_block="- [ ] [2026-03-01] 毓峰: 既有規則。\n",
                new_block=(
                    "- [ ] [2026-03-01] 毓峰: 既有規則。\n"
                    "- [ ] [2026-03-21] 毓峰: 新規則要插入正確 section。\n"
                ),
            )
        ],
    )
    batch = MemoryEditBatch(
        as_of="2026-03-21T12:00:00+08:00",
        turn_id="turn-1",
        requests=[request],
    )

    editor = MemoryEditor(
        commit_log=SessionCommitLog(),
        planner=_StaticPlanner({"r1": plan}),
    )
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "ok"
    assert result.applied[0].status == "applied"
    content = target.read_text(encoding="utf-8")
    assert "- [ ] [2026-03-21] 毓峰: 新規則要插入正確 section。" in content


def test_memory_editor_allows_replace_block_to_insert_long_term_list_item(tmp_path: Path):
    target = tmp_path / "memory" / "agent" / "long-term.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    original = (
        "# 長期重要事項\n\n"
        "## 核心價值\n\n"
        "- 主動想著老公這個人\n\n"
        "## 約定\n\n"
        "- [ ] [2026-03-01] 毓峰: 既有規則。\n\n"
        "## 清單\n\n"
        "- [2026-03-01] 既有清單項目。\n\n"
        "## 重要記錄\n\n"
        "- [2026-03-01] 既有背景記錄。\n"
    )
    target.write_text(original, encoding="utf-8")

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/long-term.md",
        instruction="insert a new long-term list item",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[
            MemoryEditOperation(
                kind="replace_block",
                old_block="- [2026-03-01] 既有清單項目。\n",
                new_block=(
                    "- [2026-03-01] 既有清單項目。\n"
                    "- [2026-03-21] 新的清單項目要進清單 section。\n"
                ),
            )
        ],
    )
    batch = MemoryEditBatch(
        as_of="2026-03-21T12:00:00+08:00",
        turn_id="turn-1",
        requests=[request],
    )

    editor = MemoryEditor(
        commit_log=SessionCommitLog(),
        planner=_StaticPlanner({"r1": plan}),
    )
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "ok"
    assert result.applied[0].status == "applied"
    content = target.read_text(encoding="utf-8")
    assert "- [2026-03-21] 新的清單項目要進清單 section。" in content


def test_memory_editor_delete_file_via_service(tmp_path: Path):
    target = tmp_path / "memory" / "agent" / "knowledge" / "old.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# old\n", encoding="utf-8")

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/knowledge/old.md",
        instruction="delete this file",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[MemoryEditOperation(kind="delete_file")],
    )
    batch = MemoryEditBatch(
        as_of="2026-02-12T10:00:00+08:00",
        turn_id="turn-1",
        requests=[request],
    )

    editor = MemoryEditor(
        commit_log=SessionCommitLog(),
        planner=_StaticPlanner({"r1": plan}),
    )
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "ok"
    assert result.applied[0].status == "applied"
    assert not target.exists()


def test_memory_editor_delete_file_removes_now_empty_directory(tmp_path: Path):
    topic_dir = tmp_path / "memory" / "agent" / "knowledge" / "demo-topic"
    topic_dir.mkdir(parents=True, exist_ok=True)
    target = topic_dir / "guide.md"
    target.write_text("# demo\n", encoding="utf-8")
    (topic_dir / "index.md").write_text(
        "# demo-topic\n- [guide.md](guide.md)\n",
        encoding="utf-8",
    )
    parent_index = topic_dir.parent / "index.md"
    parent_index.write_text(
        "# Knowledge\n- [demo-topic/](demo-topic/) — demo\n",
        encoding="utf-8",
    )

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/knowledge/demo-topic/guide.md",
        instruction="delete this file",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[MemoryEditOperation(kind="delete_file")],
    )
    batch = MemoryEditBatch(
        as_of="2026-02-12T10:00:00+08:00",
        turn_id="turn-1",
        requests=[request],
    )

    editor = MemoryEditor(
        commit_log=SessionCommitLog(),
        planner=_StaticPlanner({"r1": plan}),
    )
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "ok"
    assert result.applied[0].status == "applied"
    assert not target.exists()
    assert not topic_dir.exists()
    assert "demo-topic/" not in parent_index.read_text(encoding="utf-8")


def test_memory_editor_delete_file_rollback(tmp_path: Path):
    """Delete followed by a failing operation should restore the deleted file."""
    target = tmp_path / "memory" / "agent" / "knowledge" / "precious.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    original = "# precious data\n"
    target.write_text(original, encoding="utf-8")

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/knowledge/precious.md",
        instruction="delete then fail",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[
            MemoryEditOperation(kind="delete_file"),
            MemoryEditOperation(
                kind="replace_block",
                old_block="impossible",
                new_block="replacement",
            ),
        ],
    )
    batch = MemoryEditBatch(
        as_of="2026-02-12T10:00:00+08:00",
        turn_id="turn-1",
        requests=[request],
    )

    editor = MemoryEditor(
        commit_log=SessionCommitLog(),
        planner=_StaticPlanner({"r1": plan}),
    )
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "failed"
    # Rollback should restore the file with original content.
    assert target.exists()
    assert target.read_text(encoding="utf-8") == original


def test_memory_editor_delete_file_idempotent_replay(tmp_path: Path):
    """Second apply_batch for delete_file returns already_applied."""
    target = tmp_path / "memory" / "agent" / "knowledge" / "temp.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# temp\n", encoding="utf-8")

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/knowledge/temp.md",
        instruction="delete temp file",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[MemoryEditOperation(kind="delete_file")],
    )
    batch = MemoryEditBatch(
        as_of="2026-02-12T10:00:00+08:00",
        turn_id="turn-1",
        requests=[request],
    )

    editor = MemoryEditor(
        commit_log=SessionCommitLog(),
        planner=_StaticPlanner({"r1": plan}),
    )
    first = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)
    second = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert first.status == "ok"
    assert first.applied[0].status == "applied"
    assert second.status == "ok"
    assert second.applied[0].status == "already_applied"


# --- overwrite tests ---


def test_apply_overwrite_creates_new_file(tmp_path: Path):
    target = tmp_path / "memory" / "agent" / "knowledge" / "new-topic.md"
    target.parent.mkdir(parents=True, exist_ok=True)

    operation = MemoryEditOperation(kind="overwrite", payload_text="# New Topic\n")
    result = apply_operation(target, operation, base_dir=tmp_path)
    assert result.status == "applied"
    assert target.read_text(encoding="utf-8") == "# New Topic\n"


def test_apply_overwrite_replaces_existing(tmp_path: Path):
    target = tmp_path / "memory" / "agent" / "knowledge" / "topic.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Old Content\nold line\n", encoding="utf-8")

    operation = MemoryEditOperation(kind="overwrite", payload_text="# New Content\nnew line\n")
    result = apply_operation(target, operation, base_dir=tmp_path)
    assert result.status == "applied"
    assert target.read_text(encoding="utf-8") == "# New Content\nnew line\n"


def test_apply_overwrite_noop_identical(tmp_path: Path):
    target = tmp_path / "memory" / "agent" / "knowledge" / "topic.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    content = "# Same Content\n"
    target.write_text(content, encoding="utf-8")

    operation = MemoryEditOperation(kind="overwrite", payload_text=content)
    result = apply_operation(target, operation, base_dir=tmp_path)
    assert result.status == "noop"


def test_apply_overwrite_empty_file(tmp_path: Path):
    target = tmp_path / "memory" / "agent" / "knowledge" / "empty.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("", encoding="utf-8")

    operation = MemoryEditOperation(kind="overwrite", payload_text="# Filled\n")
    result = apply_operation(target, operation, base_dir=tmp_path)
    assert result.status == "applied"
    assert target.read_text(encoding="utf-8") == "# Filled\n"


def test_memory_editor_overwrite_via_service(tmp_path: Path):
    target = tmp_path / "memory" / "agent" / "knowledge" / "topic.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Old\n", encoding="utf-8")

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/knowledge/topic.md",
        instruction="overwrite entire file",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[MemoryEditOperation(kind="overwrite", payload_text="# Replaced\nnew content\n")],
    )
    batch = MemoryEditBatch(
        as_of="2026-02-14T12:00:00+08:00",
        turn_id="turn-1",
        requests=[request],
    )

    editor = MemoryEditor(
        commit_log=SessionCommitLog(),
        planner=_StaticPlanner({"r1": plan}),
    )
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "ok"
    assert result.applied[0].status == "applied"
    assert target.read_text(encoding="utf-8") == "# Replaced\nnew content\n"


# --- index auto-maintenance tests ---


def test_create_auto_adds_index_link(tmp_path: Path):
    """Creating a file should auto-add a link to parent index.md."""
    parent = tmp_path / "memory" / "agent" / "knowledge"
    parent.mkdir(parents=True)
    (parent / "index.md").write_text("# Knowledge\n\n", encoding="utf-8")

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/knowledge/new-topic.md",
        instruction="Build new topic file about cooking",
    )
    plan = MemoryEditPlan(
        status="ok",
        index_description="Cooking recipes and tips",
        operations=[MemoryEditOperation(kind="create_if_missing", payload_text="# Cooking\n")],
    )
    batch = MemoryEditBatch(as_of="2026-02-22T12:00:00+08:00", turn_id="t1", requests=[request])
    editor = MemoryEditor(commit_log=SessionCommitLog(), planner=_StaticPlanner({"r1": plan}))
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "ok"
    index_content = (parent / "index.md").read_text(encoding="utf-8")
    assert "new-topic.md" in index_content
    assert "Cooking recipes and tips" in index_content


def test_delete_auto_removes_index_link(tmp_path: Path):
    """Deleting a file should auto-remove its link from parent index.md."""
    parent = tmp_path / "memory" / "agent" / "knowledge"
    parent.mkdir(parents=True)
    # Use the normalized path format that _ensure_index_link produces
    (parent / "index.md").write_text(
        "# Knowledge\n\n"
        "- [old.md](memory/agent/knowledge/old.md) \u2014 old topic\n"
        "- [keep.md](memory/agent/knowledge/keep.md) \u2014 keep this\n",
        encoding="utf-8",
    )
    (parent / "old.md").write_text("# Old\n", encoding="utf-8")
    (parent / "keep.md").write_text("# Keep\n", encoding="utf-8")

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/knowledge/old.md",
        instruction="delete this file",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[MemoryEditOperation(kind="delete_file")],
    )
    batch = MemoryEditBatch(as_of="2026-02-22T12:00:00+08:00", turn_id="t1", requests=[request])
    editor = MemoryEditor(commit_log=SessionCommitLog(), planner=_StaticPlanner({"r1": plan}))
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "ok"
    index_content = (parent / "index.md").read_text(encoding="utf-8")
    assert "(old.md)" not in index_content


def test_delete_last_file_cleans_directory(tmp_path: Path):
    """Deleting the last non-index file should clean up the directory's index."""
    parent = tmp_path / "memory" / "people" / "someone"
    parent.mkdir(parents=True)
    (parent / "index.md").write_text("# someone\n\n- [info.md](info.md)\n", encoding="utf-8")
    (parent / "info.md").write_text("# Info\n", encoding="utf-8")

    grandparent_index = tmp_path / "memory" / "people" / "index.md"
    grandparent_index.write_text("# People\n\n- [someone/](someone/)\n", encoding="utf-8")

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/people/someone/info.md",
        instruction="delete this person",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[MemoryEditOperation(kind="delete_file")],
    )
    batch = MemoryEditBatch(as_of="2026-02-22T12:00:00+08:00", turn_id="t1", requests=[request])
    editor = MemoryEditor(commit_log=SessionCommitLog(), planner=_StaticPlanner({"r1": plan}))
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "ok"
    # Directory index should be cleaned up
    assert not (parent / "index.md").exists()
    # Grandparent index should no longer reference the directory
    gp_content = grandparent_index.read_text(encoding="utf-8")
    assert "someone" not in gp_content


def test_create_in_new_subdir_propagates_to_grandparent_index(tmp_path: Path):
    """Creating a file in a new subdirectory should add the subdir link
    to the grandparent index.md (upward propagation)."""
    knowledge_dir = tmp_path / "memory" / "agent" / "knowledge"
    knowledge_dir.mkdir(parents=True)
    (knowledge_dir / "index.md").write_text("# Knowledge\n\n", encoding="utf-8")

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/knowledge/new-topic/guide.md",
        instruction="Create guide for new topic",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[MemoryEditOperation(kind="create_if_missing", payload_text="# Guide\n")],
    )
    batch = MemoryEditBatch(as_of="2026-02-27T12:00:00+08:00", turn_id="t1", requests=[request])
    editor = MemoryEditor(commit_log=SessionCommitLog(), planner=_StaticPlanner({"r1": plan}))
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "ok"

    # Immediate parent index should have the file link
    child_index = knowledge_dir / "new-topic" / "index.md"
    assert child_index.exists()
    child_content = child_index.read_text(encoding="utf-8")
    assert "guide.md" in child_content

    # Grandparent index should have the directory link
    knowledge_content = (knowledge_dir / "index.md").read_text(encoding="utf-8")
    assert "new-topic/" in knowledge_content


def test_create_people_file_upserts_people_registry(tmp_path: Path):
    people_root = tmp_path / "memory" / "people"
    people_root.mkdir(parents=True)
    save_people_index(people_root / "index.md", entries=[], legacy=None)

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/people/alice/basic-info.md",
        instruction="create Alice basic info",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[MemoryEditOperation(kind="create_if_missing", payload_text="# Alice\n")],
    )
    batch = MemoryEditBatch(as_of="2026-02-24T12:00:00+08:00", turn_id="t1", requests=[request])
    editor = MemoryEditor(commit_log=SessionCommitLog(), planner=_StaticPlanner({"r1": plan}))
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "ok"
    entries, legacy = load_people_index(people_root / "index.md")
    assert legacy is None
    alice = next(e for e in entries if e.user_id == "alice")
    assert alice.display_name == "Alice"
    assert alice.last_seen == "2026-02-24"


def test_delete_last_people_file_removes_people_registry_row(tmp_path: Path):
    user_dir = tmp_path / "memory" / "people" / "someone"
    user_dir.mkdir(parents=True)
    (user_dir / "index.md").write_text("# someone\n\n- [info.md](info.md)\n", encoding="utf-8")
    (user_dir / "info.md").write_text("# Info\n", encoding="utf-8")
    save_people_index(
        tmp_path / "memory" / "people" / "index.md",
        entries=[PersonEntry(user_id="someone", display_name="Someone", last_seen="2026-02-24")],
        legacy=None,
    )

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/people/someone/info.md",
        instruction="delete this person info",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[MemoryEditOperation(kind="delete_file")],
    )
    batch = MemoryEditBatch(as_of="2026-02-24T12:00:00+08:00", turn_id="t1", requests=[request])
    editor = MemoryEditor(commit_log=SessionCommitLog(), planner=_StaticPlanner({"r1": plan}))
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "ok"
    entries, _legacy = load_people_index(tmp_path / "memory" / "people" / "index.md")
    assert all(e.user_id != "someone" for e in entries)


# --- warnings tests ---


def test_warnings_file_too_long(tmp_path: Path):
    """Files exceeding max_lines threshold should trigger a warning."""
    target = tmp_path / "memory" / "agent" / "recent.md"
    target.parent.mkdir(parents=True)
    lines = ["- line %d\n" % i for i in range(80)]
    target.write_text("".join(lines), encoding="utf-8")

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/recent.md",
        instruction="append new entry",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[MemoryEditOperation(kind="append_entry", payload_text="- new line\n")],
    )
    batch = MemoryEditBatch(as_of="2026-02-22T12:00:00+08:00", turn_id="t1", requests=[request])
    editor = MemoryEditor(commit_log=SessionCommitLog(), planner=_StaticPlanner({"r1": plan}))
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "ok"
    assert any(w.code == "file_too_long" for w in result.warnings)


def test_warnings_possible_duplicates(tmp_path: Path):
    """Adjacent lines with high token overlap should trigger a warning."""
    target = tmp_path / "memory" / "agent" / "recent.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "# recent\n\n"
        "- [2026-02-22 10:00] some unique content here about the topic today\n"
        "- [2026-02-22 10:01] some unique content here about the topic today\n",
        encoding="utf-8",
    )

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/recent.md",
        instruction="append",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[MemoryEditOperation(kind="append_entry", payload_text="- new\n")],
    )
    batch = MemoryEditBatch(as_of="2026-02-22T12:00:00+08:00", turn_id="t1", requests=[request])
    editor = MemoryEditor(commit_log=SessionCommitLog(), planner=_StaticPlanner({"r1": plan}))
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert any(w.code == "possible_duplicates" for w in result.warnings)


def test_warnings_not_triggered_on_overwrite(tmp_path: Path):
    """Overwrite operations should not trigger file health warnings."""
    target = tmp_path / "memory" / "agent" / "recent.md"
    target.parent.mkdir(parents=True)
    lines = ["- line %d\n" % i for i in range(80)]
    target.write_text("".join(lines), encoding="utf-8")

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/recent.md",
        instruction="restructure",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[MemoryEditOperation(kind="overwrite", payload_text="# Cleaned\n- only one line\n")],
    )
    batch = MemoryEditBatch(as_of="2026-02-22T12:00:00+08:00", turn_id="t1", requests=[request])
    editor = MemoryEditor(commit_log=SessionCommitLog(), planner=_StaticPlanner({"r1": plan}))
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.warnings == []


def test_result_has_warnings_field(tmp_path: Path):
    """MemoryEditResult always has a warnings field (even if empty)."""
    target = tmp_path / "memory" / "agent" / "recent.md"
    target.parent.mkdir(parents=True)
    target.write_text("# short file\n", encoding="utf-8")

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/recent.md",
        instruction="append",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[MemoryEditOperation(kind="append_entry", payload_text="- entry\n")],
    )
    batch = MemoryEditBatch(as_of="2026-02-22T12:00:00+08:00", turn_id="t1", requests=[request])
    editor = MemoryEditor(commit_log=SessionCommitLog(), planner=_StaticPlanner({"r1": plan}))
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert isinstance(result.warnings, list)


# --- long-term 核心價值 structure guard tests ---


def _make_long_term_content(
    core_values: list[str] | None = None,
    rules: str = "- [ ] [2026-03-01] 毓峰: 既有規則。\n",
    lists: str = "- [2026-03-01] 既有清單項目。\n",
    records: str = "- [2026-03-01] 既有背景記錄。\n",
) -> str:
    cv = "\n".join(core_values) + "\n" if core_values else ""
    return (
        "# 長期重要事項\n\n"
        f"## 核心價值\n\n{cv}\n"
        f"## 約定\n\n{rules}\n"
        f"## 清單\n\n{lists}\n"
        f"## 重要記錄\n\n{records}"
    )


def test_long_term_core_values_accepted(tmp_path: Path):
    """Core values section with up to 5 free-text bullets is valid."""
    target = tmp_path / "memory" / "agent" / "long-term.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    original = _make_long_term_content(core_values=[
        "- 主動想著老公這個人",
        "- 回覆前先想他現在怎麼了",
    ])
    target.write_text(original, encoding="utf-8")

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/long-term.md",
        instruction="add a core value",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[
            MemoryEditOperation(
                kind="replace_block",
                old_block="- 回覆前先想他現在怎麼了\n",
                new_block=(
                    "- 回覆前先想他現在怎麼了\n"
                    "- 不確定的事不當事實講\n"
                ),
            )
        ],
    )
    batch = MemoryEditBatch(as_of="2026-03-27T12:00:00+08:00", turn_id="t1", requests=[request])
    editor = MemoryEditor(commit_log=SessionCommitLog(), planner=_StaticPlanner({"r1": plan}))
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "ok"
    content = target.read_text(encoding="utf-8")
    assert "- 不確定的事不當事實講" in content


def test_long_term_core_values_max_exceeded(tmp_path: Path):
    """More than 5 core value items should be rejected."""
    target = tmp_path / "memory" / "agent" / "long-term.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    original = _make_long_term_content(core_values=[
        f"- value {i}" for i in range(5)
    ])
    target.write_text(original, encoding="utf-8")

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/long-term.md",
        instruction="add sixth core value",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[
            MemoryEditOperation(
                kind="replace_block",
                old_block="- value 4\n",
                new_block="- value 4\n- value 5 (sixth item)\n",
            )
        ],
    )
    batch = MemoryEditBatch(as_of="2026-03-27T12:00:00+08:00", turn_id="t1", requests=[request])
    editor = MemoryEditor(commit_log=SessionCommitLog(), planner=_StaticPlanner({"r1": plan}))
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "failed"
    assert result.errors[0].code == "long_term_structure_invalid"
    assert "max 5" in result.errors[0].detail


def test_long_term_missing_core_values_section_rejected(tmp_path: Path):
    """Long-term file without 核心價值 section should be rejected."""
    target = tmp_path / "memory" / "agent" / "long-term.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Old format without 核心價值
    target.write_text(
        "# 長期重要事項\n\n"
        "## 約定\n\n"
        "- [ ] [2026-03-01] 毓峰: rule\n\n"
        "## 清單\n\n"
        "- [2026-03-01] list\n\n"
        "## 重要記錄\n\n"
        "- [2026-03-01] record\n",
        encoding="utf-8",
    )

    request = MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/long-term.md",
        instruction="add a record",
    )
    plan = MemoryEditPlan(
        status="ok",
        operations=[
            MemoryEditOperation(
                kind="append_entry",
                payload_text="- [2026-03-27] new record\n",
            )
        ],
    )
    batch = MemoryEditBatch(as_of="2026-03-27T12:00:00+08:00", turn_id="t1", requests=[request])
    editor = MemoryEditor(commit_log=SessionCommitLog(), planner=_StaticPlanner({"r1": plan}))
    result = editor.apply_batch(batch, allowed_paths=_allowed(tmp_path), base_dir=tmp_path)

    assert result.status == "failed"
    assert "核心價值" in result.errors[0].detail
