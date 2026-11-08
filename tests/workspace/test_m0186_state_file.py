"""Tests for m0186: temp-memory log -> state file, index line, config keys."""

from importlib import resources
from pathlib import Path

import yaml

from lincy.workspace.migrations.m0186_state_file_and_recall import M0186StateFileAndRecall

TEMPLATES = Path(str(resources.files("lincy.workspace"))) / "templates" / "kernel"

OLD_LOG = (
    "# 暫存記憶\n\n"
    "只放近期工作上下文。\n\n"
    "- [2026-11-05 00:20] 黃毓峰傳語音，說有點餓。\n"
    "  續行：澤希替他決定不點。\n"
    "- [2026-11-05 08:30] 叫醒，他說醒了。\n"
    "- [digest 2026-11-01] 摘要行（全文：memory/archive/temp-memory/2026-11-01.md）\n"
    "- [2026-11-06 12:00] 午餐吃麥當勞。\n"
)
OLD_INDEX = (
    "# Agent 記憶索引\n\n"
    "- [persona.md](persona.md) — 核心身份\n"
    "- [temp-memory.md](temp-memory.md) — 暫存工作記憶與近期上下文\n"
    "- [mine.md](mine.md) — 自由筆記\n"
)


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    kernel_dir = tmp_path / "kernel"
    kernel_dir.mkdir()
    agent_dir = tmp_path / "memory" / "agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "temp-memory.md").write_text(OLD_LOG, encoding="utf-8")
    (agent_dir / "index.md").write_text(OLD_INDEX, encoding="utf-8")
    return kernel_dir, tmp_path / "memory"


def test_converts_log_into_state_file_and_archives_by_date(tmp_path: Path):
    kernel_dir, memory_dir = _workspace(tmp_path)

    M0186StateFileAndRecall().upgrade(kernel_dir, TEMPLATES)

    state = (memory_dir / "agent" / "temp-memory.md").read_text(encoding="utf-8")
    assert state.startswith("# 今日現況\n")
    for heading in ("## 最後互動", "## 今日時間線", "## 未閉環", "## 下一步"):
        assert heading in state
    assert "archive/temp-memory/2026-11-06.md" in state
    assert "麥當勞" not in state
    assert "digest" not in state

    archive = memory_dir / "archive" / "temp-memory"
    day5 = (archive / "2026-11-05.md").read_text(encoding="utf-8")
    assert day5.startswith("# 2026-11-05\n\n- [2026-11-05 00:20]")
    assert "  續行：" in day5
    assert "08:30" in day5
    day6 = (archive / "2026-11-06.md").read_text(encoding="utf-8")
    assert "麥當勞" in day6
    assert not (archive / "2026-11-01.md").exists()
    index = (archive / "index.md").read_text(encoding="utf-8")
    assert "- [2026-11-05](2026-11-05.md)" in index
    assert "- [2026-11-06](2026-11-06.md)" in index

    assert (kernel_dir / "agents" / "recall" / "prompts" / "system.md").exists()
    assert (kernel_dir / "agents" / "brain" / "prompts" / "system.md").exists()


def test_appends_to_existing_archive_day_and_is_idempotent(tmp_path: Path):
    kernel_dir, memory_dir = _workspace(tmp_path)
    archive = memory_dir / "archive" / "temp-memory"
    archive.mkdir(parents=True)
    (archive / "2026-11-05.md").write_text("# 2026-11-05\n\n- [2026-11-05 00:01] earlier\n", encoding="utf-8")

    migration = M0186StateFileAndRecall()
    migration.upgrade(kernel_dir, TEMPLATES)
    day5 = (archive / "2026-11-05.md").read_text(encoding="utf-8")
    assert day5.startswith("# 2026-11-05\n\n- [2026-11-05 00:01] earlier\n\n- [2026-11-05 00:20]")

    state_after_first = (memory_dir / "agent" / "temp-memory.md").read_text(encoding="utf-8")
    migration.upgrade(kernel_dir, TEMPLATES)
    assert (memory_dir / "agent" / "temp-memory.md").read_text(encoding="utf-8") == state_after_first
    assert (archive / "2026-11-05.md").read_text(encoding="utf-8") == day5


def test_index_line_is_replaced_in_place_only(tmp_path: Path):
    kernel_dir, memory_dir = _workspace(tmp_path)

    M0186StateFileAndRecall().upgrade(kernel_dir, TEMPLATES)

    index = (memory_dir / "agent" / "index.md").read_text(encoding="utf-8")
    assert "- [temp-memory.md](temp-memory.md) — 今日現況檔（" in index
    assert "暫存工作記憶" not in index
    assert index.splitlines()[2] == "- [persona.md](persona.md) — 核心身份"
    assert index.splitlines()[4] == "- [mine.md](mine.md) — 自由筆記"


def test_without_memory_dir_only_prompts_are_copied(tmp_path: Path):
    kernel_dir = tmp_path / "kernel"
    kernel_dir.mkdir()

    M0186StateFileAndRecall().upgrade(kernel_dir, TEMPLATES)

    assert (kernel_dir / "agents" / "recall" / "prompts" / "system.md").exists()
    assert not (tmp_path / "memory").exists()


def test_config_keys_are_migrated(tmp_path: Path):
    kernel_dir = tmp_path / "kernel"
    kernel_dir.mkdir()
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(yaml.safe_dump({
        "tools": {
            "memory_sync": {"every_n_turns": 9, "max_retries": 1},
            "memory_edit": {"allow_failure": False},
            "memory_search": {"bm25": {"exclude": ["memory/archive/temp-memory/"]}},
        },
        "maintenance": {
            "archive": {"retain_days": 3},
            "curate": {"enabled": True, "digest_retain_days": 14, "digest_max_chars": 1200},
            "session_file_cleanup": {"enabled": True, "retention_days": 30},
        },
        "agents": {"brain": {"llm": "x.yaml"}},
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")

    M0186StateFileAndRecall().upgrade(kernel_dir, TEMPLATES)

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["tools"]["memory_sync"] == {"max_retries": 1}
    assert config["tools"]["memory_edit"]["temp_memory_max_chars"] == 6000
    assert config["tools"]["memory_search"]["bm25"]["exclude"] == [
        "memory/archive/temp-memory/", "memory/archive/curation/",
    ]
    assert "archive" not in config["maintenance"]
    assert config["maintenance"]["curate"] == {"enabled": True}
    assert config["maintenance"]["session_file_cleanup"]["debug_retention_days"] == 7
    assert config["agents"]["recall"]["llm"] == "cfgs/llm/kano-proxy/recall.yaml"
    assert config["agents"]["recall"]["recall"]["prefetch_max_tool_calls"] == 6
