"""Tests for BM25 memory search."""

from pathlib import Path

from lincy.core.schema import BM25SearchConfig
from lincy.memory.bm25_search import (
    BM25MemorySearch,
    create_bm25_memory_search,
    _normalize_dates,
    _tokenize,
)


# -- helpers -------------------------------------------------------------------

def _make_memory(tmp_path: Path) -> Path:
    """Create a minimal memory directory with Chinese content."""
    mem = tmp_path / "memory"
    mem.mkdir()
    agent_dir = mem / "agent"
    agent_dir.mkdir()
    (agent_dir / "index.md").write_text("# Agent Index", encoding="utf-8")
    (agent_dir / "persona.md").write_text(
        "我是陪伴者，負責關心毓峰的生活", encoding="utf-8",
    )
    (agent_dir / "recent.md").write_text(
        "# 近期記憶\n\n"
        "- [2026-02-22 10:00] 毓峰說今天要去南科教 APCS\n"
        "- [2026-02-22 09:30] 毓峰吃了早餐\n"
        "- [2026-02-21 22:00] 毓峰確認藥已服用完畢\n",
        encoding="utf-8",
    )
    people_dir = mem / "people" / "yufeng"
    people_dir.mkdir(parents=True)
    (people_dir / "index.md").write_text("# 毓峰", encoding="utf-8")
    (people_dir / "schedule.md").write_text(
        "# 毓峰的行程\n\n"
        "- 每週日下午：南科 APCS 教課\n"
        "- 地點：南部科學園區\n"
        "- 需提前準備教材\n",
        encoding="utf-8",
    )
    (people_dir / "health.md").write_text(
        "# 毓峰的健康\n\n"
        "- 目前服用感冒藥\n"
        "- 過敏體質，換季容易不舒服\n",
        encoding="utf-8",
    )
    return mem


# -- unit tests: helpers -------------------------------------------------------

class TestNormalizeDates:
    def test_chinese_month_day(self):
        assert "02-22" in _normalize_dates("2月22日")

    def test_chinese_month_day_no_suffix(self):
        assert "02-22" in _normalize_dates("2月22")

    def test_full_date_with_year(self):
        assert "2026-02-22" in _normalize_dates("2026年2月22日")

    def test_no_dates_unchanged(self):
        assert _normalize_dates("APCS 教課") == "APCS 教課"


class TestTokenize:
    def test_filters_stopwords(self):
        tokens = _tokenize("我的健康狀況")
        assert "我" not in tokens
        assert "的" not in tokens

    def test_keeps_meaningful_tokens(self):
        tokens = _tokenize("南科 APCS 教課")
        assert any("APCS" in t for t in tokens)

    def test_filters_short_tokens(self):
        tokens = _tokenize("a b cd efg")
        assert "a" not in tokens
        assert "b" not in tokens

    def test_empty_input(self):
        assert _tokenize("") == []


# -- unit tests: BM25MemorySearch ----------------------------------------------

class TestBM25MemorySearch:
    def test_basic_search_returns_snippets(self, tmp_path: Path):
        mem = _make_memory(tmp_path)
        search = BM25MemorySearch(mem)
        result = search.search("APCS 教課")
        assert "APCS" in result
        assert "## memory/" in result

    def test_specific_terms_rank_higher(self, tmp_path: Path):
        """health.md should rank high for health-specific terms."""
        mem = _make_memory(tmp_path)
        search = BM25MemorySearch(mem)
        result = search.search("感冒藥 過敏")
        assert "health.md" in result

    def test_idf_weighting(self, tmp_path: Path):
        """Rare terms should produce better rankings than common terms."""
        mem = _make_memory(tmp_path)
        search = BM25MemorySearch(mem)
        # "APCS" is rare -> schedule.md and recent.md should rank high
        result = search.search("APCS 南科 教課")
        # schedule.md should appear (it has all three terms)
        assert "schedule.md" in result

    def test_empty_query_tokens(self, tmp_path: Path):
        mem = _make_memory(tmp_path)
        search = BM25MemorySearch(mem)
        # Query with only stopwords produces no tokens
        result = search.search("的 了 在")
        assert "No relevant" in result

    def test_no_memory_dir(self, tmp_path: Path):
        search = BM25MemorySearch(tmp_path / "nonexistent")
        result = search.search("anything")
        assert "No relevant" in result

    def test_index_files_excluded(self, tmp_path: Path):
        mem = _make_memory(tmp_path)
        search = BM25MemorySearch(mem)
        result = search.search("Agent Index")
        assert "index.md" not in result

    def test_top_k_limits_results(self, tmp_path: Path):
        mem = _make_memory(tmp_path)
        config = BM25SearchConfig(top_k=1)
        search = BM25MemorySearch(mem, config=config)
        result = search.search("APCS 教課")
        # Only 1 file section header
        assert result.count("## memory/") == 1

    def test_max_response_chars(self, tmp_path: Path):
        mem = _make_memory(tmp_path)
        config = BM25SearchConfig(max_response_chars=100)
        search = BM25MemorySearch(mem, config=config)
        result = search.search("APCS 教課 南科")
        # Should be truncated but still contain some content
        assert len(result) <= 200  # tolerance for first-result inclusion

    def test_date_normalization_query(self, tmp_path: Path):
        mem = _make_memory(tmp_path)
        search = BM25MemorySearch(mem)
        result = search.search("2月22日")
        # Should match content containing "02-22" patterns
        assert "2026-02-22" in result or "02-22" in result

    def test_snippet_context_lines(self, tmp_path: Path):
        mem = _make_memory(tmp_path)
        config = BM25SearchConfig(snippet_lines=1)
        search = BM25MemorySearch(mem, config=config)
        result = search.search("APCS 教課")
        assert "APCS" in result

    def test_max_snippets_per_file(self, tmp_path: Path):
        mem = _make_memory(tmp_path)
        config = BM25SearchConfig(max_snippets_per_file=1)
        search = BM25MemorySearch(mem, config=config)
        result = search.search("APCS 教課")
        assert "## memory/" in result

    def test_no_matches(self, tmp_path: Path):
        mem = _make_memory(tmp_path)
        search = BM25MemorySearch(mem)
        result = search.search("zzzznonexistent")
        assert "No relevant" in result

    def test_exact_exclude_skips_matching_file(self, tmp_path: Path):
        mem = _make_memory(tmp_path)
        config = BM25SearchConfig(exclude=["memory/agent/recent.md"])
        search = BM25MemorySearch(mem, config=config)
        result = search.search("APCS 教課")
        assert "schedule.md" in result
        assert "memory/agent/recent.md" not in result

    def test_directory_exclude_skips_matching_subtree(self, tmp_path: Path):
        mem = _make_memory(tmp_path)
        config = BM25SearchConfig(exclude=["memory/people/"])
        search = BM25MemorySearch(mem, config=config)
        result = search.search("感冒藥 過敏")
        assert "health.md" not in result
        assert "No relevant" in result

    def test_non_matching_exclude_does_not_affect_results(self, tmp_path: Path):
        mem = _make_memory(tmp_path)
        config = BM25SearchConfig(exclude=["memory/agent/knowledge/"])
        search = BM25MemorySearch(mem, config=config)
        result = search.search("APCS 教課")
        assert "schedule.md" in result


# -- unit tests: factory -------------------------------------------------------

class TestCreateBm25MemorySearch:
    def test_factory_returns_callable(self, tmp_path: Path):
        mem = _make_memory(tmp_path)
        search = BM25MemorySearch(mem)
        tool_fn = create_bm25_memory_search(search)
        output = tool_fn(query="APCS 教課")
        assert isinstance(output, str)
        assert "APCS" in output

    def test_factory_empty_query(self, tmp_path: Path):
        mem = _make_memory(tmp_path)
        search = BM25MemorySearch(mem)
        tool_fn = create_bm25_memory_search(search)
        output = tool_fn(query="")
        assert "Error" in output

    def test_factory_kwargs_fallback(self, tmp_path: Path):
        mem = _make_memory(tmp_path)
        search = BM25MemorySearch(mem)
        tool_fn = create_bm25_memory_search(search)
        output = tool_fn(q="APCS 教課")
        assert "APCS" in output
