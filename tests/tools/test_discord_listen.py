"""Tests for the discord_listen tool."""

import json

from lincy.agent.contact_map import ContactMap
from lincy.agent.discord_history import DiscordHistoryStore
from lincy.agent.turn_context import TurnContext
from lincy.tools.builtin.discord_listen import DISCORD_LISTEN_DEFINITION, create_discord_listen


def _make_tool(tmp_path):
    contact_map = ContactMap(tmp_path / "cache")
    history = DiscordHistoryStore(tmp_path / "cache")
    turn_context = TurnContext()
    fn = create_discord_listen(history, contact_map, turn_context)
    return fn, history, contact_map, turn_context


def _register(history, channel_id, alias, filter_mode, guild_id="g1"):
    history.upsert_channel(
        channel_id=channel_id,
        guild_id=guild_id,
        guild_name="Guild" if guild_id else None,
        channel_name=alias,
        alias=alias,
        filter_mode=filter_mode,
        source="auto_mention" if guild_id else "dm",
        review_interval_seconds=60,
    )


def test_definition_has_no_permission_parameter():
    assert DISCORD_LISTEN_DEFINITION.name == "discord_listen"
    assert set(DISCORD_LISTEN_DEFINITION.parameters) == {"action", "filter", "to", "channel_id"}
    assert DISCORD_LISTEN_DEFINITION.parameters["filter"].enum == ["all", "mention_only", "mute"]


def test_list_shows_server_channels_only_sorted_by_alias(tmp_path):
    fn, history, _, _ = _make_tool(tmp_path)
    _register(history, "2", "#lounge @ Guild", "all")
    _register(history, "1", "#abyss @ Guild", "all")
    _register(history, "9", "Alice", "all", guild_id=None)

    payload = json.loads(fn(action="list"))

    assert [c["alias"] for c in payload["channels"]] == ["#abyss @ Guild", "#lounge @ Guild"]
    assert payload["channels"][0] == {
        "channel_id": "1",
        "alias": "#abyss @ Guild",
        "filter": "all",
        "source": "auto_mention",
        "last_seen_at": None,
    }


def test_set_by_alias(tmp_path):
    fn, history, contact_map, _ = _make_tool(tmp_path)
    _register(history, "1", "#abyss @ Guild", "all")
    contact_map.update("discord", "1", "#abyss @ Guild")

    payload = json.loads(fn(action="set", filter="mention_only", to="#abyss @ Guild"))

    assert payload == {
        "channel_id": "1",
        "alias": "#abyss @ Guild",
        "previous_filter": "all",
        "filter": "mention_only",
    }
    assert history.get_channel_entry("1")["filter"] == "mention_only"


def test_set_defaults_to_current_discord_channel(tmp_path):
    fn, history, _, turn_context = _make_tool(tmp_path)
    _register(history, "1", "#abyss @ Guild", "mention_only")
    turn_context.channel = "discord"
    turn_context.metadata = {"channel_id": "1"}

    payload = json.loads(fn(action="set", filter="mute"))

    assert payload["filter"] == "mute"
    assert history.get_channel_entry("1")["filter"] == "mute"


def test_set_works_for_any_requester(tmp_path):
    # No owner gate: whoever asked, the call is the agent's own decision.
    fn, history, _, turn_context = _make_tool(tmp_path)
    _register(history, "1", "#abyss @ Guild", "mention_only")
    turn_context.channel = "discord"
    turn_context.metadata = {"channel_id": "1", "author_id": "someone-else"}

    payload = json.loads(fn(action="set", filter="all"))

    assert payload["filter"] == "all"


def test_set_rejects_unregistered_channel_and_dm(tmp_path):
    fn, history, _, _ = _make_tool(tmp_path)
    _register(history, "9", "Alice", "all", guild_id=None)

    assert fn(action="set", filter="all", channel_id="404").startswith("Error:")
    assert fn(action="set", filter="mute", channel_id="9").startswith("Error:")
    assert history.get_channel_entry("9")["filter"] == "all"


def test_set_rejects_unknown_alias_with_known_targets(tmp_path):
    fn, history, _, _ = _make_tool(tmp_path)
    _register(history, "1", "#abyss @ Guild", "all")

    result = fn(action="set", filter="mute", to="#nowhere @ Guild")

    assert result.startswith("Error:")
    assert "#abyss @ Guild" in result


def test_set_needs_a_target_outside_discord(tmp_path):
    fn, history, _, _ = _make_tool(tmp_path)
    _register(history, "1", "#abyss @ Guild", "all")

    assert fn(action="set", filter="mute").startswith("Error:")


def test_bad_action_and_filter(tmp_path):
    fn, history, _, _ = _make_tool(tmp_path)
    _register(history, "1", "#abyss @ Guild", "all")

    assert fn(action="remove").startswith("Error:")
    assert fn(action="set", filter="from_contacts", channel_id="1").startswith("Error:")
    assert fn(action="set", channel_id="1").startswith("Error:")
    assert history.get_channel_entry("1")["filter"] == "all"
