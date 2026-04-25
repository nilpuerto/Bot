"""Apply ``database.sql`` against the configured PostgreSQL instance.

A tiny home-grown runner so newcomers can spin up the DB with a single
``python -m scripts.init_db`` without needing Alembic.  For real migrations
in production, prefer Alembic.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from app.config.settings import PROJECT_ROOT
from app.database.engine import get_engine
from app.utils.logger import get_logger


logger = get_logger(__name__)


SCHEMA_PATH = PROJECT_ROOT / "database.sql"


async def apply_schema(path: Path = SCHEMA_PATH) -> None:
    """Execute the schema SQL script.  Safe to run multiple times.

    ``asyncpg`` wraps every statement in a server-side prepared
    statement, which rejects scripts that contain more than one command.
    We sidestep that by grabbing the raw asyncpg connection and using
    its ``execute()`` method — which accepts multi-statement SQL — once
    per DO/BEGIN/COMMIT block.  The whole thing runs inside an
    ``engine.begin()`` so any failure rolls back cleanly.
    """
    if not path.exists():
        raise FileNotFoundError(f"Schema file not found: {path}")

    sql = path.read_text(encoding="utf-8")
    engine = get_engine()
    async with engine.begin() as conn:
        # ``connection`` here is the SQLAlchemy AsyncAdapter; reach
        # through to the raw asyncpg connection so we can run the full
        # script in one shot without prepared-statement wrapping.
        raw = await conn.get_raw_connection()
        asyncpg_conn = raw.driver_connection
        await asyncpg_conn.execute(sql)
    logger.info("schema_applied", file=str(path))
