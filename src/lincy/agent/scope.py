"""Conversation scope resolution for time-anchored common ground."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .schema import InboundMessage


def _norm(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _norm_email(value: Any) -> str | None:
    text = _norm(value)
    if text is None:
        return None
    return text.lower()


def _discord_inbound_scope(msg: InboundMessage) -> str | None:
    meta = msg.metadata or {}
    channel_id = _norm(meta.get("channel_id"))
    is_dm = bool(meta.get("is_dm"))
    if is_dm:
        user_id = _norm(meta.get("author_id")) or _norm(meta.get("reply_to")) or _norm(msg.sender)
        if user_id is None:
            return None
        return f"discord:dm:{user_id}"
    if channel_id is not None:
        return f"discord:channel:{channel_id}"
    return None


def _gmail_inbound_scope(msg: InboundMessage) -> str | None:
    meta = msg.metadata or {}
    thread_id = _norm(meta.get("thread_id"))
    if thread_id is not None:
        return f"gmail:thread:{thread_id}"
    reply_to = _norm_email(meta.get("reply_to")) or _norm_email(msg.sender)
    if reply_to is not None:
        return f"gmail:sender:{reply_to}"
    return None


def _line_inbound_scope(msg: InboundMessage) -> str | None:
    meta = msg.metadata or {}
    contact = _norm(meta.get("reply_to")) or _norm(msg.sender)
    if contact is None:
        return None
    return f"line:chat:{contact}"


def scope_for_inbound(msg: InboundMessage) -> str | None:
    """Resolve a stable common-ground scope for an inbound queue message."""
    channel = _norm(msg.channel)
    if channel in (None, "system", "cli"):
        return None
    if channel == "discord":
        return _discord_inbound_scope(msg)
    if channel == "gmail":
        return _gmail_inbound_scope(msg)
    if channel == "line":
        return _line_inbound_scope(msg)
    return None


def scope_for_send_message_call(
    *,
    channel: str,
    to: str | None,
    metadata: dict[str, Any],
    inbound_channel: str | None,
    inbound_sender: str | None,
    inbound_metadata: dict[str, Any] | None,
) -> str | None:
    """Resolve scope for a send_message call using outbound args + current turn context."""
    ch = _norm(channel)
    if ch in (None, "system", "cli"):
        return None

    inbound_meta = inbound_metadata or {}

    if ch == "gmail":
        thread_id = _norm(metadata.get("thread_id"))
        if thread_id is not None:
            return f"gmail:thread:{thread_id}"
        reply_to = _norm_email(metadata.get("reply_to"))
        if reply_to is not None:
            return f"gmail:sender:{reply_to}"
        # Reply-mode fallback for old data where send metadata is sparse
        if inbound_channel == "gmail":
            reply_to = _norm_email(inbound_meta.get("reply_to")) or _norm_email(inbound_sender)
            if reply_to is not None:
                return f"gmail:sender:{reply_to}"
        return None

    if ch == "discord":
        if bool(metadata.get("is_dm")):
            dm_user = _norm(metadata.get("author_id")) or _norm(metadata.get("reply_to"))
            if dm_user is not None:
                return f"discord:dm:{dm_user}"

        dm_user = _norm(metadata.get("reply_to"))
        if dm_user is not None:
            return f"discord:dm:{dm_user}"

        if _norm(metadata.get("channel_id")):
            return f"discord:channel:{_norm(metadata.get('channel_id'))}"

        # Reply mode inherits inbound metadata; prefer exact channel type if available.
        if inbound_channel == "discord":
            is_dm = bool(inbound_meta.get("is_dm"))
            if is_dm:
                dm_user = (
                    _norm(inbound_meta.get("author_id"))
                    or _norm(inbound_meta.get("reply_to"))
                    or _norm(inbound_sender)
                )
                if dm_user is not None:
                    return f"discord:dm:{dm_user}"
            ch_id = _norm(inbound_meta.get("channel_id"))
            if ch_id is not None:
                return f"discord:channel:{ch_id}"
        # Proactive DM fallback (contact alias only)
        if to is not None:
            return f"discord:dm:{to.strip()}"
        return None

    if ch == "line":
        contact = _norm(metadata.get("reply_to"))
        if contact is None and inbound_channel == "line":
            contact = _norm(inbound_meta.get("reply_to")) or _norm(inbound_sender)
        if contact is None and to is not None:
            contact = _norm(to)
        if contact is None:
            return None
        return f"line:chat:{contact}"

    return None


@dataclass(frozen=True)
class ScopeResolver:
    """Injectable wrapper for scope resolution rules."""

    def inbound(self, msg: InboundMessage) -> str | None:
        return scope_for_inbound(msg)

    def outbound(
        self,
        *,
        channel: str,
        to: str | None,
        metadata: dict[str, Any],
        inbound_channel: str | None,
        inbound_sender: str | None,
        inbound_metadata: dict[str, Any] | None,
    ) -> str | None:
        return scope_for_send_message_call(
            channel=channel,
            to=to,
            metadata=metadata,
            inbound_channel=inbound_channel,
            inbound_sender=inbound_sender,
            inbound_metadata=inbound_metadata,
        )


DEFAULT_SCOPE_RESOLVER = ScopeResolver()
