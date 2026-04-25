"""Time helpers.  Always work in UTC."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def seconds_since(ts: datetime) -> float:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (utcnow() - ts).total_seconds()


def is_fresh(ts: datetime, max_age_seconds: int) -> bool:
    return seconds_since(ts) <= max_age_seconds


def minutes_ago(minutes: int) -> datetime:
    return utcnow() - timedelta(minutes=minutes)


def days_ago(days: int) -> datetime:
    return utcnow() - timedelta(days=days)
