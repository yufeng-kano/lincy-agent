"""Discord self-bot adapter (discord.py-self) with local history store."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ...timezone_utils import now as tz_now
import logging
import random
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ..contact_map import ContactMap
from ..discord_history import DiscordHistoryStore
from ..schema import InboundMessage, OutboundMessage
from .formatting import format_attachment_lines

if TYPE_CHECKING:
    from ..core import AgentCore
    from ..thread_registry import ThreadRegistry
    from ...core.schema import DiscordChannelConfig

logger = logging.getLogger(__name__)

_DISCORD_MAX_MESSAGE_CHARS = 2000


def _utc_now() -> datetime:
    return tz_now()


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _display_name(author: Any) -> str:
    for attr in ("display_name", "global_name", "name"):
        val = getattr(author, attr, None)
        if isinstance(val, str) and val.strip():
            return val.strip()
    aid = getattr(author, "id", None)
    return str(aid) if aid is not None else "unknown"


def _channel_name(channel: Any) -> str:
    name = getattr(channel, "name", None)
    if isinstance(name, str) and name:
        return name
    cid = getattr(channel, "id", None)
    return str(cid) if cid is not None else "unknown"


def _guild_name(channel: Any) -> str | None:
    guild = getattr(channel, "guild", None)
    if guild is None:
        return None
    name = getattr(guild, "name", None)
    if isinstance(name, str) and name:
        return name
    gid = getattr(guild, "id", None)
    return str(gid) if gid is not None else None


def _channel_id(channel: Any) -> str:
    cid = getattr(channel, "id", None)
    return str(cid) if cid is not None else ""


def _guild_id(channel: Any) -> str | None:
    guild = getattr(channel, "guild", None)
    if guild is None:
        return None
    gid = getattr(guild, "id", None)
    return str(gid) if gid is not None else None


def _is_dm_channel(channel: Any) -> bool:
    return getattr(channel, "guild", None) is None


def _message_mentions_user(message: Any, user_id: str | None) -> bool:
    if not user_id:
        return False
    try:
        mentions = getattr(message, "mentions", None) or []
    except Exception:
        return False
    for u in mentions:
        uid = getattr(u, "id", None)
        if uid is not None and str(uid) == str(user_id):
            return True
    return False


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _build_channel_alias(channel: Any) -> str:
    name = _channel_name(channel)
    gname = _guild_name(channel)
    if gname:
        return f"#{name} @ {gname}"
    return f"DM:{name}"


def _split_discord_message(text: str, limit: int = _DISCORD_MAX_MESSAGE_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit + 1)
        if cut <= 0:
            cut = limit
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    return [p for p in parts if p] or [""]


@dataclass
class _DebounceBuffer:
    """Buffer for immediate (DM / mention review) debounce aggregation."""

    first_seen_monotonic: float
    messages: list[dict[str, Any]] = field(default_factory=list)
    latest_seq: int | None = None
    last_message_monotonic: float | None = None
    last_typing_monotonic: float | None = None


class DiscordAdapter:
    """Discord self-bot adapter with local history and review batching."""

    channel_name = "discord"
    priority = 1

    def __init__(
        self,
        *,
        token: str,
        contact_map: ContactMap,
        thread_registry: "ThreadRegistry" | None,
        config: "DiscordChannelConfig",
        history_store: DiscordHistoryStore,
    ) -> None:
        self._token = token
        self._contact_map = contact_map
        self._thread_registry = thread_registry  # intentionally unused in v1
        self._config = config
        self._history = history_store

        self._agent: AgentCore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._loop_ready = threading.Event()
        self._client_ready = threading.Event()
        self._self_user_id: str | None = None

        self._hard_allowlist_filters: dict[str, str] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._dm_buffers: dict[str, _DebounceBuffer] = {}
        self._mention_buffers: dict[str, _DebounceBuffer] = {}
        self._periodic_task: asyncio.Task[Any] | None = None
        self._presence_task: asyncio.Task[Any] | None = None
        self._typing_task: asyncio.Task[Any] | None = None
        self._typing_target_channel_id: str | None = None
        self._presence_last_active_monotonic: float = time.monotonic()
        self._presence_last_status: str | None = None
        self._startup_catchup_done = False

        # Scope IDs with messages still in debounce buffer.
        # Checked by the preempt mechanism so it can see pending
        # messages before debounce flushes them into the queue.
        self._buffered_scopes: set[str] = set()
        self._buffered_scopes_lock = threading.Lock()

    @property
    def attachments_dir(self) -> str:
        return str(self._history.media_dir)

    @property
    def history_base_dir(self) -> str:
        return str(self._history.base_dir)

    @property
    def history_store(self) -> DiscordHistoryStore:
        return self._history

    def has_buffered_inbound(self, scope_id: str) -> bool:
        """Return True if a debounce buffer holds messages for *scope_id*.

        Called from the preempt checker (brain thread) so must be thread-safe.
        """
        with self._buffered_scopes_lock:
            return scope_id in self._buffered_scopes

    # -- ChannelAdapter protocol --------------------------------------

    def start(self, agent: "AgentCore") -> None:
        self._agent = agent
        self._hard_allowlist_filters = {
            entry.channel_id: entry.filter
            for entry in self._config.listen_channels
        }
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="discord-adapter",
            daemon=True,
        )
        self._thread.start()

    def send(self, message: OutboundMessage) -> None:
        if self._loop is None:
            logger.warning("Discord send: event loop not ready")
            return
        if not self._loop_ready.wait(timeout=10):
            logger.warning("Discord send: loop readiness timeout")
            return
        # Allow proactive sends before on_ready, but most flows need client ready.
        if not self._client_ready.wait(timeout=30):
            logger.warning("Discord send: client not ready")
            return
        future = asyncio.run_coroutine_threadsafe(self._async_send(message), self._loop)
        future.result(timeout=30)

    def on_turn_start(self, channel: str) -> None:
        if channel != "discord":
            return
        self._mark_presence_active()

    def on_turn_complete(self) -> None:
        pass

    def stop(self) -> None:
        self._stop_event.set()
        if self._loop and self._loop_ready.is_set():
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown_async(), self._loop).result(timeout=10)
            except Exception:
                logger.exception("Discord adapter async shutdown failed")
        if self._thread is not None:
            self._thread.join(timeout=10)

    # -- Thread / event loop -----------------------------------------

    def _run_loop(self) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop_ready.set()
            self._loop.run_until_complete(self._run_main())
        except Exception:
            logger.exception("Discord adapter loop crashed")
        finally:
            try:
                if self._loop is not None and not self._loop.is_closed():
                    self._loop.close()
            except Exception:
                pass
            self._loop = None
            self._client = None
            self._loop_ready.clear()
            self._client_ready.clear()

    async def _run_main(self) -> None:
        if self._stop_event.is_set():
            return
        client = self._build_client()
        if client is None:
            return
        self._client = client
        self._periodic_task = asyncio.create_task(self._periodic_review_loop())
        if self._config.presence_mode != "off":
            self._presence_task = asyncio.create_task(self._presence_loop())
        try:
            await client.start(self._token)
        except Exception:
            logger.exception("Discord client.start failed")
        finally:
            if self._periodic_task is not None:
                self._periodic_task.cancel()
                try:
                    await self._periodic_task
                except BaseException:
                    pass
                self._periodic_task = None
            if self._presence_task is not None:
                self._presence_task.cancel()
                try:
                    await self._presence_task
                except BaseException:
                    pass
                self._presence_task = None

    async def _shutdown_async(self) -> None:
        self._flush_all_pending_buffers()
        await self._stop_thinking_typing()
        for handle in list(self._timers.values()):
            handle.cancel()
        self._timers.clear()
        if self._periodic_task is not None:
            self._periodic_task.cancel()
        if self._presence_task is not None:
            self._presence_task.cancel()
        client = self._client
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.debug("Discord client close failed", exc_info=True)

    def _flush_all_pending_buffers(self) -> None:
        """Flush all pending debounce buffers before shutdown.

        Ensures messages received but not yet enqueued survive graceful restart.
        """
        for channel_id in list(self._dm_buffers.keys()):
            try:
                self._flush_dm_buffer(channel_id)
            except Exception:
                logger.debug(
                    "Flush DM buffer on shutdown failed: ch=%s",
                    channel_id, exc_info=True,
                )
        for channel_id in list(self._mention_buffers.keys()):
            try:
                self._flush_mention_review(channel_id)
            except Exception:
                logger.debug(
                    "Flush mention buffer on shutdown failed: ch=%s",
                    channel_id, exc_info=True,
                )

    def _build_client(self) -> Any | None:
        try:
            import discord  # type: ignore
        except ImportError:
            logger.error("discord.py-self not installed; Discord adapter disabled")
            return None

        adapter = self

        class _DiscordClient(discord.Client):  # type: ignore[misc, valid-type]
            async def on_ready(self) -> None:  # pragma: no cover - integration callback
                user = getattr(self, "user", None)
                uid = getattr(user, "id", None)
                adapter._self_user_id = str(uid) if uid is not None else None
                adapter._mark_presence_active()
                adapter._client_ready.set()
                logger.info(
                    "Discord adapter ready as %s",
                    getattr(user, "name", uid),
                )
                asyncio.ensure_future(adapter._startup_catchup())

            async def on_message(self, message: Any) -> None:  # pragma: no cover - integration callback
                await adapter._handle_message(message)

            async def on_message_edit(self, before: Any, after: Any) -> None:  # pragma: no cover - integration callback
                del before
                await adapter._handle_message_edit(after)

            async def on_typing(self, channel: Any, user: Any, when: Any) -> None:  # pragma: no cover - integration callback
                del when
                adapter._handle_typing(_channel_id(channel), str(getattr(user, "id", "")))

        kwargs: dict[str, Any] = {}
        intents_cls = getattr(discord, "Intents", None)
        if intents_cls is not None:
            try:
                intents = intents_cls.default()
                for attr in (
                    "guilds",
                    "messages",
                    "dm_messages",
                    "message_content",
                    "typing",
                    "dm_typing",
                ):
                    if hasattr(intents, attr):
                        setattr(intents, attr, True)
                kwargs["intents"] = intents
            except Exception:
                logger.debug("Discord intents setup failed", exc_info=True)
        try:
            return _DiscordClient(**kwargs)
        except TypeError:
            return _DiscordClient()

    # -- Sending ------------------------------------------------------

    async def _async_send(self, message: OutboundMessage) -> None:
        self._mark_presence_active()
        client = self._client
        if client is None:
            logger.warning("Discord send: no client")
            return

        target_channel = None
        metadata = message.metadata or {}
        channel_id = metadata.get("channel_id")
        reply_to_user = metadata.get("reply_to")
        reply_message_id = metadata.get("message_id")

        if channel_id:
            target_channel = await self._resolve_channel(str(channel_id))
        elif reply_to_user:
            target_channel = await self._resolve_dm_channel(str(reply_to_user))
        if target_channel is None:
            logger.warning("Discord send: no target channel resolved")
            return

        body = message.content
        chunks = _split_discord_message(body, _DISCORD_MAX_MESSAGE_CHARS)
        files = await self._build_discord_files(message.attachments)
        try:
            send_delay = self._estimate_send_delay_seconds(
                body,
                chunk_count=len(chunks),
                has_attachments=bool(files),
            )
            await self._wait_before_send_with_typing(target_channel, send_delay)
            ref = self._build_reply_reference(target_channel, reply_message_id, metadata)
            for i, chunk in enumerate(chunks):
                kwargs: dict[str, Any] = {}
                if ref is not None:
                    kwargs["reference"] = ref
                if i == 0 and files:
                    kwargs["files"] = files
                if i > 0:
                    await self._wait_before_send_with_typing(
                        target_channel,
                        self._estimate_followup_chunk_delay(chunk),
                    )
                sent_msg = await target_channel.send(chunk, **kwargs)
                if sent_msg is not None:
                    try:
                        event = self._build_outbound_event(sent_msg, target_channel)
                        ch_id = _channel_id(target_channel)
                        if ch_id:
                            self._history.append_message_create(channel_id=ch_id, event=event)
                    except Exception:
                        logger.debug("Failed to record outbound in history", exc_info=True)
        finally:
            for f in files:
                close = getattr(f, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

    def _estimate_send_delay_seconds(
        self,
        body: str,
        *,
        chunk_count: int,
        has_attachments: bool,
    ) -> float:
        """Estimate a human-like pre-send typing delay.

        `send_delay_min/max` acts as the baseline "thinking then starting to type"
        jitter. Extra delay is added based on message length/chunks/attachments so
        longer outputs do not look instantly generated.
        """
        min_delay = float(self._config.send_delay_min)
        max_delay = float(self._config.send_delay_max)
        if min_delay <= 0 and max_delay <= 0:
            return 0.0

        base = random.uniform(min(min_delay, max_delay), max(min_delay, max_delay))
        text = body.strip()
        if not text:
            char_delay = 0.0
        else:
            chars_per_sec = random.uniform(
                self._config.send_typing_cps_min,
                self._config.send_typing_cps_max,
            )
            char_delay = min(self._config.send_delay_char_max, len(text) / chars_per_sec)
        chunk_penalty = max(0, chunk_count - 1) * random.uniform(0.4, 0.9)
        attachment_penalty = random.uniform(0.4, 1.0) if has_attachments else 0.0
        return min(self._config.send_delay_total_max, base + char_delay + chunk_penalty + attachment_penalty)

    def _estimate_followup_chunk_delay(self, chunk: str) -> float:
        if self._config.send_delay_min <= 0 and self._config.send_delay_max <= 0:
            return 0.0
        cps = random.uniform(
            self._config.send_typing_cps_min,
            self._config.send_typing_cps_max,
        )
        return min(4.0, 0.2 + (len(chunk.strip()) / cps))

    async def _wait_before_send_with_typing(self, channel: Any, delay: float) -> None:
        delay_f = max(0.0, float(delay))
        # Safety net: cancel any lingering typing task before send-phase delay.
        await self._stop_thinking_typing()
        if delay_f <= 0:
            return

        refresh = min(float(self._config.send_typing_refresh_seconds), 5.0)
        remaining = delay_f
        while remaining > 0 and not self._stop_event.is_set():
            try:
                await self._send_typing_once(channel)
            except Exception:
                logger.debug("Discord send-phase typing failed", exc_info=True)
                break
            step = min(refresh, remaining)
            await asyncio.sleep(step)
            remaining -= step

    async def _resolve_channel(self, channel_id: str) -> Any | None:
        client = self._client
        if client is None:
            return None
        try:
            cid_int = int(channel_id)
        except (TypeError, ValueError):
            return None
        ch = client.get_channel(cid_int)
        if ch is not None:
            return ch
        fetch = getattr(client, "fetch_channel", None)
        if callable(fetch):
            try:
                return await fetch(cid_int)
            except Exception:
                logger.debug("Discord fetch_channel failed for %s", channel_id, exc_info=True)
        return None

    async def _resolve_dm_channel(self, user_id: str) -> Any | None:
        client = self._client
        if client is None:
            return None
        try:
            uid_int = int(user_id)
        except (TypeError, ValueError):
            return None
        user = None
        get_user = getattr(client, "get_user", None)
        if callable(get_user):
            user = get_user(uid_int)
        if user is None:
            fetch_user = getattr(client, "fetch_user", None)
            if callable(fetch_user):
                try:
                    user = await fetch_user(uid_int)
                except Exception:
                    logger.debug("Discord fetch_user failed for %s", user_id, exc_info=True)
        if user is None:
            return None
        create_dm = getattr(user, "create_dm", None)
        if callable(create_dm):
            return await create_dm()
        dm_channel = getattr(user, "dm_channel", None)
        return dm_channel

    def _build_reply_reference(
        self,
        channel: Any,
        reply_message_id: str | None,
        metadata: dict[str, Any],
    ) -> Any | None:
        if not reply_message_id:
            return None
        try:
            mid_int = int(reply_message_id)
        except ValueError:
            return None
        get_partial = getattr(channel, "get_partial_message", None)
        if callable(get_partial):
            try:
                return get_partial(mid_int)
            except Exception:
                pass
        try:
            import discord  # type: ignore
        except ImportError:
            return None
        try:
            return discord.MessageReference(  # type: ignore[attr-defined]
                message_id=mid_int,
                channel_id=int(metadata.get("channel_id")) if metadata.get("channel_id") else None,
                guild_id=int(metadata.get("guild_id")) if metadata.get("guild_id") else None,
                fail_if_not_exists=False,
            )
        except Exception:
            return None

    async def _build_discord_files(self, attachments: list[str]) -> list[Any]:
        if not attachments:
            return []
        try:
            import discord  # type: ignore
        except ImportError:
            return []
        files: list[Any] = []
        for path in attachments:
            try:
                files.append(discord.File(Path(path)))  # type: ignore[attr-defined]
            except Exception:
                logger.exception("Discord send: failed to open attachment %s", path)
        return files

    # -- Discord event handlers --------------------------------------

    async def _handle_message(self, message: Any) -> None:
        if self._agent is None:
            return
        author = getattr(message, "author", None)
        if author is None:
            return
        author_id = str(getattr(author, "id", ""))
        if not author_id:
            return
        if self._self_user_id and author_id == self._self_user_id:
            return
        if author_id in set(self._config.ignore_users):
            return

        channel = getattr(message, "channel", None)
        if channel is None:
            return
        channel_id = _channel_id(channel)
        if not channel_id:
            return

        is_dm = _is_dm_channel(channel)
        mentions_self = _message_mentions_user(message, self._self_user_id)

        if is_dm:
            if not self._config.listen_dms:
                return
        else:
            if not self._guild_ingestion_allowed(message, mentions_self):
                return

        self._mark_presence_active()
        snapshot = await self._snapshot_message(message, mentions_self=mentions_self)
        if snapshot is None:
            return

        # Persist sender and channel aliases for proactive sends.
        self._contact_map.update("discord", snapshot["author_id"], snapshot["author_display_name"])
        if is_dm:
            self._history.upsert_channel(
                channel_id=channel_id,
                guild_id=None,
                guild_name=None,
                channel_name="dm",
                alias=snapshot["author_display_name"] or snapshot["author_name"] or channel_id,
                filter_mode="all",
                source="dm",
                review_interval_seconds=None,
                extra={"dm_peer_user_id": snapshot["author_id"]},
            )
        else:
            alias = snapshot["channel_alias"]
            self._contact_map.update("discord", channel_id, alias)

        event = self._event_from_snapshot(snapshot)
        seq = self._history.append_message_create(channel_id=channel_id, event=event)
        self._history.update_channel_last_seen(
            channel_id,
            message_id=snapshot["message_id"],
            seen_at=snapshot["event_time"],
        )

        if is_dm:
            self._buffer_dm(channel_id, snapshot, seq)
            self._reset_timer(f"dm:{channel_id}", self._flush_dm_buffer, channel_id)
            return

        # Guild immediate review on mention (or reply-to-self in future)
        if mentions_self:
            self._buffer_mention(channel_id, seq, snapshot)
            self._reset_timer(
                f"mention:{channel_id}",
                self._flush_mention_review,
                channel_id,
            )

    async def _handle_message_edit(self, message: Any) -> None:
        author = getattr(message, "author", None)
        channel = getattr(message, "channel", None)
        if author is None or channel is None:
            return
        author_id = str(getattr(author, "id", ""))
        if not author_id:
            return
        if self._self_user_id and author_id == self._self_user_id:
            return
        channel_id = _channel_id(channel)
        if not channel_id:
            return
        if _is_dm_channel(channel):
            if not self._config.listen_dms:
                return
        else:
            if self._history.get_channel_entry(channel_id) is None:
                return
        mentions_self = _message_mentions_user(message, self._self_user_id)
        snapshot = await self._snapshot_message(message, mentions_self=mentions_self)
        if snapshot is None:
            return
        event = self._event_from_snapshot(snapshot)
        event["edited_at"] = event.get("event_time")
        seq = self._history.append_message_edit(channel_id=channel_id, event=event)
        self._update_buffered_message(channel_id, snapshot, seq)

    def _handle_typing(self, channel_id: str, user_id: str) -> None:
        if not channel_id or not user_id:
            return
        if self._self_user_id and str(user_id) == str(self._self_user_id):
            return
        dm_key = f"dm:{channel_id}"
        mention_key = f"mention:{channel_id}"
        if dm_key in self._timers:
            if self._loop is not None:
                dm_buf = self._dm_buffers.get(channel_id)
                if dm_buf is not None:
                    dm_buf.last_typing_monotonic = self._loop.time()
                    logger.debug(
                        "DM typing reset: ch=%s user=%s, new deadline=+%.1fs",
                        channel_id, user_id,
                        float(self._config.dm_typing_quiet_seconds),
                    )
            self._reset_timer(dm_key, self._flush_dm_buffer, channel_id)
        else:
            logger.debug(
                "DM typing ignored (no active buffer): ch=%s user=%s",
                channel_id, user_id,
            )
        if mention_key in self._timers:
            self._reset_timer(mention_key, self._flush_mention_review, channel_id)

    # -- Filtering / registration ------------------------------------

    def _guild_ingestion_allowed(self, message: Any, mentions_self: bool) -> bool:
        channel = getattr(message, "channel", None)
        if channel is None:
            return False
        channel_id = _channel_id(channel)
        if not channel_id:
            return False

        if self._hard_allowlist_filters and channel_id not in self._hard_allowlist_filters:
            return False

        entry = self._history.get_channel_entry(channel_id)
        if entry is None and channel_id in self._hard_allowlist_filters:
            alias = _build_channel_alias(channel)
            entry = self._history.upsert_channel(
                channel_id=channel_id,
                guild_id=_guild_id(channel),
                guild_name=_guild_name(channel),
                channel_name=_channel_name(channel),
                alias=alias,
                filter_mode=self._hard_allowlist_filters[channel_id],
                source="bootstrap_config",
                review_interval_seconds=self._config.guild_review_interval_seconds,
            )

        if entry is None:
            if not mentions_self:
                return False
            alias = _build_channel_alias(channel)
            entry = self._history.upsert_channel(
                channel_id=channel_id,
                guild_id=_guild_id(channel),
                guild_name=_guild_name(channel),
                channel_name=_channel_name(channel),
                alias=alias,
                filter_mode="all",
                source="auto_mention",
                review_interval_seconds=self._config.guild_review_interval_seconds,
            )

        filter_mode = entry.get("filter", "mention_only")
        if filter_mode == "mute":
            return False
        if mentions_self:
            return True
        if filter_mode == "all":
            return True
        if filter_mode == "mention_only":
            return False
        if filter_mode == "from_contacts":
            author = getattr(message, "author", None)
            author_id = str(getattr(author, "id", "")) if author is not None else ""
            return bool(author_id and self._contact_map.resolve("discord", author_id))
        return False

    # -- Snapshot / normalization ------------------------------------

    async def _snapshot_message(self, message: Any, *, mentions_self: bool) -> dict[str, Any] | None:
        channel = getattr(message, "channel", None)
        author = getattr(message, "author", None)
        if channel is None or author is None:
            return None

        channel_id = _channel_id(channel)
        guild_id = _guild_id(channel)
        is_dm = _is_dm_channel(channel)
        msg_id = str(getattr(message, "id", ""))
        created_at = getattr(message, "created_at", None)
        event_time = _iso(_utc_now())
        message_time = _iso(created_at if isinstance(created_at, datetime) else _utc_now())
        author_id = str(getattr(author, "id", ""))
        author_name = _safe_text(getattr(author, "name", ""))
        author_display = _display_name(author)
        content = _safe_text(getattr(message, "content", ""))

        reply_to = None
        reply_author_id = None
        reply_author_name = None
        reply_preview = None
        reference = getattr(message, "reference", None)
        if reference is not None:
            reply_to = getattr(reference, "message_id", None)
        resolved_ref = getattr(reference, "resolved", None) if reference is not None else None
        if resolved_ref is not None:
            ref_author = getattr(resolved_ref, "author", None)
            if ref_author is not None:
                reply_author_id = str(getattr(ref_author, "id", "")) or None
                reply_author_name = _display_name(ref_author)
            reply_preview = self._history.preview_text(_safe_text(getattr(resolved_ref, "content", "")))

        embeds = self._extract_embeds(getattr(message, "embeds", None) or [])
        stickers = self._extract_stickers(getattr(message, "stickers", None) or [])
        attachments = await self._extract_attachments(
            channel_id=channel_id,
            message_id=msg_id,
            attachments=getattr(message, "attachments", None) or [],
            is_dm=is_dm,
        )

        channel_alias = _build_channel_alias(channel)
        reply_to_message_id = str(reply_to) if reply_to is not None else None
        normalized_text = self._build_normalized_text(
            content=content,
            reply_message_id=reply_to_message_id,
            reply_author_name=reply_author_name,
            reply_preview=reply_preview,
            embeds=embeds,
            stickers=stickers,
            attachments=attachments,
        )

        return {
            "event_time": event_time,
            "channel_id": channel_id,
            "guild_id": guild_id,
            "guild_name": _guild_name(channel),
            "channel_name": _channel_name(channel),
            "channel_alias": channel_alias,
            "message_id": msg_id,
            "message_time": message_time,
            "author_id": author_id,
            "author_name": author_name,
            "author_display_name": author_display,
            "is_dm": is_dm,
            "mentions_self": mentions_self,
            "raw_content": content,
            "reply_to_message_id": reply_to_message_id,
            "reply_to_author_id": reply_author_id,
            "reply_to_author_name": reply_author_name,
            "reply_to_preview_text": reply_preview,
            "embeds": embeds,
            "stickers": stickers,
            "attachments": attachments,
            "normalized_text": normalized_text,
        }

    def _event_from_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            "event_time": snapshot["event_time"],
            "channel_id": snapshot["channel_id"],
            "guild_id": snapshot["guild_id"],
            "message_id": snapshot["message_id"],
            "message_time": snapshot["message_time"],
            "author_id": snapshot["author_id"],
            "author_name": snapshot["author_name"],
            "author_display_name": snapshot["author_display_name"],
            "is_dm": snapshot["is_dm"],
            "mentions_self": snapshot["mentions_self"],
            "raw_content": snapshot["raw_content"],
            "reply_to_message_id": snapshot["reply_to_message_id"],
            "reply_to_author_id": snapshot["reply_to_author_id"],
            "reply_to_author_name": snapshot["reply_to_author_name"],
            "reply_to_preview_text": snapshot["reply_to_preview_text"],
            "embeds": snapshot["embeds"],
            "stickers": snapshot["stickers"],
            "attachments": snapshot["attachments"],
            "normalized_text": snapshot["normalized_text"],
            "edited_at": None,
        }

    def _build_outbound_event(self, sent_msg: Any, target_channel: Any) -> dict[str, Any]:
        author = getattr(sent_msg, "author", None)
        if author is None and self._client is not None:
            author = getattr(self._client, "user", None)
        created_at = getattr(sent_msg, "created_at", None)
        msg_time = _iso(created_at) if isinstance(created_at, datetime) else _iso(_utc_now())
        now = _iso(_utc_now())
        return {
            "event_time": now,
            "channel_id": _channel_id(target_channel),
            "guild_id": _guild_id(target_channel),
            "message_id": str(getattr(sent_msg, "id", "")),
            "message_time": msg_time,
            "author_id": self._self_user_id or "",
            "author_name": _safe_text(getattr(author, "name", "")) if author else "",
            "author_display_name": _display_name(author) if author else "",
            "is_dm": _is_dm_channel(target_channel),
            "mentions_self": False,
            "raw_content": _safe_text(getattr(sent_msg, "content", "")),
            "reply_to_message_id": None,
            "reply_to_author_id": None,
            "reply_to_author_name": None,
            "reply_to_preview_text": None,
            "embeds": [],
            "stickers": [],
            "attachments": [],
            "normalized_text": _safe_text(getattr(sent_msg, "content", "")),
            "edited_at": None,
        }

    def _extract_embeds(self, embeds: list[Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for emb in embeds:
            url = _safe_text(getattr(emb, "url", "")).strip() or None
            title = _safe_text(getattr(emb, "title", "")).strip() or None
            desc = _safe_text(getattr(emb, "description", "")).strip() or None
            etype = _safe_text(getattr(emb, "type", "")).strip() or None
            provider = getattr(emb, "provider", None)
            site_name = None
            if provider is not None:
                site_name = _safe_text(getattr(provider, "name", "")).strip() or None
            if not any([url, title, desc, etype, site_name]):
                continue
            item = {
                "url": url,
                "title": self._history.preview_text(title),
                "description": self._history.preview_text(desc, max_chars=500),
                "site_name": site_name,
                "type": etype,
            }
            result.append(item)
        return result

    def _extract_stickers(self, stickers: list[Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for sticker in stickers:
            result.append({
                "id": str(getattr(sticker, "id", "")) or None,
                "name": _safe_text(getattr(sticker, "name", "")) or None,
                "format": _safe_text(getattr(sticker, "format", "")) or None,
            })
        return result

    async def _extract_attachments(
        self,
        *,
        channel_id: str,
        message_id: str,
        attachments: list[Any],
        is_dm: bool,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        max_download_bytes = self._config.auto_download_attachment_max_mb * 1024 * 1024
        max_auto_read_bytes = self._config.auto_read_image_max_mb * 1024 * 1024
        image_hint_count = 0
        for att in attachments:
            filename = _safe_text(getattr(att, "filename", "")) or "attachment"
            content_type = _safe_text(getattr(att, "content_type", "")) or None
            size = getattr(att, "size", None)
            try:
                size_int = int(size) if size is not None else None
            except (TypeError, ValueError):
                size_int = None
            url = _safe_text(getattr(att, "url", "")) or None
            item: dict[str, Any] = {
                "filename": filename,
                "content_type": content_type,
                "size": size_int,
                "url": url,
                "local_path": None,
                "is_image": bool(content_type and content_type.startswith("image/")),
                "image_summary": None,
                "needs_summary": False,
                "sha256": None,
                "download_note": None,
            }

            should_auto_read = (
                self._config.auto_read_images
                and item["is_image"]
                and (
                    (is_dm and self._config.auto_read_images_in_dm)
                    or (not is_dm and self._config.auto_read_images_in_guild)
                )
            )
            if size_int is not None and size_int > max_download_bytes:
                item["download_note"] = "too large, not downloaded"
            else:
                local_path = await self._download_attachment(channel_id, message_id, att, filename, url)
                if local_path is not None:
                    item["local_path"] = str(local_path)
                else:
                    item["download_note"] = "download failed"

            if item["is_image"] and item["local_path"]:
                local_path = Path(item["local_path"])
                sha = self._history.compute_sha256(local_path)
                item["sha256"] = sha
                cached = self._history.get_image_summary(sha)
                if cached and isinstance(cached.get("summary"), str):
                    item["image_summary"] = cached["summary"]
                elif (
                    should_auto_read
                    and (size_int is None or size_int <= max_auto_read_bytes)
                    and image_hint_count < self._config.auto_read_image_max_per_batch
                ):
                    item["needs_summary"] = True
                    image_hint_count += 1
            result.append(item)
        return result

    def _append_attachment_lines(
        self,
        lines: list[str],
        attachments: list[dict[str, Any]],
        *,
        indent: str = "",
    ) -> None:
        lines.extend(format_attachment_lines(attachments, indent=indent))

    async def _download_attachment(
        self,
        channel_id: str,
        message_id: str,
        attachment: Any,
        filename: str,
        url: str | None,
    ) -> Path | None:
        dst = self._history.make_media_path(channel_id, message_id, filename)
        save = getattr(attachment, "save", None)
        if callable(save):
            try:
                await save(dst)
                return dst
            except Exception:
                logger.debug("Discord attachment.save failed", exc_info=True)
        read = getattr(attachment, "read", None)
        if callable(read):
            try:
                data = await read()
                dst.write_bytes(data)
                return dst
            except Exception:
                logger.debug("Discord attachment.read failed", exc_info=True)
        if url:
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                dst.write_bytes(resp.content)
                return dst
            except Exception:
                logger.debug("Discord attachment URL download failed", exc_info=True)
        return None

    def _append_structured_message_blocks(
        self,
        lines: list[str],
        *,
        content: str,
        reply_message_id: str | None = None,
        reply_author_name: str | None = None,
        reply_preview: str | None = None,
        embeds: list[dict[str, Any]] | None = None,
        stickers: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        indent: str = "",
    ) -> None:
        """Append inbound blocks that keep message body separate from context."""
        embeds = embeds or []
        stickers = stickers or []
        attachments = attachments or []

        # Always emit [Message] so reply/context blocks are never mistaken for body.
        lines.append(f"{indent}[Message]")
        body = _safe_text(content)
        if body:
            for body_line in body.splitlines():
                lines.append(f"{indent}{body_line}")

        if reply_message_id or reply_preview:
            lines.append(f"{indent}[Reply To]")
            if reply_message_id:
                lines.append(f"{indent}message_id: {reply_message_id}")
            lines.append(f"{indent}author: {reply_author_name or 'unknown'}")
            if reply_preview:
                lines.append(f"{indent}preview: {reply_preview}")

        for emb in embeds:
            title = emb.get("title") or emb.get("url") or emb.get("site_name") or "link"
            desc = emb.get("description")
            lines.append(f"{indent}[Link Preview]")
            lines.append(f"{indent}title: {title}")
            if desc:
                lines.append(f"{indent}description: {desc}")

        for stk in stickers:
            name = stk.get("name") or stk.get("id") or "sticker"
            lines.append(f"{indent}[Sticker]")
            lines.append(f"{indent}name: {name}")

        self._append_attachment_lines(lines, attachments, indent=indent)

    def _build_normalized_text(
        self,
        *,
        content: str,
        reply_message_id: str | None = None,
        reply_author_name: str | None = None,
        reply_preview: str | None = None,
        embeds: list[dict[str, Any]] | None = None,
        stickers: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> str:
        lines: list[str] = []
        self._append_structured_message_blocks(
            lines,
            content=content,
            reply_message_id=reply_message_id,
            reply_author_name=reply_author_name,
            reply_preview=reply_preview,
            embeds=embeds,
            stickers=stickers,
            attachments=attachments,
        )
        return "\n".join(lines).strip()

    # -- Debounce / review batching ----------------------------------

    def _buffer_dm(self, channel_id: str, snapshot: dict[str, Any], seq: int) -> None:
        assert self._loop is not None
        now = self._loop.time()
        buf = self._dm_buffers.get(channel_id)
        if buf is None:
            buf = _DebounceBuffer(first_seen_monotonic=now)
            self._dm_buffers[channel_id] = buf
        else:
            # Reset hard cap anchor so dm_max_wait_seconds counts from latest message.
            buf.first_seen_monotonic = now
        buf.messages.append(snapshot)
        buf.last_message_monotonic = now
        buf.latest_seq = max(buf.latest_seq or 0, seq)

        # Signal preempt checker immediately (before debounce flush).
        author_id = snapshot.get("author_id")
        if author_id:
            scope_id = f"discord:dm:{author_id}"
            with self._buffered_scopes_lock:
                self._buffered_scopes.add(scope_id)

    def _buffer_mention(self, channel_id: str, seq: int, snapshot: dict[str, Any]) -> None:
        assert self._loop is not None
        buf = self._mention_buffers.get(channel_id)
        if buf is None:
            buf = _DebounceBuffer(first_seen_monotonic=self._loop.time())
            self._mention_buffers[channel_id] = buf
        buf.messages.append(snapshot)
        buf.latest_seq = seq

    def _update_buffered_message(self, channel_id: str, snapshot: dict[str, Any], seq: int) -> None:
        for table in (self._dm_buffers, self._mention_buffers):
            buf = table.get(channel_id)
            if buf is None:
                continue
            for i, item in enumerate(buf.messages):
                if item.get("message_id") == snapshot.get("message_id"):
                    buf.messages[i] = snapshot
            buf.latest_seq = max(int(buf.latest_seq or 0), seq)

    def _reset_timer(self, key: str, cb: Any, channel_id: str) -> None:
        if self._loop is None:
            return
        old = self._timers.pop(key, None)
        if old is not None:
            old.cancel()
        if key.startswith("dm:"):
            buf = self._dm_buffers.get(channel_id)
        else:
            buf = self._mention_buffers.get(channel_id)
        if buf is None:
            return
        age = self._loop.time() - buf.first_seen_monotonic
        max_wait = (
            float(self._config.dm_max_wait_seconds)
            if key.startswith("dm:")
            else float(self._config.max_wait_seconds)
        )
        if age >= max_wait:
            self._loop.call_soon(cb, channel_id)
            return
        if key.startswith("dm:"):
            delay = self._compute_dm_flush_delay(buf)
        else:
            delay = float(self._config.debounce_seconds)
            remaining = max(0.0, max_wait - age)
            delay = min(delay, remaining)
        self._timers[key] = self._loop.call_later(delay, cb, channel_id)

    def _compute_dm_flush_delay(self, buf: _DebounceBuffer) -> float:
        assert self._loop is not None
        now = self._loop.time()
        age = now - buf.first_seen_monotonic
        remaining = max(0.0, float(self._config.dm_max_wait_seconds) - age)

        last_msg = buf.last_message_monotonic or now
        msg_quiet = float(self._config.dm_debounce_seconds) + float(
            min(max(len(buf.messages) - 1, 0), 3)
        )
        target_at = last_msg + msg_quiet
        if buf.last_typing_monotonic is not None:
            target_at = max(
                target_at,
                buf.last_typing_monotonic + float(self._config.dm_typing_quiet_seconds),
            )

        # If the latest DM is attachment-only (e.g., user sends image first and
        # plans to follow with text), wait a little longer even without typing.
        if buf.messages:
            last = buf.messages[-1]
            has_attachments = bool(last.get("attachments") or [])
            raw_text = _safe_text(last.get("raw_content", "")).strip()
            if has_attachments and not raw_text:
                target_at = max(target_at, last_msg + 8.0)

        return min(max(0.0, target_at - now), remaining)

    def _flush_dm_buffer(self, channel_id: str) -> None:
        self._timers.pop(f"dm:{channel_id}", None)
        buf = self._dm_buffers.pop(channel_id, None)
        if buf is None or not buf.messages or self._agent is None:
            return
        # Clear preempt signal — message is about to enter the queue.
        author_id = buf.messages[-1].get("author_id")
        if author_id:
            with self._buffered_scopes_lock:
                self._buffered_scopes.discard(f"discord:dm:{author_id}")
        msgs = sorted(buf.messages, key=lambda m: m.get("message_time") or "")
        last = msgs[-1]
        sender_name = self._contact_map.resolve("discord", last["author_id"]) or last["author_display_name"]
        # Allow one alias hop (e.g. numeric_id -> handle -> preferred name).
        if sender_name:
            alias_name = self._contact_map.resolve("discord", str(sender_name))
            if alias_name:
                sender_name = alias_name
        content_lines: list[str] = []
        for m in msgs:
            text = _safe_text(m.get("normalized_text", "")).strip() or _safe_text(m.get("raw_content", "")).strip()
            if text:
                content_lines.append(text)
            self._append_image_hint_only(content_lines, m, is_dm=True)
        content = "\n".join([x for x in content_lines if x]).strip()
        metadata = {
            "channel_id": channel_id,
            "message_id": last["message_id"],
            "author_id": last["author_id"],
            "reply_to": last["author_id"],
            "reply_to_message_id": last.get("reply_to_message_id"),
            "is_dm": True,
            "source": "dm_immediate",
        }
        inbound = InboundMessage(
            channel="discord",
            content=content or "[Empty Discord DM message]",
            priority=self.priority,
            sender=sender_name,
            metadata=metadata,
            timestamp=_parse_iso(last["message_time"]) or tz_now(),
        )
        self._agent.enqueue(inbound)
        if buf.latest_seq is not None:
            self._history.mark_reviewed(channel_id, seq=buf.latest_seq, immediate=True)

    def _flush_mention_review(self, channel_id: str) -> None:
        self._timers.pop(f"mention:{channel_id}", None)
        buf = self._mention_buffers.pop(channel_id, None)
        if buf is None:
            return
        self._enqueue_review_from_history(channel_id, source="guild_mention_review", immediate=True)

    async def _periodic_review_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._run_periodic_review_tick()
            except Exception:
                logger.exception("Discord periodic review tick failed")
            await asyncio.sleep(1.0)

    def _run_periodic_review_tick(self) -> None:
        now = tz_now()
        for entry in self._history.list_registered_channels():
            if not entry.get("guild_id"):
                continue
            if entry.get("filter") != "all":
                continue
            channel_id = str(entry.get("channel_id") or "")
            if not channel_id:
                continue
            interval = entry.get("review_interval_seconds")
            try:
                interval_i = int(interval) if interval is not None else self._config.guild_review_interval_seconds
            except (TypeError, ValueError):
                interval_i = self._config.guild_review_interval_seconds
            cursor = self._history.get_cursor(channel_id)
            last_review_at = _parse_iso(cursor.get("last_review_at"))
            if last_review_at is not None and (now - last_review_at).total_seconds() < interval_i:
                continue
            self._enqueue_review_from_history(channel_id, source="guild_review", immediate=False)

    def _enqueue_review_from_history(self, channel_id: str, *, source: str, immediate: bool) -> None:
        if self._agent is None:
            return
        cursor = self._history.get_cursor(channel_id)
        after_seq = int(cursor.get("last_reviewed_seq", 0))
        events = self._history.get_events_after_seq(channel_id, after_seq)
        if not events:
            return
        folded = self._history.fold_latest_messages(events)
        if not folded:
            # Mark reviewed anyway so invalid/corrupt-only tails don't loop forever.
            max_seq = max(int(e.get("seq", 0)) for e in events)
            self._history.mark_reviewed(channel_id, seq=max_seq, immediate=immediate)
            return

        entry = self._history.get_channel_entry(channel_id) or {}
        alias = str(entry.get("alias") or channel_id)
        header = f"[{alias}]"
        lines: list[str] = [header]
        for msg in folded:
            author = msg.get("author") or msg.get("author_id") or "unknown"
            author_id = msg.get("author_id") or "unknown"
            lines.append(f"{author} <@{author_id}>:")
            reply = msg.get("reply") if isinstance(msg.get("reply"), dict) else {}
            self._append_structured_message_blocks(
                lines,
                content=_safe_text(msg.get("content", "")),
                reply_message_id=reply.get("message_id"),
                reply_author_name=reply.get("author_name") or reply.get("author_id"),
                reply_preview=reply.get("preview"),
                embeds=msg.get("embeds") or [],
                stickers=msg.get("stickers") or [],
                attachments=msg.get("attachments") or [],
                indent="  ",
            )

        latest = folded[-1]
        max_seq = max(int(e.get("seq", 0)) for e in events)
        metadata: dict[str, Any] = {
            "channel_id": channel_id,
            "guild_id": entry.get("guild_id"),
            "message_id": latest.get("message_id"),
            "author_id": latest.get("author_id"),
            "source": source,
            "review_turn": True,
            "evict_if_noop": True,
            "batch_seq_from": after_seq + 1,
            "batch_seq_to": max_seq,
        }
        # DM channels need reply routing metadata
        if not entry.get("guild_id"):
            metadata["is_dm"] = True
            metadata["reply_to"] = (
                entry.get("dm_peer_user_id") or latest.get("author_id")
            )
        inbound = InboundMessage(
            channel="discord",
            content="\n".join(lines).strip(),
            priority=self.priority,
            sender=alias,
            metadata=metadata,
            timestamp=_parse_iso(str(latest.get("timestamp") or "")) or tz_now(),
        )
        self._agent.enqueue(inbound)
        self._history.mark_reviewed(channel_id, seq=max_seq, immediate=immediate)

    def _append_image_hint_only(self, lines: list[str], snapshot: dict[str, Any], *, is_dm: bool) -> None:
        atts = snapshot.get("attachments") or []
        if not atts:
            return
        if self._config.auto_read_images and (
            (is_dm and self._config.auto_read_images_in_dm)
            or (not is_dm and self._config.auto_read_images_in_guild)
        ):
            if any(att.get("needs_summary") and att.get("local_path") for att in atts):
                lines.append(
                    "[Discord image hint] Images are likely important. "
                    "Use read_image_by_subagent/read_image on local paths before replying if relevant."
                )

    # -- Presence ----------------------------------------------------

    def _mark_presence_active(self) -> None:
        self._presence_last_active_monotonic = time.monotonic()

    async def _presence_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                await self._refresh_presence_once()
                await asyncio.sleep(self._config.presence_refresh_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Discord presence loop failed", exc_info=True)

    async def _refresh_presence_once(self) -> None:
        if not self._client_ready.is_set():
            return
        mode = self._config.presence_mode
        if mode == "off":
            return

        desired = "online"
        # In auto mode we only bump to online. We intentionally do not force
        # idle/offline transitions to avoid extra presence churn and conflicts
        # with other Discord sessions. Discord can decide idle state itself.

        if desired == self._presence_last_status:
            return
        await self._apply_presence_status(desired)

    async def _apply_presence_status(self, status_name: str) -> None:
        client = self._client
        if client is None:
            return
        change_presence = getattr(client, "change_presence", None)
        if not callable(change_presence):
            return
        status_obj = None
        try:
            import discord  # type: ignore
        except Exception:
            discord = None  # type: ignore[assignment]
        if discord is not None:
            status_enum = getattr(discord, "Status", None)
            if status_enum is not None:
                status_obj = getattr(status_enum, status_name, None)
        try:
            if status_obj is None:
                await change_presence()
            else:
                await change_presence(status=status_obj)
            self._presence_last_status = status_name
        except Exception:
            logger.debug("Discord change_presence(%s) failed", status_name, exc_info=True)

    # -- Startup catchup -----------------------------------------------

    async def _startup_catchup(self) -> None:
        """Recover missed messages after restart/reconnect.

        Part A: Enqueue unreviewed DM events from history (covers unflushed
                debounce buffers lost to crash or non-graceful shutdown).
        Part B: Fetch messages via REST API that arrived while process was down.
        """
        if self._startup_catchup_done:
            return
        self._startup_catchup_done = True

        try:
            self._catchup_unreviewed_dm_events()
        except Exception:
            logger.exception("DM startup catchup failed")

        # Brief pause to let gateway deliver any pending messages before REST
        # backfill, reducing duplicate processing.
        await asyncio.sleep(3)

        try:
            await self._backfill_missed_messages()
        except Exception:
            logger.exception("Discord message backfill failed")

    def _catchup_unreviewed_dm_events(self) -> None:
        """Enqueue unreviewed DM events from history.

        After a crash, DM messages persisted in history but never flushed from
        the debounce buffer will have seq > last_reviewed_seq.
        """
        for entry in self._history.list_registered_channels():
            if entry.get("guild_id"):
                continue  # Guild channels covered by periodic review
            channel_id = str(entry.get("channel_id") or "")
            if not channel_id:
                continue
            cursor = self._history.get_cursor(channel_id)
            last_reviewed = int(cursor.get("last_reviewed_seq", 0))
            if last_reviewed <= 0:
                # First deploy with review tracking -- set baseline seq so we
                # do not replay the entire DM history.
                next_seq = int(cursor.get("next_event_seq", 1))
                if next_seq > 1:
                    self._history.mark_reviewed(
                        channel_id, seq=next_seq - 1, immediate=True,
                    )
                continue
            events = self._history.get_events_after_seq(channel_id, last_reviewed)
            own_id = self._self_user_id
            if own_id:
                events = [
                    e for e in events
                    if str(e.get("author_id", "")) != own_id
                ]
            if not events:
                continue
            logger.info(
                "DM startup catchup: ch=%s, %d unreviewed events",
                channel_id, len(events),
            )
            self._enqueue_review_from_history(
                channel_id, source="dm_startup_catchup", immediate=True,
            )

    async def _backfill_missed_messages(self) -> None:
        """Fetch messages via REST API that arrived while process was down.

        Discord gateway does not replay missed messages on reconnect, so we
        use channel.history(after=last_seen_message_id) to backfill.
        """
        client = self._client
        if client is None:
            return
        for entry in self._history.list_registered_channels():
            channel_id = str(entry.get("channel_id") or "")
            if not channel_id:
                continue
            cursor = self._history.get_cursor(channel_id)
            last_seen_id = cursor.get("last_seen_message_id")
            if not last_seen_id:
                continue
            try:
                channel = await self._resolve_channel(channel_id)
                if channel is None:
                    continue
                history_fn = getattr(channel, "history", None)
                if not callable(history_fn):
                    continue
                try:
                    import discord as _discord  # type: ignore
                    after_obj = _discord.Object(id=int(last_seen_id))
                except Exception:
                    continue
                messages: list[Any] = []
                async for msg in history_fn(after=after_obj, limit=100):
                    messages.append(msg)
                if not messages:
                    continue
                messages.sort(key=lambda m: int(getattr(m, "id", 0)))
                logger.info(
                    "Discord backfill: ch=%s, %d missed messages",
                    channel_id, len(messages),
                )
                for msg in messages:
                    # Skip if gateway already delivered this message
                    cur = self._history.get_cursor(channel_id)
                    cur_last = cur.get("last_seen_message_id")
                    msg_id = str(getattr(msg, "id", ""))
                    if cur_last and msg_id:
                        try:
                            if int(msg_id) <= int(cur_last):
                                continue
                        except (ValueError, TypeError):
                            pass
                    await self._handle_message(msg)
            except Exception:
                logger.debug(
                    "Discord backfill failed: ch=%s", channel_id,
                    exc_info=True,
                )

    # -- Send-phase typing ---------------------------------------------

    async def _stop_thinking_typing(self) -> None:
        """Cancel any lingering typing task (safety net for send-phase)."""
        if self._typing_task is None:
            return
        task = self._typing_task
        self._typing_task = None
        task.cancel()
        try:
            await task
        except BaseException:
            pass

    async def _send_typing_once(self, channel: Any) -> None:
        trigger = getattr(channel, "trigger_typing", None)
        if callable(trigger):
            await trigger()
            return
        typing_cm = getattr(channel, "typing", None)
        if callable(typing_cm):
            cm = typing_cm()
            aenter = getattr(cm, "__aenter__", None)
            aexit = getattr(cm, "__aexit__", None)
            if callable(aenter) and callable(aexit):
                await aenter()
                await aexit(None, None, None)
