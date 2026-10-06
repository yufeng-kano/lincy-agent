"""AgentHandle: the only surface the host uses to drive the agent."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol


class AgentState(StrEnum):
    STARTING = "starting"
    READY = "ready"  # no turn in flight
    BUSY = "busy"  # a turn is in flight
    STOPPING = "stopping"


class ExitReason(StrEnum):
    SHUTDOWN = "shutdown"
    RESTART = "restart"


class AgentError(Exception):
    """Base error surfaced to the host; status_code maps onto the HTTP response."""

    status_code: int = 500


class AgentBusy(AgentError):
    status_code = 409


class InvalidRequest(AgentError):
    status_code = 400


class UnsupportedChannel(AgentError):
    status_code = 400


class ShellSessionNotFound(AgentError):
    status_code = 404


class AgentHandle(Protocol):
    def state(self) -> AgentState: ...
    def session_id(self) -> str | None: ...
    def channels(self) -> list[str]: ...
    def submit(self, content: str, channel: str = "cli") -> None: ...
    def cancel_turn(self) -> None: ...
    def request_new_session(self) -> None: ...
    def request_compact(self) -> None: ...
    def request_clear(self) -> None: ...
    def request_reload(self, target: Literal["all", "system-prompt"]) -> None: ...
    def request_shutdown(self, *, graceful: bool = True) -> None: ...
    def request_restart(self) -> None: ...
    def token_status(self) -> str: ...
    def shell_sessions(self) -> list[dict]: ...
    def shell_send(self, session_id: str, *, text: str | None, key: str | None) -> str: ...
    def shell_cancel(self, session_id: str) -> str: ...
