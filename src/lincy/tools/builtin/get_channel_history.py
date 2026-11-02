"""Channel history lookup tool (generic interface, Discord backend first)."""

from __future__ import annotations

from collections.abc import Callable
import json
from typing import TYPE_CHECKING

from ...llm.schema import ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from ...agent.contact_map import ContactMap
    from ...agent.discord_history import DiscordHistoryStore
    from ...agent.turn_context import TurnContext


GET_CHANNEL_HISTORY_DEFINITION = ToolDefinition(
    name="get_channel_history",
    description=(
        "Query recent message history for a channel using a generic interface. "
        "Currently only supports channel='discord'."
    ),
    parameters={
        "channel": ToolParameter(
            type="string",
            description="Channel backend name. Generic interface; v1 supports only 'discord'.",
        ),
        "to": ToolParameter(
            type="string",
            description=(
                "Target alias to resolve via ContactMap (e.g. person name or '#channel @ guild'). "
                "Optional if querying current Discord channel."
            ),
        ),
        "channel_id": ToolParameter(
            type="string",
            description="Explicit channel id. Takes precedence over 'to'.",
        ),
        "limit": ToolParameter(
            type="integer",
            description="Max messages to return after folding edits. Default 50.",
        ),
        "since_minutes": ToolParameter(
            type="integer",
            description="Only include messages newer than this many minutes.",
        ),
    },
    required=["channel"],
)


def _registered_entries(history_store: "DiscordHistoryStore") -> list[dict]:
    return [e for e in history_store.list_registered_channels() if isinstance(e, dict)]


def resolve_contact_channel_id(
    history_store: "DiscordHistoryStore",
    contact_map: "ContactMap",
    to: str,
) -> str | None:
    """Resolve a contact alias to a registered channel id.

    Contact-map entries can chain (display name -> username -> user id), so a
    single reverse lookup may land mid-chain on a value that is neither a
    channel id nor a user id. Walk the chain and match every hop against the
    registry: exact channel id first, then channel alias, then DM peer user id.
    """
    candidates = [to]
    seen = {to}
    cur: str | None = to
    while cur is not None:
        cur = contact_map.reverse_lookup("discord", cur)
        if cur is None or cur in seen:
            break
        candidates.append(cur)
        seen.add(cur)

    entries = _registered_entries(history_store)
    for cand in candidates:
        if history_store.get_channel_entry(cand) is not None:
            return cand
        for entry in entries:
            if cand in (
                str(entry.get("alias") or ""),
                str(entry.get("dm_peer_user_id") or ""),
            ):
                channel_id = str(entry.get("channel_id") or "").strip()
                if channel_id:
                    return channel_id
    return None


def known_aliases(history_store: "DiscordHistoryStore", cap: int = 20) -> str:
    aliases = sorted(
        {str(e.get("alias") or "").strip() for e in _registered_entries(history_store)}
        - {""}
    )
    if len(aliases) > cap:
        aliases = aliases[:cap] + ["..."]
    return ", ".join(aliases)


def create_get_channel_history(
    history_store: "DiscordHistoryStore",
    contact_map: "ContactMap",
    turn_context: "TurnContext",
) -> Callable[..., str]:
    """Create get_channel_history bound to runtime state."""

    def get_channel_history(
        channel: str,
        to: str | None = None,
        channel_id: str | None = None,
        limit: int = 50,
        since_minutes: int | None = None,
    ) -> str:
        if channel != "discord":
            return "Error: get_channel_history currently supports only 'discord'"

        target = to
        resolved_channel_id = channel_id
        if resolved_channel_id is None and to:
            resolved_channel_id = resolve_contact_channel_id(
                history_store, contact_map, to
            )
            if resolved_channel_id is None:
                known = known_aliases(history_store)
                hint = f" Known targets: {known}." if known else ""
                return (
                    f"Error: no discord channel/contact mapping found for '{to}'."
                    f"{hint} Use a known alias or provide channel_id."
                )
        if resolved_channel_id is None:
            if (
                turn_context.channel == "discord"
                and isinstance(turn_context.metadata, dict)
            ):
                current = turn_context.metadata.get("channel_id")
                if isinstance(current, str) and current:
                    resolved_channel_id = current
                    if target is None:
                        target = turn_context.sender
        if resolved_channel_id is None:
            return "Error: provide 'to' or 'channel_id' (or call from a Discord turn)"

        try:
            limit_i = int(limit)
        except (TypeError, ValueError):
            return "Error: limit must be an integer"
        if limit_i < 0:
            return "Error: limit must be >= 0"

        since_i: int | None = None
        if since_minutes is not None:
            try:
                since_i = int(since_minutes)
            except (TypeError, ValueError):
                return "Error: since_minutes must be an integer"
            if since_i < 0:
                return "Error: since_minutes must be >= 0"

        # Fail loudly on unknown channels: a silent empty result reads as
        # "no recent messages" and hides resolution bugs from the model.
        if (
            history_store.get_channel_entry(resolved_channel_id) is None
            and not history_store.read_events(resolved_channel_id)
        ):
            known = known_aliases(history_store)
            hint = f" Known targets: {known}." if known else ""
            return (
                f"Error: no discord history recorded for channel id "
                f"'{resolved_channel_id}'.{hint}"
            )

        payload = history_store.get_channel_history(
            resolved_channel_id,
            limit=limit_i,
            since_minutes=since_i,
            target=target or resolved_channel_id,
        )
        return json.dumps(payload, ensure_ascii=False)

    return get_channel_history
