"""Upstream Anthropic transport for the native Claude Code proxy."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
import json
import secrets
from typing import Any

import anyio
import anyio.to_thread
import httpx
from pydantic import BaseModel, ConfigDict

from lincy.llm.http_error import format_http_error
from lincy.llm.schema import ClaudeCodeRequest

from .auth import (
    DEFAULT_CLAUDE_CODE_OAUTH_TOKEN_URL,
    ClaudeCodeBrowserAuthorization,
    ClaudeCodeOAuthClient,
    StoredClaudeCodeToken,
    StoredClaudeCodeTokenStore,
    is_token_fresh,
    normalize_bearer_token,
)
from .settings import ClaudeCodeProxySettings

EFFORT_BETA_HEADER = "effort-2025-11-24"

# Reverse-engineered OAuth account endpoints used by the Claude Code CLI for its
# /usage display. Documented in docs/dev/provider-api-spec.md.
OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
OAUTH_BETA_HEADER = "oauth-2025-04-20"

# Usage snapshots hit two upstream endpoints per stored token; cache aligned
# with the dashboard's 60s polling so each viewer triggers at most one sweep.
USAGE_CACHE_TTL_SECONDS = 60.0

# Upstream status codes that should trigger failover to the next token rather than
# surfacing the error to the client. Claude Code quota exhaustion may surface as
# 429, while hard auth / entitlement failures commonly surface as 401/403.
FAILOVER_STATUS_CODES = frozenset({401, 403, 429})

# How long a token stays benched after an upstream auth/quota failure. A cooldown
# (rather than a permanent bench) lets a token that failed on a transient 401/403
# rejoin the failover pool automatically, so a single blip cannot shrink the pool
# until restart. Only FAILOVER_STATUS_CODES bench: a timeout says nothing about the
# credential, so benching on it would empty the pool over a slow generation.
FAILURE_COOLDOWN_SECONDS = 300.0

# Sentinel token id used when --access-token / env bypasses the OAuth token store.
ENV_ACCESS_TOKEN_ID = "__env_access_token__"

# A pending web login holds PKCE state in memory; abandoned flows expire so the
# dict cannot grow unbounded.
LOGIN_STATE_TTL_SECONDS = 900.0


class ClaudeCodeUpstreamError(RuntimeError):
    """Wrap upstream HTTP errors while preserving raw response payload."""

    def __init__(self, *, status_code: int, body: bytes, media_type: str):
        super().__init__(body.decode("utf-8", errors="replace"))
        self.status_code = status_code
        self.body = body
        self.media_type = media_type


class ClaudeCodeUpstreamTimeoutError(RuntimeError):
    """Every candidate token hit a transport timeout reaching upstream."""


class ClaudeCodeTokenUnavailableError(RuntimeError):
    """No usable OAuth token is available to serve the request.

    ``retry_after_seconds`` is set when the pool is only temporarily empty (every
    token benched), so the caller can wait instead of treating it as a credential
    problem.
    """

    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _now() -> float:
    return datetime.now(tz=UTC).timestamp()


def _is_usage_auth_failure(exc: BaseException) -> bool:
    """True when a usage fetch failed because the credential is gone/invalid.

    Missing or invalidated auth is not an operational error for the dashboard:
    it means there is no usable account, not that usage reporting broke.
    """

    text = str(exc)
    return "HTTP 401" in text or "HTTP 403" in text


class _TolerantModel(BaseModel):
    """Parse reverse-engineered payloads without breaking on unknown fields."""

    model_config = ConfigDict(extra="ignore")


class OAuthUsageWindow(_TolerantModel):
    utilization: float | None = None
    resets_at: str | None = None


class _OAuthLimitScopeModel(_TolerantModel):
    id: str | None = None
    display_name: str | None = None


class _OAuthLimitScope(_TolerantModel):
    # Overrides the base config: this class has a field named `model`, which
    # collides with pydantic's default protected namespace ("model_").
    model_config = ConfigDict(extra="ignore", protected_namespaces=())

    model: _OAuthLimitScopeModel | None = None


class OAuthUsageLimit(_TolerantModel):
    kind: str | None = None
    percent: float | None = None
    resets_at: str | None = None
    scope: _OAuthLimitScope | None = None


class OAuthUsagePayload(_TolerantModel):
    five_hour: OAuthUsageWindow | None = None
    seven_day: OAuthUsageWindow | None = None
    limits: list[OAuthUsageLimit] | None = None


class _OAuthProfileAccount(_TolerantModel):
    email: str | None = None
    display_name: str | None = None


class _OAuthProfileOrganization(_TolerantModel):
    organization_type: str | None = None
    rate_limit_tier: str | None = None


class OAuthProfilePayload(_TolerantModel):
    account: _OAuthProfileAccount | None = None
    organization: _OAuthProfileOrganization | None = None


@dataclass(frozen=True)
class PoolEntry:
    """One credential in the failover pool, resolved for usage reporting."""

    token_id: str
    source: str
    access_token: str | None
    benched: bool
    error: str | None


class ClaudeCodeTokenManager:
    """Load, cache, and refresh Claude Code OAuth tokens (multi-token failover)."""

    def __init__(self, settings: ClaudeCodeProxySettings):
        self._settings = settings
        self._store = StoredClaudeCodeTokenStore()
        self._lock = anyio.Lock()
        # token id -> epoch seconds until which the token stays benched.
        self._failed_until: dict[str, float] = {}

    async def acquire(self, *, exclude: Collection[str] = ()) -> tuple[str, str]:
        """Return (access_token, token_id) for the highest-priority usable token.

        ``exclude`` holds tokens the caller already tried within this one request;
        they are skipped without benching, which keeps a per-request retry loop
        moving without shrinking the pool for everyone else.

        Raises RuntimeError if no token is usable (credentials import is removed, so
        the only sources are --access-token / env and the OAuth token store).
        """

        if self._settings.access_token:
            return normalize_bearer_token(self._settings.access_token), ENV_ACCESS_TOKEN_ID

        async with self._lock:
            return await self._select_usable_token(exclude)

    def mark_failed(self, token_id: str) -> None:
        """Bench a token for FAILURE_COOLDOWN_SECONDS so acquire() skips it meanwhile."""

        if token_id == ENV_ACCESS_TOKEN_ID:
            return
        self._failed_until[token_id] = _now() + FAILURE_COOLDOWN_SECONDS

    def promote(self, token_id: str) -> bool:
        return self._store.promote(token_id)

    def remove(self, token_id: str) -> bool:
        removed = self._store.remove(token_id)
        if removed:
            self._failed_until.pop(token_id, None)
        return removed

    def store_token(self, token: StoredClaudeCodeToken) -> None:
        self._store.save(token)

    async def pool_entries(self) -> list[PoolEntry]:
        """Snapshot every credential in priority order for usage reporting.

        Unlike acquire(), benched tokens are included (their utilization is
        usually the reason they are benched) and stale tokens are refreshed so
        the account endpoints can still be queried.
        """

        if self._settings.access_token:
            return [
                PoolEntry(
                    token_id=ENV_ACCESS_TOKEN_ID,
                    source="env_override",
                    access_token=normalize_bearer_token(self._settings.access_token),
                    benched=False,
                    error=None,
                )
            ]

        entries: list[PoolEntry] = []
        async with self._lock:
            for token in self._store.load_all() or []:
                benched = self._is_benched(token.id)
                if is_token_fresh(token):
                    entries.append(
                        PoolEntry(token.id, token.source, token.access_token, benched, None)
                    )
                    continue
                if token.refresh_token:
                    try:
                        refreshed = await self._refresh(token)
                        self._store.save(refreshed)
                        entries.append(
                            PoolEntry(
                                token.id, token.source, refreshed.access_token, benched, None
                            )
                        )
                    except Exception as exc:
                        entries.append(
                            PoolEntry(
                                token.id, token.source, None, benched, f"refresh failed: {exc}"
                            )
                        )
                    continue
                entries.append(
                    PoolEntry(
                        token.id,
                        token.source,
                        None,
                        benched,
                        "token expired and has no refresh token",
                    )
                )
        return entries

    def _is_benched(self, token_id: str) -> bool:
        return self._failed_until.get(token_id, 0.0) > _now()

    async def _select_usable_token(self, exclude: Collection[str] = ()) -> tuple[str, str]:
        errors: list[str] = []
        bench_deadlines: list[float] = []
        excluded = 0
        tokens = self._store.load_all() or []

        for token in tokens:
            if token.id in exclude:
                excluded += 1
                continue
            if self._is_benched(token.id):
                bench_deadlines.append(self._failed_until[token.id])
                continue
            if is_token_fresh(token):
                return token.access_token, token.id
            if token.refresh_token:
                try:
                    refreshed = await self._refresh(token)
                    self._store.save(refreshed)
                    return refreshed.access_token, refreshed.id
                except Exception as exc:
                    errors.append(f"refresh failed for {token.id}: {exc}")
                    # Skip this token and try the next rather than aborting.
                    continue
            # Stale with no refresh token is a real credential problem, not a bench:
            # re-evaluating it costs nothing, and reporting it every pass keeps the
            # bench state meaning "upstream failure" only.
            errors.append(f"{token.id} is expired and has no refresh token")

        if not tokens:
            raise ClaudeCodeTokenUnavailableError(
                "No Claude Code OAuth tokens stored. Run `uv run proxy claude-code login`."
            )
        retry_after = max(0.0, min(bench_deadlines) - _now()) if bench_deadlines else None
        if bench_deadlines and not errors:
            # Nothing is wrong with the credentials; they are cooling down. Saying
            # "token is required" here sends operators after the token store instead
            # of the upstream failures that actually benched the pool.
            raise ClaudeCodeTokenUnavailableError(
                f"All {len(bench_deadlines)} stored Claude Code token(s) are benched after "
                f"upstream auth/quota failures; retry in {retry_after:.0f}s. "
                "See GET /usage for the per-token error.",
                retry_after_seconds=retry_after,
            )
        detail = "; ".join(errors) if errors else f"{excluded} token(s) already tried this request"
        if bench_deadlines:
            detail = f"{detail}; {len(bench_deadlines)} benched for another {retry_after:.0f}s"
        raise ClaudeCodeTokenUnavailableError(
            "Claude Code token is required. Set --access-token / "
            f"CLAUDE_CODE_PROXY_ACCESS_TOKEN, or run `uv run proxy claude-code login`. ({detail})",
            retry_after_seconds=retry_after,
        )

    async def _refresh(self, token: StoredClaudeCodeToken) -> StoredClaudeCodeToken:
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
            "client_id": self._settings.oauth_client_id,
        }
        async with httpx.AsyncClient(timeout=self._settings.request_timeout) as client:
            response = await client.post(
                DEFAULT_CLAUDE_CODE_OAUTH_TOKEN_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"OAuth refresh failed with status {response.status_code}: {response.text}"
            )
        data = response.json()
        access_token = data.get("access_token")
        expires_in = data.get("expires_in")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("OAuth refresh returned no access_token")
        if not isinstance(expires_in, (int, float)) or expires_in <= 0:
            raise RuntimeError("OAuth refresh returned invalid expires_in")
        next_refresh_token = data.get("refresh_token")
        if next_refresh_token is not None and not isinstance(next_refresh_token, str):
            raise RuntimeError("OAuth refresh returned invalid refresh_token")
        now = datetime.now(tz=UTC)
        return StoredClaudeCodeToken(
            id=token.id,
            access_token=access_token,
            refresh_token=next_refresh_token or token.refresh_token,
            expires_at=now + timedelta(seconds=float(expires_in)),
            source="oauth_refresh",
            client_id=self._settings.oauth_client_id,
            created_at=token.created_at,
        )


class ClaudeCodeProxyService:
    """Translate local Claude Code requests into upstream Anthropic calls."""

    def __init__(self, settings: ClaudeCodeProxySettings):
        self._settings = settings
        self._tokens = ClaudeCodeTokenManager(settings)
        self._usage_cache: tuple[float, dict[str, Any]] | None = None
        self._usage_lock = anyio.Lock()
        # token id -> last successful (account, usage), served with stale=True
        # when a later fetch fails (the OAuth endpoints rate-limit under load).
        self._last_good_usage: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self._oauth = ClaudeCodeOAuthClient(
            request_timeout=settings.request_timeout,
            client_id=settings.oauth_client_id,
            scope=settings.oauth_scope,
        )
        # login id -> (expiry epoch seconds, PKCE/state for the pending flow)
        self._pending_logins: dict[str, tuple[float, ClaudeCodeBrowserAuthorization]] = {}

    def invalidate_usage_cache(self) -> None:
        """Drop the usage snapshot so the next read reflects a token-store edit."""

        self._usage_cache = None

    def promote_token(self, token_id: str) -> bool:
        promoted = self._tokens.promote(token_id)
        if promoted:
            self.invalidate_usage_cache()
        return promoted

    def remove_token(self, token_id: str) -> bool:
        removed = self._tokens.remove(token_id)
        if removed:
            self.invalidate_usage_cache()
        return removed

    def begin_login(self) -> dict[str, str]:
        """Start a browser OAuth flow for web-driven account adds."""

        self._prune_pending_logins()
        authorization = self._oauth.begin_authorization()
        login_id = secrets.token_urlsafe(8)
        self._pending_logins[login_id] = (
            _now() + LOGIN_STATE_TTL_SECONDS,
            authorization,
        )
        return {"login_id": login_id, "authorization_url": authorization.authorization_url}

    async def complete_login(self, login_id: str, manual_code: str) -> StoredClaudeCodeToken | None:
        """Exchange the pasted `code#state` for a token and append it to the store.

        Returns None when the login id is unknown or expired. State-mismatch and
        exchange failures propagate as ValueError / RuntimeError.
        """

        self._prune_pending_logins()
        pending = self._pending_logins.get(login_id)
        if pending is None:
            return None
        _, authorization = pending
        # The OAuth client is sync httpx; keep the event loop free for streams.
        token = await anyio.to_thread.run_sync(
            partial(self._oauth.exchange_manual_code, manual_code, authorization=authorization)
        )
        self._tokens.store_token(token)
        self._pending_logins.pop(login_id, None)
        self.invalidate_usage_cache()
        return token

    def _prune_pending_logins(self) -> None:
        now = _now()
        expired = [key for key, (deadline, _) in self._pending_logins.items() if deadline <= now]
        for key in expired:
            del self._pending_logins[key]

    async def usage_snapshot(self, force_refresh: bool = False) -> dict[str, Any]:
        """Report account identity, 5h/7d utilization, and models per pool token.

        One failing account must not break the snapshot: transient fetch
        errors are reported per entry. Auth failures (401/403) mean the
        credential is gone — env override is omitted (no account, not an
        error), and store tokens stay as ``unusable`` without an error
        string so the dashboard can show empty/neutral rather than a red
        failure. The whole snapshot is cached briefly (the lock also
        collapses concurrent dashboard refreshes into one upstream sweep);
        force_refresh bypasses the cache read for the dashboard's manual
        refresh button, but still stores the fresh result.
        """

        async with self._usage_lock:
            if self._usage_cache is not None and not force_refresh:
                fetched_at, cached = self._usage_cache
                if _now() - fetched_at < USAGE_CACHE_TTL_SECONDS:
                    return cached

            entries = await self._tokens.pool_entries()
            accounts: list[dict[str, Any]] = []
            models: list[dict[str, Any]] = []
            active_token: str | None = None
            async with httpx.AsyncClient(timeout=self._settings.request_timeout) as client:
                for entry in entries:
                    item: dict[str, Any] = {
                        "id": entry.token_id,
                        "source": entry.source,
                        "priority": len(accounts),
                        "status": "unusable",
                        "error": entry.error,
                        "account": None,
                        "usage": None,
                        "stale": False,
                    }
                    if entry.access_token is None:
                        # Env override is implicit: unusable means absent.
                        if entry.source == "env_override":
                            continue
                        accounts.append(item)
                        continue

                    try:
                        account, usage = await self._fetch_account_usage(
                            client, entry.access_token
                        )
                    except Exception as exc:
                        if _is_usage_auth_failure(exc):
                            # Signed-out / invalidated credential: no account,
                            # not a usage-endpoint failure. Drop env override;
                            # keep store tokens so the user can remove/re-login.
                            if entry.source == "env_override":
                                continue
                            item["error"] = None
                            accounts.append(item)
                            continue
                        item["error"] = f"usage fetch failed: {exc}"
                        last_good = self._last_good_usage.get(entry.token_id)
                        if last_good is not None:
                            item["account"], item["usage"] = last_good
                            item["stale"] = True
                            if entry.benched:
                                item["status"] = "benched"
                            elif active_token is None:
                                item["status"] = "active"
                                active_token = entry.access_token
                            else:
                                item["status"] = "standby"
                        accounts.append(item)
                        continue

                    item["account"] = account
                    item["usage"] = usage
                    self._last_good_usage[entry.token_id] = (account, usage)
                    if entry.benched:
                        item["status"] = "benched"
                    elif active_token is None:
                        item["status"] = "active"
                        active_token = entry.access_token
                    else:
                        item["status"] = "standby"
                    accounts.append(item)

            if active_token is not None:
                models = await self._models_for_snapshot()

            payload = {"accounts": accounts, "models": models}
            self._usage_cache = (_now(), payload)
            return payload

    async def _models_for_snapshot(self) -> list[dict[str, Any]]:
        """Best-effort model list via the standard passthrough; failures yield []."""

        try:
            body, _ = await self.forward_models("limit=100")
            payload = json.loads(body)
        except Exception:
            # Model list is decoration; account usage stays useful without it.
            return []
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return []
        return [
            {"id": model.get("id"), "display_name": model.get("display_name")}
            for model in data
            if isinstance(model, dict) and model.get("id")
        ]

    def _oauth_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA_HEADER,
            "User-Agent": self._settings.user_agent,
        }

    async def _fetch_account_usage(
        self,
        client: httpx.AsyncClient,
        token: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        headers = self._oauth_headers(token)
        profile_resp = await client.get(OAUTH_PROFILE_URL, headers=headers)
        usage_resp = await client.get(OAUTH_USAGE_URL, headers=headers)
        for response in (profile_resp, usage_resp):
            if response.status_code >= 400:
                raise RuntimeError(format_http_error(response.status_code, response.text))
        profile = OAuthProfilePayload.model_validate(profile_resp.json())
        usage = OAuthUsagePayload.model_validate(usage_resp.json())
        account = {
            "email": profile.account.email if profile.account else None,
            "display_name": profile.account.display_name if profile.account else None,
            "plan_type": (
                profile.organization.organization_type if profile.organization else None
            ),
            "rate_limit_tier": (
                profile.organization.rate_limit_tier if profile.organization else None
            ),
        }
        usage_out = {
            "five_hour": usage.five_hour.model_dump() if usage.five_hour else None,
            "seven_day": usage.seven_day.model_dump() if usage.seven_day else None,
            # Model-scoped weekly quotas (Fable/Opus) only show up in limits[] as
            # kind="weekly_scoped" entries; the legacy per-model fields are null now.
            "seven_day_scoped": [
                {
                    "label": limit.scope.model.display_name,
                    "utilization": limit.percent,
                    "resets_at": limit.resets_at,
                }
                for limit in (usage.limits or [])
                if (
                    limit.kind == "weekly_scoped"
                    and limit.scope is not None
                    and limit.scope.model is not None
                    and limit.scope.model.display_name
                )
            ],
        }
        return account, usage_out


    async def forward_json(
        self,
        request: ClaudeCodeRequest,
        client_betas: str | None = None,
    ) -> tuple[bytes, str, dict[str, str]]:
        payload = self._build_upstream_request(request)
        last_upstream: ClaudeCodeUpstreamError | None = None
        last_timeout: ClaudeCodeUpstreamTimeoutError | None = None
        # Tokens that timed out within this request. They are skipped here instead of
        # benched, because a timeout is a property of the request, not the credential.
        timed_out: set[str] = set()
        while True:
            try:
                token, token_id = await self._tokens.acquire(exclude=timed_out)
            except ClaudeCodeTokenUnavailableError:
                # Every token was benched on upstream auth failures: surface the real
                # upstream error rather than an opaque "no token available" 503.
                if last_upstream is not None:
                    raise last_upstream from None
                if last_timeout is not None:
                    raise last_timeout from None
                raise
            try:
                async with httpx.AsyncClient(
                    # A non-streaming generation on a large prompt stays silent for
                    # minutes; a read timeout here kills a legitimate response and
                    # the client cannot tell that from a real outage. The caller's own
                    # request timeout is the intended bound, so leave read unbounded
                    # and only cap connect/write/pool -- same policy as open_stream().
                    timeout=httpx.Timeout(self._settings.request_timeout, read=None)
                ) as client:
                    response = await client.post(
                        f"{self._settings.anthropic_base_url}/v1/messages",
                        headers=self._headers(token, request, client_betas),
                        json=payload,
                    )
            except httpx.TimeoutException as exc:
                if self._should_failover_timeout(token_id):
                    timed_out.add(token_id)
                    last_timeout = ClaudeCodeUpstreamTimeoutError(
                        f"Claude Code upstream timed out for token {token_id}"
                    )
                    continue
                raise exc
            if response.status_code < 400:
                return (
                    response.content,
                    response.headers.get("content-type", "application/json"),
                    self.passthrough_headers(response.headers),
                )
            error = ClaudeCodeUpstreamError(
                status_code=response.status_code,
                body=response.content,
                media_type=response.headers.get("content-type", "application/json"),
            )
            if self._should_failover(token_id, response.status_code):
                self._tokens.mark_failed(token_id)
                last_upstream = error
                continue
            raise error

    async def forward_models(self, query: str = "") -> tuple[bytes, str]:
        """Forward GET /v1/models upstream with the same token failover as messages."""

        url = f"{self._settings.anthropic_base_url}/v1/models"
        if query:
            url = f"{url}?{query}"
        last_upstream: ClaudeCodeUpstreamError | None = None
        while True:
            try:
                token, token_id = await self._tokens.acquire()
            except ClaudeCodeTokenUnavailableError:
                if last_upstream is not None:
                    raise last_upstream from None
                raise
            headers = {
                **self._oauth_headers(token),
                "anthropic-version": self._settings.anthropic_version,
            }
            async with httpx.AsyncClient(timeout=self._settings.request_timeout) as client:
                response = await client.get(url, headers=headers)
            if response.status_code < 400:
                return response.content, response.headers.get(
                    "content-type", "application/json"
                )
            error = ClaudeCodeUpstreamError(
                status_code=response.status_code,
                body=response.content,
                media_type=response.headers.get("content-type", "application/json"),
            )
            if self._should_failover(token_id, response.status_code):
                self._tokens.mark_failed(token_id)
                last_upstream = error
                continue
            raise error

    async def open_stream(
        self,
        request: ClaudeCodeRequest,
        client_betas: str | None = None,
    ) -> tuple[httpx.AsyncClient, httpx.Response]:
        payload = self._build_upstream_request(request)
        last_upstream: ClaudeCodeUpstreamError | None = None
        while True:
            try:
                token, token_id = await self._tokens.acquire()
            except ClaudeCodeTokenUnavailableError:
                if last_upstream is not None:
                    raise last_upstream from None
                raise
            client = httpx.AsyncClient(timeout=self._settings.request_timeout)
            try:
                upstream_request = client.build_request(
                    "POST",
                    f"{self._settings.anthropic_base_url}/v1/messages",
                    headers={
                        **self._headers(token, request, client_betas),
                        # aiter_raw() relays bytes without decoding, so a
                        # compressed upstream stream would reach the client as
                        # unlabeled gzip garbage. SSE gains nothing from
                        # compression; force identity for a clean passthrough.
                        "Accept-Encoding": "identity",
                    },
                    json=payload,
                    # Huge prompts keep the SSE stream silent for minutes during
                    # prompt processing; a read timeout here kills legitimate
                    # long-running streams (surfacing as Cloudflare 524 behind
                    # the tunnel). Connect/write/pool stay bounded.
                    timeout=httpx.Timeout(self._settings.request_timeout, read=None),
                )
                response = await client.send(upstream_request, stream=True)
            except Exception:
                await client.aclose()
                raise
            if response.status_code < 400:
                return client, response
            body = await response.aread()
            await response.aclose()
            await client.aclose()
            error = ClaudeCodeUpstreamError(
                status_code=response.status_code,
                body=body,
                media_type=response.headers.get("content-type", "application/json"),
            )
            if self._should_failover(token_id, response.status_code):
                self._tokens.mark_failed(token_id)
                last_upstream = error
                continue
            raise error

    @staticmethod
    def _should_failover(token_id: str, status_code: int) -> bool:
        """Whether to bench this token and retry with the next one.

        The env/--access-token bypass has no alternate token, so it never fails over.
        Whether another usable token actually exists is decided by the next acquire()
        (which raises ClaudeCodeTokenUnavailableError when none remains) -- this avoids
        a lock-free global token count that races under concurrency.
        """

        return token_id != ENV_ACCESS_TOKEN_ID and status_code in FAILOVER_STATUS_CODES

    @staticmethod
    def _should_failover_timeout(token_id: str) -> bool:
        return token_id != ENV_ACCESS_TOKEN_ID

    @staticmethod
    def passthrough_headers(headers: Any) -> dict[str, str]:
        """Select upstream response headers clients may rely on.

        Claude Code reads the unified rate-limit headers off successful
        responses to surface 5h/weekly usage warnings; forward them so clients
        behind the proxy keep that visibility.
        """

        return {
            key: value
            for key, value in headers.items()
            if key.lower().startswith("anthropic-ratelimit-")
        }

    def _headers(
        self,
        token: str,
        request: ClaudeCodeRequest,
        client_betas: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {token}",
            "anthropic-version": self._settings.anthropic_version,
            "Content-Type": "application/json",
            "User-Agent": self._settings.user_agent,
        }
        beta_headers = self._beta_headers(request, client_betas)
        if beta_headers:
            headers["anthropic-beta"] = beta_headers
        return headers

    def _beta_headers(
        self,
        request: ClaudeCodeRequest,
        client_betas: str | None = None,
    ) -> str:
        betas = [
            entry.strip()
            for entry in self._settings.beta_headers.split(",")
            if entry.strip()
        ]
        # Claude Code clients gate body fields (context_management, 1M context)
        # behind their own beta entries; dropping them makes upstream reject the
        # already-forwarded body with "Extra inputs are not permitted".
        if client_betas:
            for entry in client_betas.split(","):
                cleaned = entry.strip()
                if cleaned and cleaned not in betas:
                    betas.append(cleaned)
        if self._needs_effort_beta(request) and EFFORT_BETA_HEADER not in betas:
            betas.append(EFFORT_BETA_HEADER)
        return ",".join(betas)

    @staticmethod
    def _needs_effort_beta(request: ClaudeCodeRequest) -> bool:
        model = request.model.lower()
        if (
            "opus-4-6" in model
            or "sonnet-4-6" in model
            or "opus-4-5" in model
        ):
            return True
        if not isinstance(request.output_config, dict):
            return False
        return request.output_config.get("effort") is not None

    def _build_upstream_request(self, request: ClaudeCodeRequest) -> dict[str, Any]:
        payload = request.model_dump(exclude_none=True, by_alias=True)
        system = self._normalize_system(payload.get("system"))
        payload["system"] = self._prepend_required_prompt(system)
        return payload

    @staticmethod
    def _normalize_system(system: Any) -> list[dict[str, Any]]:
        if system is None:
            return []
        if isinstance(system, str):
            return [{"type": "text", "text": system}]
        if not isinstance(system, list):
            raise ValueError("system must be a string or a list of content blocks")
        normalized: list[dict[str, Any]] = []
        for item in system:
            if isinstance(item, str):
                normalized.append({"type": "text", "text": item})
            elif isinstance(item, dict):
                normalized.append(dict(item))
            else:
                raise ValueError("system block entries must be strings or objects")
        return normalized

    def _prepend_required_prompt(
        self,
        system_blocks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        required = {
            "type": "text",
            "text": self._settings.required_system_prompt,
        }
        if system_blocks:
            first = system_blocks[0]
            if (
                first.get("type") == "text"
                and first.get("text") == self._settings.required_system_prompt
            ):
                return system_blocks
        return [required, *system_blocks]
