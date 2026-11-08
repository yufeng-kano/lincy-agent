"""Recall sub-agent: grep memory files and raw transcripts for past facts.

The brain only knows what is loaded into its context. Everything else (older
memory files, daily state snapshots, raw session transcripts) is searched on
demand by this sub-agent. It runs a small capped tool loop on a fast model and
returns a short list of sourced facts, or nothing.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...llm.base import LLMClient
from ...llm.content import content_to_text
from ...llm.schema import Message, ToolDefinition, ToolParameter, make_tool_result_message
from ...llm.session import llm_session
from ...session.schema import SessionEntry
from ...timezone_utils import format_local_stamp, now as tz_now
from ..registry import ToolRegistry
from ..security import resolve_allowed_path
from .file import create_read_file

if TYPE_CHECKING:
    from ...core.schema import RecallAgentConfig

logger = logging.getLogger(__name__)

_SESSION_FILES = frozenset({"transcript.jsonl", "messages.jsonl"})
_MAX_GREP_OUTPUT_CHARS = 6000
_MAX_GREP_MATCHES = 200
_MAX_LINE_CHARS = 300
_MAX_READ_LINE_CHARS = 1200
_MAX_READ_OUTPUT_CHARS = 12000
_MAX_LIST_ENTRIES = 200
_LIMIT_TOOL_RESULT = (
    "Tool call limit reached. Do not call tools. Answer now from what you have found."
)
_FINALIZE_PROMPT = (
    "Tool call limit reached. Write the final answer now using only the facts "
    "already found, in the required output format. Output NONE if nothing "
    "relevant was found. Do not call tools."
)
_DEADLINE_PROMPT = (
    "Time budget reached. Do not call any more tools. Answer now from what "
    "you have found, or output NONE."
)
_NONE_PATTERN = re.compile(r"NONE[.!]?", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Corpus access (path guard, glob expansion, transcript rendering)
# ---------------------------------------------------------------------------

def _allowed_relative(agent_os_dir: Path, target: Path) -> Path | None:
    """Return *target* relative to the agent dir when recall may read it.

    Only memory markdown and the two conversation files of brain sessions are
    searchable; debug dumps (requests/responses/events/turns/checkpoints) are
    never exposed to the sub-agent.
    """
    try:
        rel = target.resolve().relative_to(agent_os_dir.resolve())
    except (ValueError, OSError):
        return None
    parts = rel.parts
    if not parts:
        return None
    if parts[0] == "memory":
        if target.is_dir() or target.suffix.lower() == ".md":
            return rel
        return None
    if parts[:2] == ("session", "brain"):
        if len(parts) <= 3 and target.is_dir():
            return rel
        if len(parts) == 4 and parts[3] in _SESSION_FILES and target.is_file():
            return rel
        return None
    return None


def _expand_glob(agent_os_dir: Path, path_glob: str) -> list[tuple[Path, Path]] | str:
    """Expand a glob under the agent dir into (absolute, relative) pairs.

    Returns an error string for globs that leave the searchable corpus.
    Live memory files sort first (ascending); daily snapshots, other archive
    copies and session files follow newest first, so a capped result keeps
    the current truth and then the most recent history.
    """
    pattern = path_glob.strip()
    if not pattern:
        return "Error: path_glob is required."
    candidate = Path(pattern).expanduser()
    if candidate.is_absolute():
        try:
            pattern = str(candidate.resolve().relative_to(agent_os_dir.resolve()))
        except ValueError:
            return f"Error: path is outside the agent directory: {path_glob}"
    pattern = pattern.removeprefix("./")
    if ".." in Path(pattern).parts:
        return "Error: '..' is not allowed in path_glob."
    if not (pattern == "memory" or pattern.startswith("memory/")
            or pattern == "session/brain" or pattern.startswith("session/brain/")):
        return "Error: path_glob must start with memory/ or session/brain/."
    try:
        matches = list(agent_os_dir.glob(pattern))
    except (ValueError, NotImplementedError) as exc:
        return f"Error: invalid path_glob: {exc}"

    live_paths: list[tuple[Path, Path]] = []
    daily_paths: list[tuple[Path, Path]] = []
    archive_paths: list[tuple[Path, Path]] = []
    session_paths: list[tuple[Path, Path]] = []
    for target in matches:
        rel = _allowed_relative(agent_os_dir, target)
        if rel is None:
            continue
        parts = rel.parts
        if parts[0] != "memory":
            session_paths.append((target, rel))
        elif len(parts) > 2 and parts[1] == "archive":
            (daily_paths if parts[2] == "temp-memory" else archive_paths).append((target, rel))
        else:
            live_paths.append((target, rel))
    live_paths.sort(key=lambda pair: str(pair[1]))
    for group in (daily_paths, archive_paths, session_paths):
        group.sort(key=lambda pair: str(pair[1]), reverse=True)
    return live_paths + daily_paths + archive_paths + session_paths


def _clip(text: str, limit: int) -> str:
    text = text.replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _compact_json(value: Any, limit: int) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(value)
    return _clip(rendered, limit)


def render_transcript_entry(entry: SessionEntry) -> str:
    """Render one transcript entry as a single readable line.

    Format: ``[<local stamp> <channel>/<sender> <role>] <text>``. Tool calls
    are summarised after the text; send_message shows its body because that is
    what the user actually received.
    """
    stamp = format_local_stamp(entry.timestamp) if entry.timestamp else "unknown time"
    who = entry.role
    if entry.channel or entry.sender:
        who = f"{entry.channel or '-'}/{entry.sender or '-'} {entry.role}"
    text = content_to_text(entry.content).strip()
    if entry.role == "tool":
        text = f"({entry.message.name}) {_clip(text, 200)}"
    pieces = [text] if text else []
    for call in entry.message.tool_calls or []:
        args = call.arguments if isinstance(call.arguments, dict) else {}
        if call.name == "send_message":
            body = args.get("body") or ""
            target = args.get("to") or args.get("channel") or ""
            pieces.append(f"-> send_message({target}): {body}")
        else:
            pieces.append(f"-> {call.name}({_compact_json(args, 200)})")
    return f"[{stamp} {who}] " + " ".join(pieces)


def _parse_entry(line: str) -> SessionEntry | None:
    line = line.strip()
    if not line:
        return None
    try:
        return SessionEntry.model_validate_json(line)
    except ValueError:
        return None


def format_context_tail(
    entries: Iterable[SessionEntry],
    *,
    max_entries: int = 3,
    max_chars: int = 300,
) -> str:
    """Render the last few user/assistant texts for disambiguating a question.

    User lines keep their channel and sender so the sub-agent can tell whose
    conversation it is looking at.
    """
    picked: list[str] = []
    for entry in reversed(list(entries)):
        if entry.role not in ("user", "assistant"):
            continue
        text = content_to_text(entry.content).strip()
        if not text:
            continue
        label = entry.role
        if entry.role == "user" and (entry.channel or entry.sender):
            label = f"user ({'/'.join(p for p in (entry.channel, entry.sender) if p)})"
        picked.append(f"{label}: {_clip(text, max_chars)}")
        if len(picked) >= max_entries:
            break
    return "\n".join(reversed(picked))


# ---------------------------------------------------------------------------
# Private tools of the sub-agent
# ---------------------------------------------------------------------------

_GREP_DEFINITION = ToolDefinition(
    name="grep",
    description=(
        "Search files with a case-insensitive regular expression. Markdown "
        "files return matching lines with context; transcript .jsonl files "
        "return rendered conversation entries with context. Use count_only "
        "first to see which files match before reading their lines."
    ),
    parameters={
        "pattern": ToolParameter(
            type="string",
            description="Python regular expression. Use | for alternatives.",
        ),
        "path_glob": ToolParameter(
            type="string",
            description=(
                "Glob under the agent directory, e.g. 'memory/**/*.md', "
                "'memory/archive/temp-memory/2026-11-*.md', "
                "'session/brain/202611*/transcript.jsonl'. Defaults to memory/**/*.md."
            ),
        ),
        "context_lines": ToolParameter(
            type="integer",
            description="Lines (or transcript entries) of context around each match. Default 1.",
        ),
        "max_matches": ToolParameter(
            type="integer",
            description="Stop after this many matches. Default 40.",
        ),
        "count_only": ToolParameter(
            type="boolean",
            description="Return only per-file match counts.",
        ),
    },
    required=["pattern"],
)

_LIST_DIR_DEFINITION = ToolDefinition(
    name="list_dir",
    description=(
        "List files and directories matching a glob under memory/ or "
        "session/brain/, with sizes and modification times."
    ),
    parameters={
        "path_glob": ToolParameter(
            type="string",
            description="Glob such as 'memory/people/*' or 'session/brain/202611*'.",
        ),
    },
    required=["path_glob"],
)

_READ_FILE_DEFINITION = ToolDefinition(
    name="read_file",
    description=(
        "Read a memory markdown file or a session transcript (.jsonl, rendered "
        "one entry per line) with line numbers."
    ),
    parameters={
        "path": ToolParameter(type="string", description="Path relative to the agent directory."),
        "offset": ToolParameter(
            type="integer",
            description="First line to read (1-indexed). Default 1.",
        ),
        "limit": ToolParameter(
            type="integer",
            description="Maximum lines to read. Default 200.",
        ),
    },
    required=["path"],
)


def _grep_markdown(
    target: Path,
    rel: Path,
    regex: re.Pattern[str],
    *,
    context_lines: int,
) -> list[tuple[int, list[str]]]:
    """Return (match_line_no, rendered block lines) for each match."""
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    blocks: list[tuple[int, list[str]]] = []
    for index, line in enumerate(lines):
        if not regex.search(line):
            continue
        start = max(0, index - context_lines)
        end = min(len(lines), index + context_lines + 1)
        block = []
        for j in range(start, end):
            sep = ":" if j == index else "-"
            block.append(f"{rel}{sep}{j + 1}{sep} {_clip(lines[j], _MAX_LINE_CHARS)}")
        blocks.append((index + 1, block))
    return blocks


def _grep_transcript(
    target: Path,
    rel: Path,
    regex: re.Pattern[str],
    *,
    context_lines: int,
) -> list[tuple[int, list[str]]]:
    """Like _grep_markdown, but each line is a rendered SessionEntry.

    Only the rendered text is searched: a pattern that spans the stamp, the
    sender and the body must match the line the way the model sees it, and
    entry metadata (overlay blocks, earlier [Recall] output) never matches.
    """
    try:
        raw_lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rendered: list[str | None] = []
    for raw in raw_lines:
        entry = _parse_entry(raw)
        rendered.append(render_transcript_entry(entry) if entry else None)

    blocks: list[tuple[int, list[str]]] = []
    for index, text in enumerate(rendered):
        if text is None or not regex.search(text):
            continue
        start = max(0, index - context_lines)
        end = min(len(rendered), index + context_lines + 1)
        block = []
        for j in range(start, end):
            line_text = rendered[j]
            if line_text is None:
                continue
            sep = ":" if j == index else "-"
            block.append(f"{rel}{sep}{j + 1}{sep} {_clip(line_text, _MAX_LINE_CHARS)}")
        blocks.append((index + 1, block))
    return blocks


def _make_grep(agent_os_dir: Path) -> Callable[..., str]:
    def grep(
        pattern: str = "",
        path_glob: str = "memory/**/*.md",
        context_lines: int = 1,
        max_matches: int = 40,
        count_only: bool = False,
        **kwargs: Any,
    ) -> str:
        if not pattern:
            return "Error: pattern is required."
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return f"Error: invalid pattern: {exc}"
        files = _expand_glob(agent_os_dir, path_glob or "memory/**/*.md")
        if isinstance(files, str):
            return files
        files = [pair for pair in files if pair[0].is_file()]
        if not files:
            return f"No files match {path_glob}."
        context_lines = max(0, min(int(context_lines), 5))
        max_matches = max(1, min(int(max_matches), _MAX_GREP_MATCHES))

        out: list[str] = []
        total_chars = 0
        total_matches = 0
        matched_files = 0
        truncated = False
        for target, rel in files:
            if rel.suffix == ".jsonl":
                blocks = _grep_transcript(target, rel, regex, context_lines=context_lines)
            else:
                blocks = _grep_markdown(target, rel, regex, context_lines=context_lines)
            if not blocks:
                continue
            matched_files += 1
            if count_only:
                out.append(f"{rel}: {len(blocks)}")
                total_matches += len(blocks)
                continue
            for _, block in blocks:
                if total_matches >= max_matches or total_chars >= _MAX_GREP_OUTPUT_CHARS:
                    truncated = True
                    break
                chunk = "\n".join(block)
                out.append(chunk)
                out.append("--")
                total_chars += len(chunk) + 3
                total_matches += 1
            if truncated:
                break
        if total_matches == 0:
            return f"No matches for /{pattern}/ in {len(files)} file(s)."
        if count_only:
            out.append(f"Total: {total_matches} match(es) in {matched_files} of {len(files)} file(s).")
            return "\n".join(out)
        if out and out[-1] == "--":
            out.pop()
        if truncated:
            out.append(
                f"(stopped after {total_matches} matches; narrow the pattern or path_glob)"
            )
        return "\n".join(out)

    return grep


def _make_list_dir(agent_os_dir: Path) -> Callable[..., str]:
    def list_dir(path_glob: str = "", **kwargs: Any) -> str:
        files = _expand_glob(agent_os_dir, path_glob)
        if isinstance(files, str):
            return files
        if not files:
            return f"Nothing matches {path_glob}."
        lines: list[str] = []
        for target, rel in files[:_MAX_LIST_ENTRIES]:
            try:
                stat = target.stat()
            except OSError:
                continue
            modified = format_local_stamp(
                datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            )
            if target.is_dir():
                lines.append(f"{rel}/  (dir, modified {modified})")
            else:
                lines.append(f"{rel}  ({stat.st_size} bytes, modified {modified})")
        if len(files) > _MAX_LIST_ENTRIES:
            lines.append(f"(showing {_MAX_LIST_ENTRIES} of {len(files)} entries)")
        return "\n".join(lines)

    return list_dir


def _make_read_file(agent_os_dir: Path) -> Callable[..., str]:
    allowed = [str(agent_os_dir)]
    base_read_file = create_read_file(allowed, agent_os_dir)

    def read_file(path: str = "", offset: int = 1, limit: int = 200, **kwargs: Any) -> str:
        if not path:
            return "Error: path is required."
        target = resolve_allowed_path(path, allowed, agent_os_dir)
        rel = _allowed_relative(agent_os_dir, target) if target is not None else None
        if target is None or rel is None or not target.is_file():
            return f"Error: path is not readable by recall: {path}"
        offset = max(1, int(offset))
        limit = max(1, int(limit))
        if rel.suffix != ".jsonl":
            return base_read_file(str(target), offset=offset, limit=limit)
        raw_lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = offset - 1
        chunk = raw_lines[start:start + limit]
        out = [f"{rel} lines {start + 1}-{start + len(chunk)} of {len(raw_lines)}"]
        total = 0
        for index, raw in enumerate(chunk):
            entry = _parse_entry(raw)
            text = render_transcript_entry(entry) if entry else "(unparseable line)"
            line = f"{start + index + 1}: {_clip(text, _MAX_READ_LINE_CHARS)}"
            total += len(line) + 1
            if total > _MAX_READ_OUTPUT_CHARS:
                out.append("(output truncated; continue with a larger offset)")
                break
            out.append(line)
        return "\n".join(out)

    return read_file


def build_recall_registry(agent_os_dir: Path) -> ToolRegistry:
    """Registry with the three read-only tools the recall sub-agent may use."""
    registry = ToolRegistry()
    registry.register("grep", _make_grep(agent_os_dir), _GREP_DEFINITION)
    registry.register("list_dir", _make_list_dir(agent_os_dir), _LIST_DIR_DEFINITION)
    registry.register("read_file", _make_read_file(agent_os_dir), _READ_FILE_DEFINITION)
    return registry


# ---------------------------------------------------------------------------
# Sub-agent
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RecallRun:
    """Raw outcome of one recall tool loop."""

    text: str
    tool_calls: int
    hit_limit: bool
    timed_out: bool = False


class RecallAgent:
    """Answer one self-contained question from memory files and transcripts."""

    def __init__(
        self,
        client: LLMClient,
        system_prompt: str,
        *,
        agent_os_dir: Path,
        config: "RecallAgentConfig",
    ):
        self.client = client
        self.system_prompt = system_prompt
        self.agent_os_dir = agent_os_dir
        self.config = config
        self._registry = build_recall_registry(agent_os_dir)

    def recall(
        self,
        question: str,
        *,
        sender: str | None = None,
        channel: str | None = None,
        context_tail: str = "",
        exclude_paths: Iterable[str] = (),
        max_tool_calls: int | None = None,
        deadline_seconds: float | None = None,
    ) -> str | None:
        """Return sourced facts for *question*, or None when nothing was found.

        ``sender`` / ``channel`` name who is asking, so first-person questions
        resolve to that person's files and conversations.
        """
        run = self.run(
            question,
            sender=sender,
            channel=channel,
            context_tail=context_tail,
            exclude_paths=exclude_paths,
            max_tool_calls=max_tool_calls,
            deadline_seconds=deadline_seconds,
        )
        text = run.text.strip()
        if not text or _NONE_PATTERN.fullmatch(text):
            return None
        return _truncate_result(text, self.config.max_result_chars)

    def run(
        self,
        question: str,
        *,
        sender: str | None = None,
        channel: str | None = None,
        context_tail: str = "",
        exclude_paths: Iterable[str] = (),
        max_tool_calls: int | None = None,
        deadline_seconds: float | None = None,
    ) -> RecallRun:
        """Run the capped tool loop and return the model's final text.

        ``deadline_seconds`` bounds the wall-clock time spent on search
        rounds; once it has passed, the next round is the answer.
        """
        limit = max_tool_calls or self.config.max_tool_calls
        deadline = time.monotonic() + deadline_seconds if deadline_seconds else None
        with llm_session("recall"):
            return self._run_loop(
                _build_request(
                    question,
                    sender=sender,
                    channel=channel,
                    context_tail=context_tail,
                    exclude_paths=exclude_paths,
                    max_tool_calls=limit,
                    max_result_chars=self.config.max_result_chars,
                ),
                limit=limit,
                deadline=deadline,
            )

    def _run_loop(self, request: str, *, limit: int, deadline: float | None) -> RecallRun:
        messages = [
            Message(role="system", content=self.system_prompt),
            Message(role="user", content=request),
        ]
        tool_defs = self._registry.get_definitions()
        calls_used = 0
        hit_limit = False
        timed_out = False
        response = self.client.chat_with_tools(messages, tool_defs)
        while response.tool_calls:
            # Replay reasoning like the brain's side channels do: signed
            # thinking blocks must travel with the tool calls they produced.
            messages.append(Message(
                role="assistant",
                content=response.content,
                reasoning_content=response.reasoning_content,
                reasoning_details=response.reasoning_details,
                reasoning_origin=response.served_by,
                tool_calls=response.tool_calls,
            ))
            for call in response.tool_calls:
                if calls_used >= limit:
                    hit_limit = True
                    content = _LIMIT_TOOL_RESULT
                else:
                    content = self._registry.execute(call).content
                    calls_used += 1
                messages.append(make_tool_result_message(
                    tool_call_id=call.id,
                    name=call.name,
                    content=content,
                ))
            timed_out = deadline is not None and time.monotonic() >= deadline
            if calls_used >= limit or timed_out:
                hit_limit = True
                # Tools stay attached: Anthropic rejects a history with
                # tool_use blocks when the request defines no tools. Any
                # further call is ignored; only the text is kept.
                messages.append(Message(
                    role="user",
                    content=_DEADLINE_PROMPT if timed_out else _FINALIZE_PROMPT,
                ))
                response = self.client.chat_with_tools(messages, tool_defs)
                break
            response = self.client.chat_with_tools(messages, tool_defs)
        return RecallRun(
            text=response.content or "",
            tool_calls=calls_used,
            hit_limit=hit_limit,
            timed_out=timed_out,
        )


def _build_request(
    question: str,
    *,
    sender: str | None,
    channel: str | None,
    context_tail: str,
    exclude_paths: Iterable[str],
    max_tool_calls: int,
    max_result_chars: int,
) -> str:
    parts = [f"Now: {format_local_stamp(tz_now())}", f"Question: {question.strip()}"]
    if sender or channel:
        via = f" via {channel}" if channel else ""
        parts.append(
            f"Asked by: {sender or 'unknown'}{via}. First person in the question "
            "(I, my, me) is this person: search their files and conversations, "
            "and never report another person's facts as theirs."
        )
    if context_tail.strip():
        parts.append(
            "Recent conversation (only to disambiguate the question; do not "
            "report facts from it):\n" + context_tail.strip()
        )
    excluded = sorted({p for p in exclude_paths if p})
    if excluded:
        parts.append(
            "Already loaded in the brain's context (facts found only in these "
            "files are already known; do not report them, and output NONE if "
            "you find nothing beyond them):\n"
            + "\n".join(f"- {p}" for p in excluded)
        )
    parts.append(
        f"Budgets: {max_tool_calls} tool calls (calls beyond it are refused; "
        f"a reply without tool calls ends the run); {max_result_chars} "
        "characters for the answer."
    )
    return "\n\n".join(parts)


def _truncate_result(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text.rfind("\n", 0, max_chars)
    if cut < max_chars // 2:
        cut = max_chars
    return text[:cut].rstrip() + "\n(truncated)"


# ---------------------------------------------------------------------------
# Brain-facing tool
# ---------------------------------------------------------------------------

RECALL_DEFINITION = ToolDefinition(
    name="recall",
    description=(
        "Search past conversation transcripts and memory files for facts "
        "related to a question. A fast sub-agent greps memory/**/*.md, the "
        "daily state snapshots and session/brain/*/transcript.jsonl, then "
        "returns sourced facts with timestamps. It has NO access to the current "
        "conversation, so the question must be self-contained: name the person, "
        "the topic and any date hints. Use it when the user refers to past "
        "events, promises, numbers or 'last time' and neither the [Recall] "
        "block nor the loaded files already answer it."
    ),
    parameters={
        "question": ToolParameter(
            type="string",
            description=(
                "Self-contained question including names, topic and date hints "
                "when known, e.g. 'What did yufeng decide about the DSP homework "
                "deadline around 2026-11-05?'"
            ),
        ),
    },
    required=["question"],
)


def create_recall_tool(recall_agent: RecallAgent) -> Callable[..., str]:
    """Create the brain's recall tool bound to a RecallAgent."""

    def recall(question: str = "", **kwargs: Any) -> str:
        if not question.strip():
            return "Error: question is required."
        try:
            result = recall_agent.recall(question)
        except Exception as exc:
            logger.warning("Recall sub-agent failed: %s", exc)
            return f"Error: recall failed: {exc}"
        return result or "No relevant memory found."

    return recall
