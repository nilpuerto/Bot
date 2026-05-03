"""Unit tests for :mod:`app.services.crypto_sizer`."""
from __future__ import annotations

import pytest

from app.services.crypto_sizer import first_entry_size, late_scoop_size


def _patch_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.crypto_sizer.settings.crypto_first_anchor_pct", 27.0)
    monkeypatch.setattr("app.services.crypto_sizer.settings.crypto_late_anchor_pct", 1.5)
    monkeypatch.setattr("app.services.crypto_sizer.settings.crypto_per_trade_cap_pct", 12.0)
    monkeypatch.setattr("app.services.crypto_sizer.settings.crypto_concurrent_cap_pct", 45.0)
    monkeypatch.setattr("app.services.crypto_sizer.settings.crypto_kelly_fraction", 0.25)
    monkeypatch.setattr("app.services.crypto_sizer.settings.min_trade_usd", 2.0)
    monkeypatch.setattr("app.services.crypto_sizer.settings.crypto_late_scoop_low_threshold", 0.05)
    monkeypatch.setattr("app.services.crypto_sizer.settings.crypto_late_scoop_high_threshold", 0.95)


# ---- first_entry_size ----------------------------------------------------

def test_first_entry_zero_balance(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_defaults(monkeypatch)
    s = first_entry_size(balance=0.0, edge_pct=10.0)
    assert s.amount_usd == 0.0
    assert s.reason == "zero_balance"


def test_first_entry_kelly_dominates_for_weak_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4 % edge should never deploy the full 27 % anchor."""
    _patch_defaults(monkeypatch)
    s = first_entry_size(balance=1_000.0, edge_pct=4.0)
    # Kelly = 4% * 0.25 = 1% of balance = $10
    assert s.amount_usd == pytest.approx(10.0, abs=1e-6)
    assert s.reason == "ok"


def test_first_entry_per_trade_cap_dominates_for_huge_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrealistically huge edge gets clipped by the per-trade cap, not 27 %."""
    _patch_defaults(monkeypatch)
    s = first_entry_size(balance=1_000.0, edge_pct=300.0)
    # Anchor 27% = 270, Kelly = 75%*$1000 = 750, per-trade cap 12% = 120.
    # The min is 120.
    assert s.amount_usd == pytest.approx(120.0, abs=1e-6)


def test_first_entry_anchor_caps_when_kelly_huge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even 27 % anchor never exceeds per-trade cap (defensive)."""
    _patch_defaults(monkeypatch)
    monkeypatch.setattr("app.services.crypto_sizer.settings.crypto_per_trade_cap_pct", 30.0)
    # Now per-trade cap 30 % > anchor 27 %.
    s = first_entry_size(balance=1_000.0, edge_pct=300.0)
    assert s.amount_usd == pytest.approx(270.0, abs=1e-6)


def test_first_entry_concurrent_cap_dominates(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_defaults(monkeypatch)
    # 1000 * 45% = 450 cap; 400 already open -> only 50 available.
    s = first_entry_size(balance=1_000.0, edge_pct=10.0, currently_open_usd=400.0)
    assert s.amount_usd == pytest.approx(25.0, abs=1e-6)  # Kelly = 2.5% = 25 (< 50 left)
    s2 = first_entry_size(balance=1_000.0, edge_pct=300.0, currently_open_usd=400.0)
    assert s2.amount_usd == pytest.approx(50.0, abs=1e-6)


def test_first_entry_below_min_trade_usd_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_defaults(monkeypatch)
    # 100 * 0.5% kelly = $0.5 -> below min $2.
    s = first_entry_size(balance=100.0, edge_pct=2.0)
    assert s.amount_usd == 0.0
    assert s.reason == "edge_too_small"


def test_first_entry_concurrent_cap_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_defaults(monkeypatch)
    s = first_entry_size(balance=1_000.0, edge_pct=10.0, currently_open_usd=450.0)
    assert s.amount_usd == 0.0
    assert s.reason == "concurrent_cap_exhausted"


# ---- late_scoop_size ----------------------------------------------------

def test_late_scoop_inactive_at_normal_price(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_defaults(monkeypatch)
    s = late_scoop_size(balance=1_000.0, market_price=0.40)
    assert s.amount_usd == 0.0
    assert s.reason == "price_not_extreme"


def test_late_scoop_active_at_low_extreme(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_defaults(monkeypatch)
    s = late_scoop_size(balance=1_000.0, market_price=0.04)
    # 1.5% of 1000 = 15
    assert s.amount_usd == pytest.approx(15.0, abs=1e-6)


def test_late_scoop_active_at_high_extreme(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_defaults(monkeypatch)
    s = late_scoop_size(balance=1_000.0, market_price=0.97)
    assert s.amount_usd == pytest.approx(15.0, abs=1e-6)


def test_late_scoop_respects_concurrent_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_defaults(monkeypatch)
    s = late_scoop_size(balance=1_000.0, market_price=0.04, currently_open_usd=450.0)
    assert s.amount_usd == 0.0
    assert s.reason == "concurrent_cap_exhausted"
