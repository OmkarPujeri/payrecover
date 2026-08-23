"""Server-Sent Events (SSE) manager.

A tiny in-process pub/sub used to push real-time updates (new failures,
agent steps, executions, circuit breakers) to connected dashboards. SSE is
one-way server->client which is all the UI needs — see PRD design decision #8.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any


class SSEManager:
    def __init__(self) -> None:
        self._connections: list[asyncio.Queue] = []

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def connect(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._connections.append(queue)
        return queue

    def disconnect(self, queue: asyncio.Queue) -> None:
        if queue in self._connections:
            self._connections.remove(queue)

    async def broadcast(self, data: dict[str, Any]) -> None:
        """Fan a JSON-serialisable payload out to every connected client."""
        payload = {"ts": datetime.now(timezone.utc).isoformat(), **data}
        for queue in list(self._connections):
            await queue.put(payload)

    @staticmethod
    def format(payload: dict[str, Any]) -> str:
        """Render a payload as an SSE ``data:`` frame."""
        return f"data: {json.dumps(payload, default=str)}\n\n"


# App-wide singleton.
sse_manager = SSEManager()
