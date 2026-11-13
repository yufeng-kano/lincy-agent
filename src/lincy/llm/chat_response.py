"""Out-of-band report of the parsed response behind a plain ``chat()`` call.

``chat()`` returns text only, so the session debug wrapper cannot read token
usage or the upstream a gateway named from its return value. Providers that
parse a full response report it here; the wrapper scopes each call with
``observe_chat_response()`` and records whatever was reported.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .schema import LLMResponse

class ChatResponseProbe:
    """Holds the response reported by the chat() call running in this scope."""

    def __init__(self) -> None:
        self._response: LLMResponse | None = None

    def get(self) -> LLMResponse | None:
        return self._response


# Holds the active probe, not the response, so a chat() call outside any
# observed scope writes nothing and leaves no response pinned to the thread.
_ACTIVE_PROBE: ContextVar[ChatResponseProbe | None] = ContextVar(
    "llm_chat_response_probe",
    default=None,
)


def report_chat_response(response: LLMResponse) -> None:
    """Called by a provider's ``chat()`` right before it returns the text."""
    probe = _ACTIVE_PROBE.get()
    if probe is not None:
        probe._response = response


@contextmanager
def observe_chat_response() -> Iterator[ChatResponseProbe]:
    """Scope one chat() call; ``get()`` is None when the provider reports nothing."""
    probe = ChatResponseProbe()
    token = _ACTIVE_PROBE.set(probe)
    try:
        yield probe
    finally:
        _ACTIVE_PROBE.reset(token)
