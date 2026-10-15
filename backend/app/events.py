from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class EventBus:
    """Fans app events out to every connected browser socket."""

    def __init__(self) -> None:
        self._sockets: dict[int, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    @property
    def online_user_ids(self) -> list[int]:
        return [uid for uid, sockets in self._sockets.items() if sockets]

    async def connect(self, user_id: int, ws: WebSocket) -> None:
        async with self._lock:
            first = not self._sockets[user_id]
            self._sockets[user_id].add(ws)
        if first:
            await self.publish({"type": "presence", "user_id": user_id, "online": True})

    async def disconnect(self, user_id: int, ws: WebSocket) -> None:
        async with self._lock:
            self._sockets[user_id].discard(ws)
            last = not self._sockets[user_id]
        if last:
            await self.publish({"type": "presence", "user_id": user_id, "online": False})

    async def publish(self, event: dict[str, Any]) -> None:
        async with self._lock:
            targets = [ws for sockets in self._sockets.values() for ws in sockets]
        results = await asyncio.gather(
            *(ws.send_json(event) for ws in targets), return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                logger.debug("Dropped event for a closed socket: %s", result)


bus = EventBus()
