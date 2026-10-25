"""In-process publish/subscribe for live page updates, and a tracker for background jobs."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger("subzero.live")


class Broker:
    def __init__(self, queue_size: int = 2000):
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self.queue_size = queue_size

    def subscribe(self, topic: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.queue_size)
        self._subs[topic].add(queue)
        return queue

    def unsubscribe(self, topic: str, queue: asyncio.Queue) -> None:
        subs = self._subs.get(topic)
        if subs:
            subs.discard(queue)
            if not subs:
                self._subs.pop(topic, None)

    def publish(self, topic: str, event: dict[str, Any]) -> None:
        for queue in list(self._subs.get(topic, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("Dropping a slow live-update listener on %s", topic)
                self.unsubscribe(topic, queue)

    def subscriber_count(self, topic: str) -> int:
        return len(self._subs.get(topic, ()))


class Tasks:
    """Runs background jobs, keeps references so they aren't garbage-collected, and logs failures."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro: Coroutine, name: str = "job") -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("Background job %s failed", task.get_name(), exc_info=task.exception())

    async def wait_idle(self, timeout: float = 10.0) -> None:
        """Wait until no background jobs are running (used by tests)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._tasks:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("Background jobs still running")
            await asyncio.wait(set(self._tasks), timeout=remaining)

    async def cancel_all(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
