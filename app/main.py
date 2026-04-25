"""Prym Signals entrypoint.

    python -m app.main
"""
from __future__ import annotations

import asyncio

from app.core.orchestrator import Orchestrator
from app.utils.logger import configure_logging, get_logger


async def _run() -> None:
    configure_logging()
    logger = get_logger(__name__)
    orchestrator = Orchestrator()
    try:
        await orchestrator.start()
    except KeyboardInterrupt:
        logger.info("keyboard_interrupt")
    finally:
        await orchestrator.shutdown()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
