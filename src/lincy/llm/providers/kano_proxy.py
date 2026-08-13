"""Kano Proxy provider client.

Kano Proxy is modeled as an Anthropic-compatible gateway. The concrete client
keeps a distinct provider entry point while reusing the native Anthropic
Messages adapter implementation.
"""

from ...core.schema import KanoProxyConfig
from .anthropic import AnthropicClient


class KanoProxyClient(AnthropicClient):
    """Anthropic Messages API client pointed at the Kano Proxy gateway."""

    def __init__(self, config: KanoProxyConfig):
        super().__init__(config)
