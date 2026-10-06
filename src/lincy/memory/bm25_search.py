"""Deterministic BM25 memory search with jieba tokenization."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jieba
from rank_bm25 import BM25Okapi

from ..core.schema import BM25SearchConfig
from ..llm.schema import ToolDefinition, ToolParameter

logger = logging.getLogger(__name__)

# Suppress jieba startup info logs (prefix dict/cache messages) in console output.
jieba.setLogLevel(logging.WARNING)

# Date normalization: "2月22日" -> "02-22", "2026年2月22日" -> "2026-02-22"
_ZH_DATE_PATTERNS = [
    (
        re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})[日號]?"),
        lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}",
    ),
    (
        re.compile(r"(\d{1,2})月(\d{1,2})[日號]?"),
        lambda m: f"{int(m.group(1)):02d}-{int(m.group(2)):02d}",
    ),
]

_STOPWORDS: frozenset[str] = frozenset({
    "的", "了", "在", "是", "我", "有", "和", "就",
    "不", "人", "都", "一", "一個", "上", "也", "很",
    "到", "說", "要", "去", "你", "會", "著", "沒有",
    "看", "好", "自己", "這",
})


_INDEX_LINK_RE = re.compile(r"^-\s*\[.*?\]\((.+?)\)\s*(?:\u2014|--)\s*(.+)$")


MEMORY_SEARCH_DEFINITION = ToolDefinition(
    name="memory_search",
    description=(
        "Search memory for content relevant to a topic or question. "
        "Returns matching snippets from memory files with surrounding context. "
        "Usually sufficient without follow-up read_file. "
        "Call this when you need to recall past information, knowledge, "
        "experiences, or facts about people."
    ),
    parameters={
        "query": ToolParameter(
            type="string",
            description=(
                "What you are looking for in memory. Use 3-5 specific keywords. "
                "Avoid common terms that appear everywhere. "
                "File descriptions in index.md are also searched. "
                "Examples: 'APCS teaching schedule', "
                "'medication side effects', 'cooking skills'."
            ),
        ),
    },
    required=["query"],
)


def _normalize_dates(text: str) -> str:
    """Normalize Chinese date formats to numeric form for matching."""
    for pattern, replacement in _ZH_DATE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _load_index_descriptions(memory_dir: Path) -> dict[str, dict[str, str]]:
    """Parse all index.md files and extract file descriptions.

    Returns {filename: {parent_dir_str: description}} for quick lookup.
    Format expected: ``- [title](path) \u2014 description``
    """
    result: dict[str, dict[str, str]] = {}
    for index_file in memory_dir.rglob("index.md"):
        try:
            content = index_file.read_text(encoding="utf-8")
        except Exception:
            continue
        parent_str = str(index_file.parent)
        for line in content.splitlines():
            m = _INDEX_LINK_RE.match(line.strip())
            if not m:
                continue
            link_path = m.group(1)
            description = m.group(2).strip()
            # Extract just the filename from the link path
            filename = link_path.rstrip("/").rsplit("/", 1)[-1]
            result.setdefault(filename, {})[parent_str] = description
    return result


def _is_cjk(char: str) -> bool:
    """Check if a character is CJK (Chinese/Japanese/Korean)."""
    cp = ord(char)
    return 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF


def _tokenize(text: str) -> list[str]:
    """Tokenize text using jieba, filtering stopwords and short tokens.

    CJK single-char tokens are kept (e.g. name chars like '峰');
    non-CJK tokens require length >= 2 (filters 'a', 'b', etc.).
    """
    result: list[str] = []
    for t in jieba.cut(text, cut_all=False):
        if t in _STOPWORDS:
            continue
        if len(t) >= 2:
            result.append(t)
        elif len(t) == 1 and _is_cjk(t):
            result.append(t)
    return result


class _MemoryDocument:
    """A loaded memory file with its content and tokens."""

    __slots__ = ("rel_path", "content", "lines", "tokens")

    def __init__(self, rel_path: str, content: str, tokens: list[str]) -> None:
        self.rel_path = rel_path
        self.content = content
        self.lines = content.splitlines()
        self.tokens = tokens


class BM25MemorySearch:
    """BM25-based memory search over .md files."""

    def __init__(
        self,
        memory_dir: Path,
        config: BM25SearchConfig | None = None,
    ) -> None:
        self.memory_dir = memory_dir
        self.config = config or BM25SearchConfig()

    def search(self, query: str) -> str:
        """Search memory and return formatted snippets."""
        documents = self._load_documents()
        if not documents:
            return "No relevant memory files found for this query."

        corpus = [doc.tokens for doc in documents]
        bm25 = BM25Okapi(corpus)

        # Preprocess query
        processed = query
        if self.config.date_normalization:
            processed = _normalize_dates(processed)
        query_tokens = _tokenize(processed)
        if not query_tokens:
            return "No relevant memory files found for this query."

        # BM25 scoring
        scores = bm25.get_scores(query_tokens)
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)

        # Collect top-k with positive score
        results: list[tuple[_MemoryDocument, float]] = []
        for idx, score in ranked:
            if score <= 0 or len(results) >= self.config.top_k:
                break
            results.append((documents[idx], score))

        if not results:
            return "No relevant memory files found for this query."

        return self._build_response(results, query_tokens)

    def _load_documents(self) -> list[_MemoryDocument]:
        """Scan memory_dir and tokenize all .md files.

        Index.md descriptions are injected into each file's tokens
        to improve search quality for conceptual queries.
        """
        if not self.memory_dir.exists():
            return []

        # Pre-load index descriptions for token injection
        index_descs = _load_index_descriptions(self.memory_dir)

        documents: list[_MemoryDocument] = []
        for md_file in sorted(self.memory_dir.rglob("*.md")):
            if md_file.name == "index.md":
                continue
            rel_path = str(md_file.relative_to(self.memory_dir.parent))
            if self._is_excluded(rel_path):
                continue
            try:
                content = md_file.read_text(encoding="utf-8")
            except Exception:
                continue
            tokens = _tokenize(content)

            # Inject parent index.md description tokens
            desc = index_descs.get(md_file.name, {}).get(str(md_file.parent))
            if desc:
                tokens.extend(_tokenize(desc))

            if not tokens:
                continue
            documents.append(_MemoryDocument(rel_path, content, tokens))
        return documents

    def _is_excluded(self, rel_path: str) -> bool:
        """Return True when rel_path matches an explicit exclude entry."""
        for pattern in self.config.exclude:
            if pattern.endswith("/"):
                if rel_path.startswith(pattern):
                    return True
                continue
            if rel_path == pattern:
                return True
        return False

    def _build_response(
        self,
        results: list[tuple[_MemoryDocument, float]],
        query_tokens: list[str],
    ) -> str:
        """Build formatted snippet response within char budget."""
        query_tokens_lower = {t.lower() for t in query_tokens}
        parts: list[str] = []
        total_chars = 0

        for doc, _score in results:
            snippets = self._extract_snippets(doc, query_tokens_lower)
            if not snippets:
                continue

            section = f"## {doc.rel_path}\n\n" + "\n...\n".join(snippets)
            section_chars = len(section)

            if total_chars + section_chars > self.config.max_response_chars:
                if not parts:
                    # Always include at least the first result (truncated)
                    parts.append(section[:self.config.max_response_chars])
                break

            parts.append(section)
            total_chars += section_chars

        if not parts:
            return "No relevant memory files found for this query."
        return "\n\n".join(parts)

    def _extract_snippets(
        self,
        doc: _MemoryDocument,
        query_tokens_lower: set[str],
    ) -> list[str]:
        """Extract matching line regions from a document."""
        lines = doc.lines
        ctx = self.config.snippet_lines
        max_snippets = self.config.max_snippets_per_file

        # Find lines containing any query token
        matching: list[int] = []
        for i, line in enumerate(lines):
            line_lower = line.lower()
            if any(token in line_lower for token in query_tokens_lower):
                matching.append(i)

        if not matching:
            # No line-level match; return first few lines as preview
            preview = "\n".join(lines[:ctx * 2 + 1])
            return [preview] if preview.strip() else []

        # Merge overlapping ranges
        snippets: list[str] = []
        used: list[tuple[int, int]] = []

        for line_idx in matching:
            if len(snippets) >= max_snippets:
                break
            start = max(0, line_idx - ctx)
            end = min(len(lines), line_idx + ctx + 1)

            if any(start < ue and end > us for us, ue in used):
                continue

            snippet = "\n".join(lines[start:end])
            if snippet.strip():
                snippets.append(snippet)
                used.append((start, end))

        return snippets


# -- Tool factory --------------------------------------------------------------

def create_bm25_memory_search(
    search: BM25MemorySearch,
) -> Callable[..., str]:
    """Create memory_search tool function bound to BM25MemorySearch."""

    def memory_search(query: str = "", **kwargs: Any) -> str:
        q = query or kwargs.get("q", "") or kwargs.get("search", "")
        if not isinstance(q, str) or not q.strip():
            return "Error: query is required."
        return search.search(q.strip())

    return memory_search
