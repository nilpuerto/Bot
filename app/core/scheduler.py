"""Tiny periodic-task runner.

Used for background chores like pruning ``news_seen``.  Not a replacement
for APScheduler — just a couple of functions so we don't pull in a full
scheduler for the MVP.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable


async def run_periodic(
    fn: Callable[[], Awaitable[None]],
    *,
    interval_seconds: int,
    stop_event: asyncio.Event,
    name: str = "task",
) -> None:
    from app.utils.logger import get_logger

    logger = get_logger(__name__)
    while not stop_event.is_set():
        try:
            await fn()
        except Exception as exc:  # defensive
            logger.exception("periodic_task_error", name=name, error=str(exc))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
