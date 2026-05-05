"""Unit tests for :mod:`app.services.max_sniper` helper functions."""
from __future__ import annotations

from datetime import datetime, timezone

from app.services.max_sniper import (
    WINDOW_SECONDS,
    _aligned_window_ts,
    _slug_candidates,
)


def test_aligned_window_ts_floors_to_300() -> None:
    fixed = datetime(2026, 5, 5, 12, 7, 23, tzinfo=timezone.utc)
    ts = _aligned_window_ts(fixed)
    # 12:07:23 → window opened at 12:05:00 (epoch 1746446700).
    assert ts % WINDOW_SECONDS == 0
    assert ts == int(datetime(2026, 5, 5, 12, 5, 0, tzinfo=timezone.utc).timestamp())


def test_aligned_window_ts_exact_boundary_returns_self() -> None:
    boundary = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    ts = _aligned_window_ts(boundary)
    assert ts == int(boundary.timestamp())


def test_slug_candidates_default() -> None:
    cands = _slug_candidates(1746446700)
    assert "btc-updown-5m-1746446700" in cands
    assert any("bitcoin-up-or-down" in c for c in cands)
    assert all("1746446700" in c for c in cands)


def test_slug_candidates_respects_settings(monkeypatch) -> None:
    from app.config.settings import settings as st

    monkeypatch.setattr(
        st, "max_slug_templates", "btc-{ts}-test, bitcoin-5m-{ts}"
    )
    cands = _slug_candidates(123)
    assert cands == ["btc-123-test", "bitcoin-5m-123"]
