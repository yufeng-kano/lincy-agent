"""Keep cache routing stable within a conversation or task."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
from uuid import uuid4


_SESSION_KEY: ContextVar[str | None] = ContextVar("llm_session_key", default=None)


def current_llm_session_key() -> str | None:
    return _SESSION_KEY.get()


@contextmanager
def llm_session(agent: str, session_id: str | None = None) -> Iterator[None]:
    """Reuse a stored session ID, or create one for this task invocation.

    May also decorate synchronous task entry points. Each invocation gets
    its own UUID; nested tasks restore the caller's key even on failure.
    """
    identity = session_id or str(uuid4())
    key = sha256(f"{agent}:{identity}".encode()).hexdigest()
    token = _SESSION_KEY.set(key)
    try:
        yield
    finally:
        _SESSION_KEY.reset(token)
