"""State file + recall sub-agent: temp-memory becomes a rewritten current-state file."""

from datetime import date, datetime
from pathlib import Path
import re
import shutil

import yaml

from .base import Migration

_FILES = [
    "agents/brain/prompts/system.md",
    "agents/memory_editor/prompts/system.md",
    "agents/recall/prompts/system.md",
    "builtin-skills/memory-maintenance/references/rules.md",
]

_STATE_TITLE = "# 今日現況"
_STATE_SECTIONS = (
    "## 最後互動",
    "## 今日時間線",
    "## 未閉環",
    "## 下一步",
)
_STATE_INTRO = (
    "這份檔案整份改寫，不是日誌。"
    "只寫此刻還有用的事；過去的細節"
    "交給每日快照（archive/temp-memory/）與對話紀錄"
    "（recall）。"
)
_UPGRADE_NOTE = (
    "- {today} 系統升級：舊的暫存記憶日誌已依"
    "日期移到 archive/temp-memory/；今天之前的脈絡"
    "先讀 {latest}；下一輪用戶訊息後，把本檔"
    "重寫成現在的狀態"
)
_NO_HISTORY_NOTE = (
    "- {today} 系統升級：本檔改為今日現況檔；"
    "下一輪用戶訊息後，把本檔重寫成"
    "現在的狀態"
)

_OLD_INDEX_LINE = (
    "- [temp-memory.md](temp-memory.md) — "
    "暫存工作記憶與近期上下文"
)
_NEW_INDEX_LINE = (
    "- [temp-memory.md](temp-memory.md) — "
    "今日現況檔（最後互動、時間線、"
    "未閉環、下一步）"
)

_DATE_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2})")
_DIGEST_RE = re.compile(r"^\s*- \[digest \d{4}-\d{2}-\d{2}\].*\n?", re.MULTILINE)

_RECALL_AGENT_CONFIG = {
    "enabled": True,
    "llm": "cfgs/llm/kano-proxy/recall.yaml",
    "llm_request_timeout": 30,
    "llm_transient_retries": 0,
    "warn_on_failure": True,
    "recall": {
        "prefetch": True,
        "prefetch_min_chars": 4,
        "prefetch_max_tool_calls": 6,
        "max_tool_calls": 12,
        "max_result_chars": 1500,
    },
}


class M0186StateFileAndRecall(Migration):
    """Convert temp-memory.md into the state file and deploy the recall sub-agent."""

    version = "0.86.0"
    summary = (
        "temp-memory.md 改為「今日現況檔」：標題加"
        "四段（最後互動、今日時間線、未閉環、"
        "下一步），整份改寫、上限 6000 字，不再"
        "追加日誌；舊日誌已依日期移到 "
        "archive/temp-memory/。"
        "每 9 回合強制 rollup 與每日 digest 移除；只在"
        "壓縮前、睡前與每日維護時補寫現況，"
        "維護時把現況檔快照到 archive/temp-memory/。"
        "新增 recall 子代理（Haiku + grep）：每則用戶"
        "訊息先從記憶檔與對話紀錄預抓相關"
        "事實附成 [Recall]，並提供 recall 工具。"
        "session 新增 transcript.jsonl（不被 compaction 改寫），"
        "requests/responses.jsonl 只保留 7 天。"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        for rel in _FILES:
            src = templates_dir / rel
            dst = kernel_dir / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

        workspace_dir = kernel_dir.parent
        memory_dir = workspace_dir / "memory"
        if memory_dir.is_dir():
            _convert_state_file(memory_dir)
            _update_index_line(memory_dir / "agent" / "index.md")

        for config_path in (
            workspace_dir / "agent.yaml",
            workspace_dir / "cfgs" / "agent.yaml",
        ):
            if config_path.exists():
                _update_config(config_path)


# -- state file ---------------------------------------------------------------

def _parse_by_date(content: str) -> dict[date, str]:
    """Group the legacy dated log into {date: text}; the preamble is dropped."""
    groups: dict[date, list[str]] = {}
    current: date | None = None
    for line in content.splitlines(keepends=True):
        match = _DATE_RE.search(line)
        if match:
            try:
                current = date.fromisoformat(match.group(1))
            except ValueError:
                pass
        if current is not None:
            groups.setdefault(current, []).append(line)
    return {day: "".join(lines) for day, lines in groups.items()}


def _convert_state_file(memory_dir: Path) -> None:
    state_path = memory_dir / "agent" / "temp-memory.md"
    if not state_path.is_file():
        return
    content = state_path.read_text(encoding="utf-8")
    if content.lstrip().startswith(_STATE_TITLE):
        return

    # Digest lines summarise days whose full text is already archived.
    dated = _parse_by_date(_DIGEST_RE.sub("", content))
    archive_dir = memory_dir / "archive" / "temp-memory"
    if dated:
        archive_dir.mkdir(parents=True, exist_ok=True)
        for day in sorted(dated):
            text = dated[day].rstrip("\n")
            target = archive_dir / f"{day.isoformat()}.md"
            if target.is_file():
                existing = target.read_text(encoding="utf-8")
                if text in existing:
                    continue
                target.write_text(
                    f"{existing.rstrip(chr(10))}\n\n{text}\n", encoding="utf-8",
                )
            else:
                target.write_text(f"# {day.isoformat()}\n\n{text}\n", encoding="utf-8")
        _rebuild_archive_index(archive_dir)

    today = datetime.now().date().isoformat()
    if dated:
        latest = max(dated).isoformat()
        note = _UPGRADE_NOTE.format(
            today=today, latest=f"archive/temp-memory/{latest}.md",
        )
    else:
        note = _NO_HISTORY_NOTE.format(today=today)
    skeleton = (
        f"{_STATE_TITLE}\n\n{_STATE_INTRO}\n\n"
        f"{_STATE_SECTIONS[0]}\n\n{note}\n\n"
        f"{_STATE_SECTIONS[1]}\n\n"
        f"{_STATE_SECTIONS[2]}\n\n"
        f"{_STATE_SECTIONS[3]}\n"
    )
    state_path.write_text(skeleton, encoding="utf-8")


def _rebuild_archive_index(archive_dir: Path) -> None:
    md_files = sorted(
        f for f in archive_dir.iterdir() if f.suffix == ".md" and f.name != "index.md"
    )
    lines = [f"# {archive_dir.name} archive\n", "\n"]
    lines.extend(f"- [{f.stem}]({f.name})\n" for f in md_files)
    (archive_dir / "index.md").write_text("".join(lines), encoding="utf-8")


def _update_index_line(index_path: Path) -> None:
    """Swap only the temp-memory description; the live index has custom rows."""
    if not index_path.is_file():
        return
    content = index_path.read_text(encoding="utf-8")
    if _OLD_INDEX_LINE not in content:
        return
    index_path.write_text(
        content.replace(_OLD_INDEX_LINE, _NEW_INDEX_LINE, 1), encoding="utf-8",
    )


# -- config --------------------------------------------------------------------

def _update_config(config_path: Path) -> None:
    with config_path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config, dict):
        return
    changed = False

    tools = config.get("tools")
    if isinstance(tools, dict):
        memory_sync = tools.get("memory_sync")
        if isinstance(memory_sync, dict) and "every_n_turns" in memory_sync:
            memory_sync.pop("every_n_turns")
            changed = True
        memory_edit = tools.get("memory_edit")
        if isinstance(memory_edit, dict) and "temp_memory_max_chars" not in memory_edit:
            memory_edit["temp_memory_max_chars"] = 6000
            changed = True
        bm25 = (tools.get("memory_search") or {}).get("bm25") if isinstance(tools.get("memory_search"), dict) else None
        if isinstance(bm25, dict) and isinstance(bm25.get("exclude"), list):
            if "memory/archive/curation/" not in bm25["exclude"]:
                bm25["exclude"].append("memory/archive/curation/")
                changed = True

    maintenance = config.get("maintenance")
    if isinstance(maintenance, dict):
        if "archive" in maintenance:
            maintenance.pop("archive")
            changed = True
        curate = maintenance.get("curate")
        if isinstance(curate, dict):
            for key in ("digest_retain_days", "digest_max_chars"):
                if key in curate:
                    curate.pop(key)
                    changed = True
        cleanup = maintenance.get("session_file_cleanup")
        if isinstance(cleanup, dict) and "debug_retention_days" not in cleanup:
            cleanup["debug_retention_days"] = 7
            changed = True

    agents = config.get("agents")
    if isinstance(agents, dict) and "recall" not in agents:
        agents["recall"] = dict(_RECALL_AGENT_CONFIG)
        changed = True

    if changed:
        with config_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
