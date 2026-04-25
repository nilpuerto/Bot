"""Shared pytest fixtures & configuration.

All unit tests are DB-free by design — they exercise pure logic.  Anything
touching PostgreSQL lives in integration tests that are opt-in.
"""
from __future__ import annotations

import os

import pytest


# Make the app importable in CI and locally without requiring a .env file.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SIMULATION_MODE", "true")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"
