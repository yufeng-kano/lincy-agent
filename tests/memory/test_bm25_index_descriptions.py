"""Tests for BM25 index.md description injection into search tokens."""

from pathlib import Path

from lincy.memory.bm25_search import (
    BM25MemorySearch,
    _load_index_descriptions,
)


def _make_memory_with_index(tmp_path: Path) -> Path:
    """Create memory with index.md descriptions."""
    mem = tmp_path / "memory"
    mem.mkdir()

    people = mem / "people" / "yufeng"
    people.mkdir(parents=True)
    (people / "index.md").write_text(
        "# 毓峰\n\n"
        "- [health.md](health.md) \u2014 用藥紀錄、過敏體質、感冒歷史\n"
        "- [schedule.md](schedule.md) \u2014 每週行程、APCS 教課、通勤路線\n",
        encoding="utf-8",
    )
    # health.md only mentions drug names, not "用藥"
    (people / "health.md").write_text(
        "# 健康紀錄\n\n"
        "- Sanpylon + Clonopam 0.5mg\n"
        "- 換季容易不舒服\n",
        encoding="utf-8",
    )
    (people / "schedule.md").write_text(
        "# 行程\n\n"
        "- 每週日下午南科\n"
        "- 地點：南部科學園區\n",
        encoding="utf-8",
    )

    agent = mem / "agent"
    agent.mkdir()
    (agent / "index.md").write_text("# Agent\n", encoding="utf-8")
    (agent / "persona.md").write_text("persona data", encoding="utf-8")

    return mem


class TestLoadIndexDescriptions:
    def test_parse_descriptions(self, tmp_path: Path):
        mem = _make_memory_with_index(tmp_path)
        result = _load_index_descriptions(mem)
        assert "health.md" in result
        parent_key = list(result["health.md"].keys())[0]
        assert "\u7528\u85e5" in result["health.md"][parent_key]  # "用藥"

    def test_missing_index_returns_empty(self, tmp_path: Path):
        mem = tmp_path / "memory"
        mem.mkdir()
        result = _load_index_descriptions(mem)
        assert result == {}


class TestIndexDescriptionBoostsSearch:
    def test_description_keyword_matches_file(self, tmp_path: Path):
        """Searching '用藥' should find health.md via index description,
        even though health.md only contains drug names like 'Sanpylon'."""
        mem = _make_memory_with_index(tmp_path)
        search = BM25MemorySearch(mem)
        result = search.search("\u7528\u85e5 \u904e\u654f")  # "用藥 過敏"
        assert "health.md" in result

    def test_search_without_index_still_works(self, tmp_path: Path):
        """Search should still work when index.md has no descriptions."""
        mem = _make_memory_with_index(tmp_path)
        # Remove descriptions from index
        people_index = mem / "people" / "yufeng" / "index.md"
        people_index.write_text("# 毓峰\n", encoding="utf-8")

        search = BM25MemorySearch(mem)
        result = search.search("Sanpylon Clonopam")
        assert "health.md" in result

    def test_concept_search_via_description(self, tmp_path: Path):
        """Index description 'APCS 教課' should boost schedule.md
        even though the file content only says '南科'."""
        mem = _make_memory_with_index(tmp_path)
        search = BM25MemorySearch(mem)
        result = search.search("APCS \u6559\u8ab2")  # "APCS 教課"
        assert "schedule.md" in result
