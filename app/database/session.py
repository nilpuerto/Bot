"""Session context manager used throughout the app."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.engine import get_session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield a session with commit/rollback semantics.

    Usage::

        async with session_scope() as s:
            s.add(thing)
    """
    factory = get_session_factory()
    session: AsyncSession = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
