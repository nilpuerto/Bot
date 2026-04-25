"""Apply ``database.sql`` against the configured PostgreSQL instance.

Usage::

    python -m scripts.init_db
"""
from __future__ import annotations

import asyncio

from app.database.engine import dispose_engine
from app.database.migrations_runner import apply_schema
from app.utils.logger import configure_logging, get_logger


async def main() -> None:
    configure_logging()
    logger = get_logger(__name__)
    try:
        await apply_schema()
        logger.info("init_db_done")
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
