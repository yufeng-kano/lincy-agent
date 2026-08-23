"""Shared helpers for optional send_message batch-planning guidance."""

from __future__ import annotations

import re

from .workspace.prompt_resolver import OptionalKernelPromptFragment

SYSTEM_PROMPT_PLACEHOLDER = "{send_message_batch_guidance}"
SYSTEM_PROMPT_FRAGMENT_PATH = (
    "agents/brain/prompts/fragments/send-message-batch-guidance.md"
)
_LEGACY_SYSTEM_PROMPT_PATTERN = re.compile(
    r"\n(?:\*\*\u591a\u5247\u8a0a\u606f\*\*\uff1a|\*\*Multi-message sends\*\*:)"
    r".*?(?=\n\n#### |\n\n### |\n\n## |\Z)",
    re.DOTALL,
)

_SEND_MESSAGE_DESCRIPTION_BASE = (
    "Send a message to a channel. This is the ONLY way to deliver "
    "messages to users. Text output without this tool is inner "
    "thoughts visible only on the operator console."
)
_SEND_MESSAGE_DESCRIPTION_BATCH = (
    " To send multiple messages, include all send_message calls in "
    "one response instead of splitting across rounds."
)

_DISCORD_REMINDER_BASE = (
    "(Discord: read builtin skill discord-messaging before channel-specific "
    "formatting; DM messages should usually stay single-line, but a closing "
    "emoji/kaomoji should go on its own final line instead of inline"
)
_DISCORD_REMINDER_BATCH = (
    "; schedules/reminders should be split into multiple one-line "
    "send_message calls only when the points are truly distinct; if several "
    "lines serve the same ask or same immediate action, merge them into one "
    "message, and each split message should add a distinct point)"
)
_GMAIL_REMINDER_BASE = "(one send_message = one email"
_GMAIL_REMINDER_BATCH = "; do NOT split into multiple calls)"

def build_prompt_fragment_spec(
    *,
    enabled: bool,
) -> OptionalKernelPromptFragment:
    """Build the system-prompt fragment spec for this guidance policy."""
    return OptionalKernelPromptFragment(
        placeholder=SYSTEM_PROMPT_PLACEHOLDER,
        kernel_rel_path=SYSTEM_PROMPT_FRAGMENT_PATH,
        enabled=enabled,
        legacy_patterns=(_LEGACY_SYSTEM_PROMPT_PATTERN,),
    )


def build_tool_description(*, enabled: bool) -> str:
    """Build send_message tool description with optional batch guidance."""
    if enabled:
        return _SEND_MESSAGE_DESCRIPTION_BASE + _SEND_MESSAGE_DESCRIPTION_BATCH
    return _SEND_MESSAGE_DESCRIPTION_BASE


def build_channel_reminders(*, enabled: bool) -> dict[str, str]:
    """Build per-channel reminders with optional batch guidance."""
    reminders = {
        "discord": _DISCORD_REMINDER_BASE + ")",
        "gmail": _GMAIL_REMINDER_BASE + ")",
    }
    if enabled:
        reminders["discord"] = _DISCORD_REMINDER_BASE + _DISCORD_REMINDER_BATCH
        reminders["gmail"] = _GMAIL_REMINDER_BASE + _GMAIL_REMINDER_BATCH
    return reminders


