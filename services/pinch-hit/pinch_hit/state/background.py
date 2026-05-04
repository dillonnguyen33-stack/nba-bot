import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task[None]] = set()


def schedule_background(coro: Coroutine[Any, Any, None], label: str) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _on_task_done(done: asyncio.Task[None]) -> None:
        _background_tasks.discard(done)
        if not done.cancelled() and done.exception():
            logger.error("%s background task failed", label, exc_info=done.exception())

    task.add_done_callback(_on_task_done)


async def drain_background_tasks(timeout: float = 5.0) -> None:
    if not _background_tasks:
        return
    for task in list(_background_tasks):
        task.cancel()
    _, pending = await asyncio.wait(_background_tasks, timeout=timeout)
    if pending:
        logger.warning("%d background task(s) did not finish within %.1fs", len(pending), timeout)
