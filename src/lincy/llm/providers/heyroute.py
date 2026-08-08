"""Heyroute provider client.

Heyroute is modeled as an Anthropic-compatible gateway. The concrete client
keeps a distinct provider entry point while reusing the native Anthropic
Messages adapter implementation.
"""

from ...core.schema import HeyrouteConfig
from .anthropic import AnthropicClient


class HeyrouteClient(AnthropicClient):
    """Anthropic Messages API client pointed at the Heyroute gateway."""

    def __init__(self, config: HeyrouteConfig):
        super().__init__(config)
