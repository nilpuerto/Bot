"""Quick connectivity probe for the configured DATABASE_URL.

Usage::

    python -m scripts.probe_db

Prints whether asyncpg can open a connection and execute ``SELECT 1``.
Useful when migrating between Supabase poolers (5432 session vs 6543
transaction) to confirm the endpoint accepts traffic before launching
the orchestrator.
"""
from __future__ import annotations

import asyncio
import sys

from app.config.settings import settings
from app.database.engine import _prepare_dsn


async def main() -> int:
    raw = settings.database_url
    dsn, connect_args, is_pooler = _prepare_dsn(raw)
    print(f"raw DSN     : {raw}")
    print(f"clean DSN   : {dsn}")
    print(f"is_pooler   : {is_pooler}")
    print(f"connect_args: {connect_args}")

    pure = dsn.replace("postgresql+asyncpg://", "postgresql://")

    try:
        import asyncpg
    except ImportError:
        print("asyncpg not installed in this venv")
        return 1

    try:
        conn = await asyncpg.connect(pure, ssl="require", **connect_args)
    except Exception as exc:
        print(f"FAILED to connect: {type(exc).__name__}: {exc}")
        return 1

    try:
        value = await conn.fetchval("SELECT 1")
        print(f"SELECT 1 -> {value}  OK")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
