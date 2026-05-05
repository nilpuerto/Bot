"""MAX Mode aggressive sizer.

Bet-sizing policy for MAX users:

* If cumulative realised profit since they switched into MAX is **> 0**,
  the next ticket bets exactly that running profit — the original
  bankroll is therefore never directly at risk after the first win.
* Otherwise, the ticket is sized at ``MAX_BANKROLL_FALLBACK_PCT`` of the
  user's effective USDC balance (default 30 %).
* A hard ``MAX_PER_TRADE_CAP_PCT`` and a daily concurrent cap protect
  against runaway losses on a hot streak that suddenly reverses.

Confidence tiers (see ``MAX_MIN_CONFIDENCE``, ``MAX_WEAK_CONFIDENCE_FLOOR``)
shrink nominal size instead of zeroing every sub-threshold ticket: weak
signals risk a fraction of the computed ticket; deadline "lottery"
recovery uses an even smaller fraction when |window delta| is wide enough.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from app.config.settings import settings
from app.strategies.base_strategy import SizingPlan


@dataclass(frozen=True)
class MaxSizing:
    amount_usd: float
    bankroll: float
    cumulative_profit: float
    fallback_used: bool
    cap_usd: float
    reason: str

    def to_plan(self, *, entry_price: float, band: str = "max_snipe") -> SizingPlan:
        plan = SizingPlan(
            amount_usd=round(self.amount_usd, 4),
            entry_price=entry_price,
            stop_loss=None,
            take_profit=None,
            high_confidence=True,
        )
        plan.band = band  # type: ignore[attr-defined]
        return plan


def _confidence_multiplier(
    *,
    confidence: float,
    deadline_forced: bool,
    window_delta_abs_pct: float,
) -> Tuple[float, Optional[str]]:
    """Return (multiplier, skip_reason or None if tradable)."""
    min_conf = float(settings.max_min_confidence)
    weak_floor = float(settings.max_weak_confidence_floor)
    weak_frac = float(settings.max_weak_trade_fraction)
    deadline_delta = float(settings.max_deadline_delta_abs_pct)
    deadline_frac = float(settings.max_deadline_trade_fraction)
    ad = abs(float(window_delta_abs_pct))

    if confidence >= min_conf:
        return 1.0, None
    if confidence >= weak_floor:
        return weak_frac, None
    if deadline_forced and ad >= deadline_delta:
        return deadline_frac, None
    return 0.0, f"low_confidence_{confidence:.2f}_no_tier"


def size_for_entry(
    *,
    balance: float,
    cumulative_profit: float,
    confidence: float,
    currently_open_usd: float = 0.0,
    is_window_decisive: bool = False,
    deadline_forced: bool = False,
    window_delta_abs_pct: float = 0.0,
) -> MaxSizing:
    """Return the USD amount the next MAX ticket should risk."""
    if balance <= 0:
        return MaxSizing(0.0, 0.0, max(0.0, cumulative_profit), False, 0.0, "zero_balance")

    mult, skip = _confidence_multiplier(
        confidence=float(confidence),
        deadline_forced=deadline_forced,
        window_delta_abs_pct=float(window_delta_abs_pct),
    )
    if mult <= 0 or skip:
        return MaxSizing(
            0.0,
            balance,
            max(0.0, cumulative_profit),
            False,
            0.0,
            skip or "low_confidence",
        )

    profit = max(0.0, float(cumulative_profit))
    fallback_used = profit <= 0.0
    if fallback_used:
        bet = balance * float(settings.max_bankroll_fallback_pct) / 100.0
    else:
        bet = profit

    bet *= mult

    cap_pct = float(settings.max_per_trade_cap_pct)
    if is_window_decisive:
        cap_pct *= 1.25
    cap_usd = balance * cap_pct / 100.0
    if bet > cap_usd:
        bet = cap_usd

    concurrent_cap_usd = balance * float(settings.max_concurrent_cap_pct) / 100.0
    headroom = max(0.0, concurrent_cap_usd - max(0.0, currently_open_usd))
    if bet > headroom:
        bet = headroom

    floor = float(settings.min_trade_usd)
    if bet < floor:
        return MaxSizing(
            0.0,
            balance,
            profit,
            fallback_used,
            cap_usd,
            f"below_min_{bet:.2f}<{floor:.2f}",
        )
    reason = "ok_profits" if not fallback_used else "ok_fallback"
    if mult < 1.0:
        reason = f"{reason}_size_tier_{mult:.2f}"
    return MaxSizing(
        round(bet, 4),
        balance,
        profit,
        fallback_used,
        cap_usd,
        reason,
    )


__all__ = ["MaxSizing", "size_for_entry"]
