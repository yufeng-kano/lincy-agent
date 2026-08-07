"""In-memory metrics cache with incremental JSONL reading."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .pricing import ModelPricing, compute_request_cost, get_pricing_metadata
from .session_reader import (
    SessionFiles,
    discover_sessions,
    parse_response_record,
    parse_turn_record,
    read_meta,
    read_new_lines,
)

logger = logging.getLogger(__name__)

_READ_CACHE_MEASURABLE_PROVIDERS = frozenset(
    {
        "anthropic",
        "claude_code",
        "codex",
        "copilot",
        "deepseek",
        "grok",
        "openai",
        "openrouter",
    }
)
_WRITE_CACHE_MEASURABLE_PROVIDERS = frozenset({"anthropic", "claude_code", "openrouter"})


def _compute_read_cache_rate(
    prompt_tokens: int,
    cache_read_tokens: int,
    *,
    measurable: bool,
) -> float | None:
    if not measurable or prompt_tokens <= 0:
        return None
    return cache_read_tokens / prompt_tokens


def _is_read_cache_measurable(provider: str | None) -> bool:
    return provider in _READ_CACHE_MEASURABLE_PROVIDERS


def _is_write_cache_measurable(provider: str | None) -> bool:
    return provider in _WRITE_CACHE_MEASURABLE_PROVIDERS


def _aggregate_read_cache_measurable(
    rows: list[ResponseMetrics],
) -> bool:
    return bool(rows) and all(_is_read_cache_measurable(row.provider) for row in rows)


def _aggregate_write_cache_measurable(
    rows: list[ResponseMetrics],
) -> bool:
    return bool(rows) and all(_is_write_cache_measurable(row.provider) for row in rows)


def _aggregate_pricing_sources(rows: list[ResponseMetrics]) -> list[dict]:
    counts: dict[tuple[str, str | None, bool], int] = {}
    for row in rows:
        if row.pricing_source is None:
            continue
        key = (row.pricing_source, row.pricing_source_url, row.pricing_stale)
        counts[key] = counts.get(key, 0) + 1
    return [
        {
            "source": source,
            "source_url": source_url,
            "stale": stale,
            "count": count,
        }
        for (source, source_url, stale), count in sorted(counts.items())
    ]


def _has_stale_pricing(rows: list[ResponseMetrics]) -> bool:
    return any(row.pricing_stale for row in rows)


@dataclass
class ResponseMetrics:
    ts: datetime
    round: int | None
    provider: str | None
    model: str | None
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    latency_ms: int
    cost: float | None
    turn_id: str | None
    client_label: str | None = None
    pricing_source: str | None = None
    pricing_source_url: str | None = None
    pricing_stale: bool = False


@dataclass
class TurnMetrics:
    turn_id: str
    ts_started: datetime
    ts_finished: datetime
    channel: str
    sender: str | None
    status: str
    llm_rounds: int
    max_prompt_tokens: int | None
    total_prompt_tokens: int
    read_cache_rate: float | None
    cache_read_tokens: int
    cache_write_tokens: int
    write_cache_measurable: bool
    total_cost: float | None
    responses: list[ResponseMetrics] = field(default_factory=list)


@dataclass
class SessionSummary:
    session_id: str
    status: str
    created_at: datetime
    updated_at: datetime
    turn_count: int
    total_cost: float | None
    total_prompt_tokens: int
    read_cache_rate: float | None
    total_cache_read: int
    total_cache_write: int
    cache_hit_rate: float | None
    write_cache_measurable: bool
    peak_prompt_tokens: int
    pricing_sources: list[dict]
    pricing_stale: bool


@dataclass
class DashboardSummary:
    date_from: date
    date_to: date
    total_cost: float
    total_turns: int
    total_sessions: int
    total_prompt_tokens: int
    read_cache_rate: float | None
    total_cache_read: int
    total_cache_write: int
    cache_hit_rate: float | None
    write_cache_measurable: bool
    daily_costs: list[dict]  # [{date, cost, turns}]
    pricing_sources: list[dict]
    pricing_stale: bool


class MetricsCache:
    """Central in-memory cache refreshed incrementally from JSONL files."""

    def __init__(self, sessions_dir: Path, pricing: dict[str, ModelPricing]) -> None:
        self._sessions_dir = sessions_dir
        self.pricing = pricing
        self._files: dict[str, SessionFiles] = {}
        self._turns: dict[str, list[TurnMetrics]] = {}
        # responses indexed by (session_id, turn_id) for linking
        self._responses: dict[str, list[ResponseMetrics]] = {}

    def refresh_all(self) -> set[str]:
        """Discover new sessions and refresh all. Returns changed session IDs."""
        changed: set[str] = set()
        for sid in discover_sessions(self._sessions_dir):
            if self.refresh_session(sid):
                changed.add(sid)
        return changed

    def refresh_session(self, session_id: str) -> bool:
        """Read new data for one session. Returns True if anything changed."""
        session_dir = self._sessions_dir / session_id
        if not session_dir.is_dir():
            return False

        sf = self._files.get(session_id)
        if sf is None:
            sf = SessionFiles(session_dir=session_dir)
            self._files[session_id] = sf

        changed = False

        # Refresh meta if needed
        meta_path = session_dir / "meta.json"
        if meta_path.exists():
            mtime = meta_path.stat().st_mtime
            if mtime != sf.meta_mtime:
                sf.meta = read_meta(session_dir)
                sf.meta_mtime = mtime
                changed = True

        # Read new turns
        turns_path = session_dir / "turns.jsonl"
        new_turn_lines = read_new_lines(turns_path, sf.turns_state)
        if new_turn_lines:
            changed = True
            if session_id not in self._turns:
                self._turns[session_id] = []
            for raw in new_turn_lines:
                rec = parse_turn_record(raw)
                if rec is None:
                    continue
                tm = TurnMetrics(
                    turn_id=rec.turn_id,
                    ts_started=rec.ts_started,
                    ts_finished=rec.ts_finished,
                    channel=rec.channel,
                    sender=rec.sender,
                    status=rec.status,
                    llm_rounds=rec.llm_rounds,
                    max_prompt_tokens=rec.max_prompt_tokens,
                    total_prompt_tokens=0,
                    read_cache_rate=None,
                    cache_read_tokens=rec.cache_read_tokens,
                    cache_write_tokens=rec.cache_write_tokens,
                    write_cache_measurable=False,
                    total_cost=None,
                )
                self._turns[session_id].append(tm)

        # Read new responses
        resp_path = session_dir / "responses.jsonl"
        new_resp_lines = read_new_lines(resp_path, sf.responses_state)
        if new_resp_lines:
            changed = True
            if session_id not in self._responses:
                self._responses[session_id] = []
            for raw in new_resp_lines:
                rec = parse_response_record(raw)
                if rec is None:
                    continue
                resp = rec.response
                if resp is None:
                    continue
                pricing_meta = get_pricing_metadata(
                    rec.provider,
                    rec.model,
                    self.pricing,
                )
                cost = compute_request_cost(
                    provider=rec.provider,
                    model=rec.model,
                    prompt_tokens=resp.prompt_tokens or 0,
                    completion_tokens=resp.completion_tokens or 0,
                    cache_read_tokens=resp.cache_read_tokens,
                    cache_write_tokens=resp.cache_write_tokens,
                    pricing=self.pricing,
                )
                rm = ResponseMetrics(
                    ts=rec.ts,
                    round=rec.round,
                    provider=rec.provider,
                    model=rec.model,
                    prompt_tokens=resp.prompt_tokens or 0,
                    completion_tokens=resp.completion_tokens or 0,
                    cache_read_tokens=resp.cache_read_tokens,
                    cache_write_tokens=resp.cache_write_tokens,
                    latency_ms=rec.latency_ms,
                    cost=cost,
                    turn_id=rec.turn_id,
                    client_label=rec.client_label,
                    pricing_source=pricing_meta.source if pricing_meta else None,
                    pricing_source_url=(
                        pricing_meta.source_url if pricing_meta else None
                    ),
                    pricing_stale=pricing_meta.stale if pricing_meta else False,
                )
                self._responses[session_id].append(rm)

        # Link responses to turns and compute turn costs
        if changed and session_id in self._turns:
            resp_by_turn: dict[str, list[ResponseMetrics]] = {}
            for rm in self._responses.get(session_id, []):
                if rm.turn_id:
                    resp_by_turn.setdefault(rm.turn_id, []).append(rm)
            for tm in self._turns[session_id]:
                linked = resp_by_turn.get(tm.turn_id, [])
                tm.responses = linked
                costs = [r.cost for r in linked if r.cost is not None]
                tm.total_cost = sum(costs) if costs else None
                tm.total_prompt_tokens = sum(r.prompt_tokens for r in linked)
                tm.read_cache_rate = _compute_read_cache_rate(
                    tm.total_prompt_tokens,
                    tm.cache_read_tokens,
                    measurable=_aggregate_read_cache_measurable(linked),
                )
                tm.write_cache_measurable = _aggregate_write_cache_measurable(linked)

        return changed

    def get_session_summary(self, session_id: str) -> SessionSummary | None:
        sf = self._files.get(session_id)
        if sf is None or sf.meta is None:
            return None
        turns = self._turns.get(session_id, [])
        responses = self._responses.get(session_id, [])
        total_cr = sum(r.cache_read_tokens for r in responses)
        total_cw = sum(r.cache_write_tokens for r in responses)
        total_prompt = sum(r.prompt_tokens for r in responses)
        costs = [t.total_cost for t in turns if t.total_cost is not None]
        peak = max(
            [t.max_prompt_tokens or 0 for t in turns]
            + [r.prompt_tokens for r in responses],
            default=0,
        )
        hit_rate = total_cr / (total_cr + total_cw) if (total_cr + total_cw) > 0 else None
        read_cache_measurable = _aggregate_read_cache_measurable(responses)
        return SessionSummary(
            session_id=session_id,
            status=sf.meta.status,
            created_at=sf.meta.created_at,
            updated_at=sf.meta.updated_at,
            turn_count=len(turns),
            total_cost=sum(costs) if costs else None,
            total_prompt_tokens=total_prompt,
            read_cache_rate=_compute_read_cache_rate(
                total_prompt,
                total_cr,
                measurable=read_cache_measurable,
            ),
            total_cache_read=total_cr,
            total_cache_write=total_cw,
            cache_hit_rate=hit_rate,
            write_cache_measurable=_aggregate_write_cache_measurable(responses),
            peak_prompt_tokens=peak,
            pricing_sources=_aggregate_pricing_sources(responses),
            pricing_stale=_has_stale_pricing(responses),
        )

    def _date_in_range(self, value: date, date_from: date, date_to: date) -> bool:
        return date_from <= value <= date_to

    def _session_activity_in_range(
        self, session_id: str, date_from: date, date_to: date
    ) -> bool:
        """True when the session had turn/response activity (or was created) in range.

        Cross-midnight sessions must still appear on days they are active, not
        only on the create day.
        """
        for rm in self._responses.get(session_id, []):
            if self._date_in_range(rm.ts.date(), date_from, date_to):
                return True
        for tm in self._turns.get(session_id, []):
            if self._date_in_range(tm.ts_started.date(), date_from, date_to):
                return True
        sf = self._files.get(session_id)
        if sf is not None and sf.meta is not None:
            if self._date_in_range(sf.meta.created_at.date(), date_from, date_to):
                return True
        return False

    def get_sessions_in_range(
        self, date_from: date, date_to: date
    ) -> list[SessionSummary]:
        results: list[SessionSummary] = []
        for sid, sf in self._files.items():
            if sf.meta is None:
                continue
            if not self._session_activity_in_range(sid, date_from, date_to):
                continue
            summary = self.get_session_summary(sid)
            if summary:
                results.append(summary)
        # Most recently active first so the current multi-day session is on top.
        results.sort(key=lambda s: s.updated_at, reverse=True)
        return results

    def get_dashboard(self, date_from: date, date_to: date) -> DashboardSummary:
        sessions = self.get_sessions_in_range(date_from, date_to)
        all_responses: list[ResponseMetrics] = []
        total_cost = 0.0
        total_turns = 0
        total_prompt = 0
        total_cr = 0
        total_cw = 0
        daily: dict[date, dict] = {}

        def _daily_bucket(day: date) -> dict:
            if day not in daily:
                daily[day] = {
                    "date": day.isoformat(),
                    "cost": 0.0,
                    "turns": 0,
                    "prompt_tokens": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "read_cache_measurable": True,
                    "write_cache_measurable": True,
                    "pricing_stale": False,
                    "_responses": [],
                }
            return daily[day]

        for s in sessions:
            # Count only turns/responses that actually fell inside the window.
            for tm in self._turns.get(s.session_id, []):
                day = tm.ts_started.date()
                if not self._date_in_range(day, date_from, date_to):
                    continue
                total_turns += 1
                bucket = _daily_bucket(day)
                bucket["turns"] += 1

            for rm in self._responses.get(s.session_id, []):
                day = rm.ts.date()
                if not self._date_in_range(day, date_from, date_to):
                    continue
                all_responses.append(rm)
                total_prompt += rm.prompt_tokens
                total_cr += rm.cache_read_tokens
                total_cw += rm.cache_write_tokens
                if rm.cost is not None:
                    total_cost += rm.cost
                bucket = _daily_bucket(day)
                bucket["prompt_tokens"] += rm.prompt_tokens
                bucket["cache_read"] += rm.cache_read_tokens
                bucket["cache_write"] += rm.cache_write_tokens
                if rm.cost is not None:
                    bucket["cost"] += rm.cost
                bucket["_responses"].append(rm)
                bucket["pricing_stale"] = bucket["pricing_stale"] or rm.pricing_stale

        for bucket in daily.values():
            rows: list[ResponseMetrics] = bucket.pop("_responses")
            bucket["read_cache_measurable"] = _aggregate_read_cache_measurable(rows)
            bucket["write_cache_measurable"] = _aggregate_write_cache_measurable(rows)

        hit_rate = total_cr / (total_cr + total_cw) if (total_cr + total_cw) > 0 else None

        daily_list = sorted(daily.values(), key=lambda x: x["date"])
        for row in daily_list:
            row["read_cache_rate"] = _compute_read_cache_rate(
                row["prompt_tokens"],
                row["cache_read"],
                measurable=row["read_cache_measurable"],
            )

        dashboard_read_cache_measurable = _aggregate_read_cache_measurable(all_responses)

        return DashboardSummary(
            date_from=date_from,
            date_to=date_to,
            total_cost=total_cost,
            total_turns=total_turns,
            total_sessions=len(sessions),
            total_prompt_tokens=total_prompt,
            read_cache_rate=_compute_read_cache_rate(
                total_prompt,
                total_cr,
                measurable=dashboard_read_cache_measurable,
            ),
            total_cache_read=total_cr,
            total_cache_write=total_cw,
            cache_hit_rate=hit_rate,
            write_cache_measurable=_aggregate_write_cache_measurable(all_responses),
            daily_costs=daily_list,
            pricing_sources=_aggregate_pricing_sources(all_responses),
            pricing_stale=_has_stale_pricing(all_responses),
        )

    def get_session_detail(self, session_id: str) -> dict | None:
        sf = self._files.get(session_id)
        if sf is None or sf.meta is None:
            return None
        summary = self.get_session_summary(session_id)
        if summary is None:
            return None
        turns = self._turns.get(session_id, [])
        return {
            "session_id": session_id,
            "meta": {
                "status": sf.meta.status,
                "created_at": sf.meta.created_at.isoformat(),
                "updated_at": sf.meta.updated_at.isoformat(),
            },
            "summary": {
                "total_cost": summary.total_cost,
                "turn_count": summary.turn_count,
                "read_cache_rate": summary.read_cache_rate,
                "cache_hit_rate": summary.cache_hit_rate,
                "peak_prompt_tokens": summary.peak_prompt_tokens,
                "total_cache_read": summary.total_cache_read,
                "total_cache_write": summary.total_cache_write,
                "write_cache_measurable": summary.write_cache_measurable,
                "pricing_sources": summary.pricing_sources,
                "pricing_stale": summary.pricing_stale,
            },
            "turns": [_serialize_turn(t) for t in turns],
        }

    def get_all_requests(
        self, date_from: date, date_to: date
    ) -> list[dict]:
        """Return response records by response timestamp, newest first.

        Filtering is on each request's own ``ts`` (not session create day), so a
        multi-day session still contributes its recent Grok/Claude calls when the
        UI range is "today" or "7d". Newest-first matches the monitor page which
        only loads the first page (limit 500).
        """
        results: list[dict] = []
        for sid, sf in self._files.items():
            if sf.meta is None:
                continue
            session_label = sf.meta.created_at.strftime("%m/%d %H:%M")
            for rm in self._responses.get(sid, []):
                if not self._date_in_range(rm.ts.date(), date_from, date_to):
                    continue
                results.append({
                    "ts": rm.ts.isoformat(),
                    "session_id": sid,
                    "session_label": session_label,
                    "turn_id": rm.turn_id,
                    "round": rm.round,
                    "provider": rm.provider,
                    "model": rm.model,
                    "prompt_tokens": rm.prompt_tokens,
                    "completion_tokens": rm.completion_tokens,
                    "read_cache_rate": _compute_read_cache_rate(
                        rm.prompt_tokens,
                        rm.cache_read_tokens,
                        measurable=_is_read_cache_measurable(rm.provider),
                    ),
                    "cache_read_tokens": rm.cache_read_tokens,
                    "cache_write_tokens": rm.cache_write_tokens,
                    "write_cache_measurable": _is_write_cache_measurable(rm.provider),
                    "latency_ms": rm.latency_ms,
                    "cost": rm.cost,
                    "client_label": rm.client_label,
                    "pricing_source": rm.pricing_source,
                    "pricing_source_url": rm.pricing_source_url,
                    "pricing_stale": rm.pricing_stale,
                })
        results.sort(key=lambda r: r["ts"], reverse=True)
        return results

    def get_live_status(self, soft_limit: int) -> dict | None:
        """Return brain token position for the most recent active session."""
        active: SessionFiles | None = None
        for sf in self._files.values():
            if sf.meta is None or sf.meta.status != "active":
                continue
            if active is None or sf.meta.updated_at > active.meta.updated_at:
                active = sf
        if active is None or active.meta is None:
            return None

        sid = active.meta.session_id
        responses = self._responses.get(sid, [])
        brain_responses: list[ResponseMetrics] = []
        seen_turn_ids: set[str] = set()
        for response in reversed(responses):
            if response.turn_id is None or response.turn_id in seen_turn_ids:
                continue
            seen_turn_ids.add(response.turn_id)
            brain_responses = [
                candidate
                for candidate in responses
                if candidate.turn_id == response.turn_id
                and candidate.client_label == "brain"
            ]
            if brain_responses:
                break

        last_prompt = max(
            (response.prompt_tokens for response in brain_responses),
            default=0,
        )

        # Worker responses use separate contexts and must not change the brain bar.
        hard_limit = 200_000
        if brain_responses:
            from .pricing import resolve_model_key

            last_brain_response = brain_responses[-1]
            model_key = resolve_model_key(
                last_brain_response.provider,
                last_brain_response.model,
            )
            if model_key and model_key in self.pricing:
                model_pricing = self.pricing[model_key]
                if model_pricing.max_input_tokens:
                    hard_limit = model_pricing.max_input_tokens

        return {
            "active": True,
            "session_id": sid,
            "prompt_tokens": last_prompt,
            "soft_limit": soft_limit,
            "hard_limit": hard_limit,
        }


def _serialize_turn(t: TurnMetrics) -> dict:
    return {
        "turn_id": t.turn_id,
        "ts_started": t.ts_started.isoformat(),
        "ts_finished": t.ts_finished.isoformat(),
        "channel": t.channel,
        "sender": t.sender,
        "status": t.status,
        "llm_rounds": t.llm_rounds,
        "max_prompt_tokens": t.max_prompt_tokens,
        "total_prompt_tokens": t.total_prompt_tokens,
        "read_cache_rate": t.read_cache_rate,
        "cache_read_tokens": t.cache_read_tokens,
        "cache_write_tokens": t.cache_write_tokens,
        "write_cache_measurable": t.write_cache_measurable,
        "total_cost": t.total_cost,
        "pricing_sources": _aggregate_pricing_sources(t.responses),
        "pricing_stale": _has_stale_pricing(t.responses),
        "responses": [
            {
                "round": r.round,
                "provider": r.provider,
                "model": r.model,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "read_cache_rate": _compute_read_cache_rate(
                    r.prompt_tokens,
                    r.cache_read_tokens,
                    measurable=_is_read_cache_measurable(r.provider),
                ),
                "cache_read_tokens": r.cache_read_tokens,
                "cache_write_tokens": r.cache_write_tokens,
                "write_cache_measurable": _is_write_cache_measurable(r.provider),
                "latency_ms": r.latency_ms,
                "cost": r.cost,
                "client_label": r.client_label,
                "pricing_source": r.pricing_source,
                "pricing_source_url": r.pricing_source_url,
                "pricing_stale": r.pricing_stale,
            }
            for r in t.responses
        ],
    }
