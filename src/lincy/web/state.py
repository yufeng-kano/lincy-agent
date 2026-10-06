"""Runtime state shared between the web lifespan and the web router."""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import WebSocket

from .cache import MetricsCache


class _WebSocketManager:
    """Tracks connected WebSocket clients and broadcasts messages."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, message: dict) -> None:
        dead: list[WebSocket] = []
        for ws in self._clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)


@dataclass
class WebState:
    # None until web_lifespan finishes loading pricing and sessions.
    cache: MetricsCache | None = None
    ws: _WebSocketManager = field(default_factory=_WebSocketManager)
