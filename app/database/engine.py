"""Async SQLAlchemy engine & session factory.

Single source of truth for the database connection.  The engine is lazily
created so unit tests that do not touch the DB never open a pool.

Supabase / pgbouncer compatibility
----------------------------------
The free Supabase tier exposes the DB through two endpoints:

* Direct connection (port 5432) — full Postgres; use it when possible.
* Transaction pooler (port 6543) — backed by pgbouncer in transaction
  mode, which forbids prepared statements.  ``asyncpg`` must be told
  to disable its statement cache **and** the ``pgbouncer`` / pooler
  query args must be stripped from the URL before handing it to the
  driver or it explodes with
  ``TypeError: connect() got an unexpected keyword argument 'pgbouncer'``.

We transparently detect the pooler (port 6543 or a ``pgbouncer=`` query
param) and apply the workaround so the user doesn't have to hand-craft
connect args.
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config.settings import settings


_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


_PGBOUNCER_QUERY_KEYS = {"pgbouncer", "pool_mode"}


def _prepare_dsn(raw: str) -> tuple[str, dict[str, Any], bool]:
    """Return ``(clean_dsn, connect_args, is_pooler)`` tuned for the target server.

    * Strips pgbouncer-only query params that asyncpg doesn't understand.
    * Disables ``asyncpg``'s statement cache when we detect a transaction
      pooler (port 6543 or an explicit ``pgbouncer=true`` hint).
    """
    parts = urlsplit(raw)
    query_items = parse_qsl(parts.query, keep_blank_values=True)
    is_pooler = parts.port == 6543
    cleaned_query: list[tuple[str, str]] = []
    for k, v in query_items:
        if k.lower() in _PGBOUNCER_QUERY_KEYS:
            is_pooler = True
            continue
        cleaned_query.append((k, v))

    if is_pooler:
        # SQLAlchemy's asyncpg dialect reads this from the URL query
        # string and turns off its own prepared-statement cache.
        cleaned_query.append(("prepared_statement_cache_size", "0"))

    clean_dsn = urlunsplit(
        parts._replace(query=urlencode(cleaned_query))
    )

    connect_args: dict[str, Any] = {}
    if is_pooler:
        # Transaction-mode poolers forbid server-side prepared
        # statements; disable asyncpg's own cache too.
        connect_args["statement_cache_size"] = 0

    return clean_dsn, connect_args, is_pooler


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        dsn, connect_args, _is_pooler = _prepare_dsn(settings.database_url)
        _engine = create_async_engine(
            dsn,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            future=True,
            connect_args=connect_args,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _session_factory


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
