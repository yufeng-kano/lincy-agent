"""discord_listen: the agent's own control over which Discord channels it follows.

Which channels to follow is the agent's decision. The tool deliberately has no
permission check: a request from anyone, the owner included, is input the agent
weighs, not an order the code enforces. See docs/dev/discord-channels.md.
"""

from __future__ import annotations

from collections.abc import Callable
import json
from typing import TYPE_CHECKING

from ...llm.schema import ToolDefinition, ToolParameter
from .get_channel_history import resolve_contact_channel_id

if TYPE_CHECKING:
    from ...agent.contact_map import ContactMap
    from ...agent.discord_history import DiscordHistoryStore
    from ...agent.turn_context import TurnContext


_FILTERS = ["all", "mention_only", "mute"]

DISCORD_LISTEN_DEFINITION = ToolDefinition(
    name="discord_listen",
    description=(
        "List the Discord server channels you follow, or change how closely you follow one. "
        "This is your own decision: treat anyone's request, the owner's included, as input you "
        "may accept or decline. Filters: 'all' stores every message and sends you a batch about "
        "once a minute while people talk (each batch costs a turn even when you stay silent); "
        "'mention_only' stores only messages that tag you, plus the messages just before each "
        "tag as context; 'mute' ignores the channel, tags included. A channel appears here after "
        "someone tags you there once. DMs are not covered."
    ),
    parameters={
        "action": ToolParameter(
            type="string",
            description="'list' shows every followed server channel; 'set' changes one channel's filter.",
            enum=["list", "set"],
        ),
        "filter": ToolParameter(
            type="string",
            description="New filter for action='set'.",
            enum=_FILTERS,
        ),
        "to": ToolParameter(
            type="string",
            description="Channel alias such as '#general @ Guild'. Optional if 'channel_id' is given or you are in that channel now.",
        ),
        "channel_id": ToolParameter(
            type="string",
            description="Explicit channel id. Takes precedence over 'to'.",
        ),
    },
    required=["action"],
)


def create_discord_listen(
    history_store: "DiscordHistoryStore",
    contact_map: "ContactMap",
    turn_context: "TurnContext",
    allowlist_ids: frozenset[str] = frozenset(),
) -> Callable[..., str]:
    """Create discord_listen bound to the Discord registry.

    allowlist_ids is channels.discord.listen_channels: when non-empty the
    adapter drops every other channel, so the tool must not pretend a filter
    change there has any effect.
    """

    def blocked(channel_id: str) -> bool:
        return bool(allowlist_ids) and channel_id not in allowlist_ids

    def effective(filter_mode: object) -> str:
        # Unknown stored values (the old from_contacts) behave as mention_only.
        return filter_mode if filter_mode in _FILTERS else "mention_only"

    def discord_listen(
        action: str,
        filter: str | None = None,
        to: str | None = None,
        channel_id: str | None = None,
    ) -> str:
        guild_entries = [
            e for e in history_store.list_registered_channels() if e.get("guild_id")
        ]

        if action == "list":
            channels = sorted(
                (
                    {
                        "channel_id": e.get("channel_id"),
                        "alias": e.get("alias"),
                        "filter": effective(e.get("filter")),
                        "source": e.get("source"),
                        "last_seen_at": e.get("last_seen_at"),
                        "blocked_by_config": blocked(str(e.get("channel_id") or "")),
                    }
                    for e in guild_entries
                ),
                key=lambda c: str(c["alias"] or ""),
            )
            return json.dumps({"channels": channels}, ensure_ascii=False)

        if action != "set":
            return "Error: action must be 'list' or 'set'"
        if filter not in _FILTERS:
            return f"Error: filter must be one of {', '.join(_FILTERS)}"

        target_id = channel_id
        if target_id is None and to:
            target_id = resolve_contact_channel_id(history_store, contact_map, to)
            if target_id is None:
                known = sorted({str(e.get("alias") or "") for e in guild_entries} - {""})
                hint = f" Known channels: {', '.join(known[:20])}." if known else ""
                return f"Error: no followed Discord channel matches '{to}'.{hint}"
        if (
            target_id is None
            and turn_context.channel == "discord"
            and isinstance(turn_context.metadata, dict)
        ):
            current = turn_context.metadata.get("channel_id")
            if isinstance(current, str) and current:
                target_id = current
        if target_id is None:
            return "Error: provide 'to' or 'channel_id' (or call from a Discord server channel)"

        entry = history_store.get_channel_entry(target_id)
        if entry is not None and not entry.get("guild_id"):
            return f"Error: '{to or target_id}' is a DM; discord_listen covers server channels only."
        if blocked(target_id):
            return (
                f"Error: '{to or target_id}' is outside channels.discord.listen_channels, "
                "so nothing from it reaches you whatever its filter."
            )
        if entry is None:
            return (
                f"Error: '{to or target_id}' is not a server channel you follow yet. "
                "Someone has to tag you there once first."
            )

        updated = history_store.set_channel_filter(target_id, filter)
        if updated is None:
            return f"Error: '{target_id}' is no longer registered"
        return json.dumps(
            {
                "channel_id": target_id,
                "alias": updated.get("alias"),
                "previous_filter": effective(entry.get("filter")),
                "filter": updated.get("filter"),
            },
            ensure_ascii=False,
        )

    return discord_listen
