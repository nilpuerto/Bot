"""Shared entry-price gates for traded outcome tokens.

For binaries, mid ≈ implied probability on the traded leg.  These guards
enforce ``ENTRY_MAX_PRICE`` / ``ENTRY_MIN_PRICE`` and optional
``MIN_IMPLIED_PROB`` / ``MAX_IMPLIED_PROB`` everywhere we could open an
AUTO or cluster trade — not only in :class:`PrymStrategy`, which SEMI/
manual callbacks already honour but the news pipeline historically did
not consistently check before ``open_trade``.
"""
from __future__ import annotations

from typing import Optional

from app.config.settings import settings


def entry_token_gate_fail_reason(
    price: Optional[float],
    *,
    entry_min_override: Optional[float] = None,
    entry_max_override: Optional[float] = None,
) -> Optional[str]:
    """Return ``None`` if the traded token price passes all clamps.

    ``entry_*_override`` ties :class:`~app.strategies.prym_strategy.PrymStrategy`
    per-instance bands to this gate without duplicating comparisons.

    Reasons are stable log/event tokens.
    """
    if price is None or price <= 0:
        return "no_price"

    lo = float(
        entry_min_override
        if entry_min_override is not None
        else settings.entry_min_price
    )
    hi = float(
        entry_max_override
        if entry_max_override is not None
        else settings.entry_max_price
    )
    if price < lo:
        return "below_entry_min"
    if price > hi:
        return "above_entry_max"

    min_p = settings.min_implied_prob
    max_p = settings.max_implied_prob
    if min_p is not None and price < float(min_p):
        return "implied_below_min"
    if max_p is not None and price > float(max_p):
        return "implied_above_max"

    return None
