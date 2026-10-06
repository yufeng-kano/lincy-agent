from .console import ConsoleAdapter
from .discord import DiscordAdapter
from .formatting import markdown_to_plaintext
from .gmail import GmailAdapter
from .protocol import ChannelAdapter
from .scheduler import SchedulerAdapter

__all__ = [
    "ChannelAdapter",
    "ConsoleAdapter",
    "DiscordAdapter",
    "GmailAdapter",
    "SchedulerAdapter",
    "markdown_to_plaintext",
]
