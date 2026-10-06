from lincy.agent.schema import InboundMessage
from lincy.agent.scope import DEFAULT_SCOPE_RESOLVER, scope_for_inbound


def test_scope_for_inbound_discord_dm():
    msg = InboundMessage(
        channel="discord",
        content="hi",
        priority=1,
        sender="friend",
        metadata={"is_dm": True, "author_id": "123", "channel_id": "c1"},
    )
    assert scope_for_inbound(msg) == "discord:dm:123"


def test_scope_for_inbound_discord_guild_channel():
    msg = InboundMessage(
        channel="discord",
        content="hi",
        priority=1,
        sender="guild",
        metadata={"channel_id": "987", "source": "guild_review"},
    )
    assert scope_for_inbound(msg) == "discord:channel:987"


def test_scope_for_inbound_gmail_prefers_thread():
    msg = InboundMessage(
        channel="gmail",
        content="mail",
        priority=1,
        sender="Artist",
        metadata={"thread_id": "t-1", "reply_to": "artist@example.com"},
    )
    assert scope_for_inbound(msg) == "gmail:thread:t-1"


def test_scope_for_outbound_reply_mode_uses_inbound_discord_dm():
    scope = DEFAULT_SCOPE_RESOLVER.outbound(
        channel="discord",
        to=None,
        metadata={"is_dm": True, "author_id": "42", "channel_id": "dmchan"},
        inbound_channel="discord",
        inbound_sender="friend",
        inbound_metadata={"is_dm": True, "author_id": "42", "channel_id": "dmchan"},
    )
    assert scope == "discord:dm:42"


def test_scope_for_outbound_gmail_explicit_recipient_fallback_sender_scope():
    scope = DEFAULT_SCOPE_RESOLVER.outbound(
        channel="gmail",
        to="husband",
        metadata={"reply_to": "husband@example.com"},
        inbound_channel="cli",
        inbound_sender="yufeng",
        inbound_metadata={},
    )
    assert scope == "gmail:sender:husband@example.com"

