"""Position sizing for Crypto Mode.

Fuses the user's anchors ("27 % first entry, 1.5 % late scoop") with a
quarter-Kelly cap so a weak edge never deploys the full anchor.  We
land between aggressive and survivable: typical entries 2-12 % of the
balance, occasional 12 % cap-hugging entries on very strong setups,
late scoops at 1.5 %.

The first-entry size formula::

    kelly_f         = max(0, edge_pct / 100) * crypto_kelly_fraction
    anchor          = balance * crypto_first_anchor_pct / 100
    cap_per_trade   = balance * crypto_per_trade_cap_pct / 100
    cap_concurrent  = max(0, balance * crypto_concurrent_cap_pct / 100
                        - currently_open_usd)
    raw_size        = min(anchor, balance * kelly_f, cap_per_trade,
                          cap_concurrent)
    final_size      = clamp(raw_size, 0, cap_concurrent)

The late-scoop is a flat 1.5 % only when the implied price is at an
extreme imbalance and we have at least 1.5 % of headroom under the
concurrent cap.

Sizing returns a :class:`SizingPlan` so the existing
:class:`TradeExecutor.open_trade` can write the trade row unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import settings
from app.strategies.base_strategy import SizingPlan


@dataclass(frozen=True)
class CryptoSizing:
    amount_usd: float
    anchor_usd: float
    kelly_usd: float
    per_trade_cap_usd: float
    concurrent_cap_usd: float
    reason: str

    def to_plan(self, *, entry_price: float, band: str) -> SizingPlan:
        plan = SizingPlan(
            amount_usd=round(self.amount_usd, 4),
            entry_price=entry_price,
            stop_loss=None,        # Crypto Mode: no auto SL
            take_profit=None,      # Crypto Mode: no auto TP either
            high_confidence=(band == "crypto_first" and self.amount_usd >= self.per_trade_cap_usd * 0.8),
        )
        plan.band = band  # type: ignore[attr-defined]
        return plan


def first_entry_size(
    *,
    balance: float,
    edge_pct: float,
    currently_open_usd: float = 0.0,
) -> CryptoSizing:
    """First-entry sizing for a fresh BTC binary."""
    if balance <= 0:
        return CryptoSizing(0.0, 0.0, 0.0, 0.0, 0.0, "zero_balance")

    anchor_usd = balance * settings.crypto_first_anchor_pct / 100.0
    per_trade_cap_usd = balance * settings.crypto_per_trade_cap_pct / 100.0
    concurrent_cap_total = balance * settings.crypto_concurrent_cap_pct / 100.0
    concurrent_cap_remaining = max(0.0, concurrent_cap_total - max(0.0, currently_open_usd))

    kelly_f = max(0.0, edge_pct / 100.0) * settings.crypto_kelly_fraction
    kelly_usd = balance * kelly_f

    raw = min(anchor_usd, kelly_usd, per_trade_cap_usd, concurrent_cap_remaining)
    raw = max(0.0, raw)

    # Floor at MIN_TRADE_USD so we don't post dust orders; if even the
    # floor is over the concurrent cap, return 0 with an explanatory
    # reason and let the orchestrator skip.
    if raw < settings.min_trade_usd:
        if concurrent_cap_remaining < settings.min_trade_usd:
            reason = "concurrent_cap_exhausted"
        elif kelly_usd < settings.min_trade_usd:
            reason = "edge_too_small"
        else:
            reason = "below_min_trade_usd"
        return CryptoSizing(
            amount_usd=0.0,
            anchor_usd=anchor_usd,
            kelly_usd=kelly_usd,
            per_trade_cap_usd=per_trade_cap_usd,
            concurrent_cap_usd=concurrent_cap_remaining,
            reason=reason,
        )

    return CryptoSizing(
        amount_usd=raw,
        anchor_usd=anchor_usd,
        kelly_usd=kelly_usd,
        per_trade_cap_usd=per_trade_cap_usd,
        concurrent_cap_usd=concurrent_cap_remaining,
        reason="ok",
    )


def late_scoop_size(
    *,
    balance: float,
    market_price: float,
    currently_open_usd: float = 0.0,
) -> CryptoSizing:
    """Tiny end-of-market scoop on extreme imbalances.

    Triggered only when ``market_price`` sits in the configured low or
    high tail (default 5 % / 95 %) — that is when the YES/NO odds are
    so lopsided that a small contrarian probe has decent EV even at
    the bell.
    """
    if balance <= 0:
        return CryptoSizing(0.0, 0.0, 0.0, 0.0, 0.0, "zero_balance")

    low = settings.crypto_late_scoop_low_threshold
    high = settings.crypto_late_scoop_high_threshold
    if not (market_price <= low or market_price >= high):
        return CryptoSizing(0.0, 0.0, 0.0, 0.0, 0.0, "price_not_extreme")

    anchor_usd = balance * settings.crypto_late_anchor_pct / 100.0
    per_trade_cap_usd = balance * settings.crypto_per_trade_cap_pct / 100.0
    concurrent_cap_total = balance * settings.crypto_concurrent_cap_pct / 100.0
    concurrent_cap_remaining = max(0.0, concurrent_cap_total - max(0.0, currently_open_usd))

    raw = min(anchor_usd, per_trade_cap_usd, concurrent_cap_remaining)
    raw = max(0.0, raw)
    if raw < settings.min_trade_usd:
        return CryptoSizing(
            amount_usd=0.0,
            anchor_usd=anchor_usd,
            kelly_usd=0.0,
            per_trade_cap_usd=per_trade_cap_usd,
            concurrent_cap_usd=concurrent_cap_remaining,
            reason="concurrent_cap_exhausted"
            if concurrent_cap_remaining < settings.min_trade_usd
            else "below_min_trade_usd",
        )
    return CryptoSizing(
        amount_usd=raw,
        anchor_usd=anchor_usd,
        kelly_usd=0.0,
        per_trade_cap_usd=per_trade_cap_usd,
        concurrent_cap_usd=concurrent_cap_remaining,
        reason="ok",
    )


__all__ = ["CryptoSizing", "first_entry_size", "late_scoop_size"]
