"""Sizing & rounding helpers.

All amounts handled as ``Decimal`` internally to avoid float drift,
exposed as float to callers that interact with HTTP APIs.
"""
from __future__ import annotations

from decimal import ROUND_DOWN, Decimal


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def position_size_usd(
    balance: float, risk_pct: float, min_usd: float, max_usd: float
) -> float:
    """Clamp ``balance * risk_pct/100`` within the configured [min, max]."""
    if balance <= 0 or risk_pct <= 0:
        return 0.0
    raw = balance * (risk_pct / 100.0)
    return clamp(raw, min_usd, min(max_usd, balance))


def round_price(price: float, decimals: int = 3) -> float:
    q = Decimal(10) ** -decimals
    return float(Decimal(str(price)).quantize(q, rounding=ROUND_DOWN))


def shares_from_usd(amount_usd: float, price: float) -> float:
    """Convert a USD budget into share count at the given price."""
    if price <= 0:
        return 0.0
    return round(amount_usd / price, 4)


def pnl_usd(entry_price: float, current_price: float, shares: float) -> float:
    return round((current_price - entry_price) * shares, 4)


def pnl_pct(entry_price: float, current_price: float) -> float:
    if entry_price <= 0:
        return 0.0
    return round((current_price - entry_price) / entry_price * 100.0, 4)
