"""Sizing engine — confidence-banded percentage of balance.

Final spec (edge-first, balance-adaptive):

* **AUTO mode** → a *measurable* confidence tier picks a band, and each
  band commits a *percentage of the user's balance*.  The USD amount
  therefore scales with the account size:

  ==========================  =================  ===================  ============
  Tier driver                 Band name          % of balance (def.)  With 50€
  ==========================  =================  ===================  ============
  net_edge ∈ [MIN_EDGE, 4)    ``low``            1.5 %                0.75 €
  net_edge ≥ 4 OR |z| ≥ 2     ``mid``            3 %                  1.50 €
  net_edge ≥ 8 AND |z| ≥ 2.5  ``high``           5 %                  2.50 €
  ==========================  =================  ===================  ============

  The old legacy driver (the 0..100 score: 0-49 → low, 50-74 → mid,
  75-100 → high) is retained as a back-compat fallback for call sites
  that have not been upgraded yet.  See :func:`band_for_score` and
  :func:`tier_from_edge`.

* **SEMI / MANUAL mode** → we propose the AUTO anchor (balance × band%)
  but the user can override, clamped to the global
  ``[MIN_TRADE_USD, MAX_TRADE_USD]`` guard-rails.

* **``risk_pct``** acts as a per-user *hard ceiling on the %*.  Never
  widens the band.  Defaults to 10 % so it matches the high band.

* **``MIN_TRADE_USD`` / ``MAX_TRADE_USD``** are absolute USD guard-rails
  applied last so dust trades and catastrophic over-commits are both
  prevented.

Returns a :class:`SizingQuote` carrying the suggested/clamped amount
plus the balance-derived band USD bounds needed by the SEMI editor UX.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from app.config.settings import settings


Band = Literal["low_prob", "low", "mid", "high"]


def implied_entry_size_multiplier(price: float) -> float:
    """Scale stake by where ``price`` sits in the configured entry band.

    At ``entry_min_price`` we use ``entry_size_scale_at_min``; at
    ``entry_max_price`` we use ``entry_size_scale_at_max``.
    """
    if not settings.entry_implied_scale_enabled or price <= 0:
        return 1.0
    lo = float(settings.entry_min_price)
    hi = float(settings.entry_max_price)
    m_lo = float(settings.entry_size_scale_at_min)
    m_hi = float(settings.entry_size_scale_at_max)
    if hi <= lo:
        return 1.0
    p = min(max(float(price), lo), hi)
    t = (p - lo) / (hi - lo)
    return m_lo + t * (m_hi - m_lo)


def tier_from_ev(ev_tier: str, entry_price: Optional[float] = None) -> Band:
    """EV-driven band selection.

    Maps the EV estimator's quality tier to a sizing band:
      core         → high  (strong edge, full stake)
      mid          → mid   (opportunistic, moderate stake)
      low          → low_prob (exploratory asymmetric, tiny stake)
      reject / any → low   (safety fallback — should not reach here in AUTO)

    Low-probability entries are always floored at ``low_prob`` regardless of
    the EV tier so that asymmetric setups never accidentally receive full
    sizing even when they show a mathematically strong EV.
    """
    if (
        entry_price is not None
        and entry_price > 0.0
        and entry_price <= settings.low_prob_entry_price
    ):
        return "low_prob"
    if ev_tier == "core":
        return "high"
    if ev_tier == "mid":
        return "mid"
    if ev_tier == "low":
        return "low_prob"
    return "low"


@dataclass
class SizingQuote:
    amount_usd: float
    band: Band
    band_min: float  # lower USD edge of the band on the current balance
    band_max: float  # upper USD edge of the band on the current balance
    anchor: float  # balance × band%, what AUTO proposes by default
    suggested: float  # after the risk-% clamp but before any user override
    capped_by: Optional[str] = None  # "risk_pct" | "max_trade_usd" | "min_trade_usd" | None


def band_for_score(score: float) -> Band:
    """Legacy driver — maps the 0..100 cosmetic score to a band.

    The edge-first refactor prefers :func:`tier_from_edge` which uses
    measurable ``net_edge_pct`` + ``|z|`` instead of a composite score.
    This function is retained for back-compat with the SEMI UI and
    older call sites.
    """
    if score >= 75:
        return "high"
    if score >= 50:
        return "mid"
    return "low"


def tier_from_edge(
    net_edge_pct: Optional[float],
    abs_z: Optional[float],
    entry_price: Optional[float] = None,
) -> Band:
    """Measurable-only sizing tier.

    Input  ``net_edge_pct`` is the EV after fees + spread + slippage
    (see :mod:`app.services.execution_cost`).  ``abs_z`` is the
    |mispricing z-score| from :mod:`app.services.mispricing`.
    ``entry_price`` is the actual fill price (``0..1``) used to detect
    LOW-PROB "lottery ticket" entries which must be sized tiny even if
    the edge/z look great (implied probability is so low that win-rate
    is unreliable).

    * ``low_prob`` — ``entry_price ≤ LOW_PROB_ENTRY_PRICE``.  Tiny size
      regardless of edge/z; asymmetric upside is preserved by the exit
      ladder, not by stake size.
    * ``high``  — ``net_edge_pct ≥ 8`` AND ``|z| ≥ 2.5``  (perfect
      setup: deep mispricing and strong post-cost EV → full BAND_HIGH).
    * ``mid``   — ``net_edge_pct ≥ 4`` OR ``|z| ≥ 2.0``  (decent setup
      → BAND_MID).
    * ``low``   — otherwise (signal barely clears the floor, sized small
      via BAND_LOW; "if not perfect, less money").
    """
    if (
        entry_price is not None
        and entry_price > 0.0
        and entry_price <= settings.low_prob_entry_price
    ):
        return "low_prob"
    edge = float(net_edge_pct) if net_edge_pct is not None else 0.0
    z = float(abs_z) if abs_z is not None else 0.0
    if edge >= 8.0 and z >= 2.5:
        return "high"
    if edge >= 4.0 or z >= 2.0:
        return "mid"
    return "low"


def band_pct(band: Band) -> float:
    """Return the upper percentage of balance committed at this band."""
    if band == "low_prob":
        return settings.band_low_prob_pct
    if band == "low":
        return settings.band_low_pct
    if band == "mid":
        return settings.band_mid_pct
    return settings.band_high_pct


def band_bounds(band: Band, balance: float) -> tuple[float, float]:
    """Return the USD ``(min, max)`` range allowed inside a band for the
    given ``balance``.

    The lower edge is half the band percentage (so the user can pull the
    size down to ~50 % of the anchor) and the upper edge is the full band
    percentage.  Both are clamped to the absolute USD floor/ceiling so
    tiny or huge balances still get a sensible range.
    """
    pct = band_pct(band)
    if balance is None or balance <= 0:
        return settings.min_trade_usd, min(settings.min_trade_usd, settings.max_trade_usd)
    lo = balance * (pct / 2.0) / 100.0
    hi = balance * pct / 100.0
    lo = max(settings.min_trade_usd, lo)
    hi = min(settings.max_trade_usd, hi)
    if hi < lo:
        # Balance is large enough that even the half-band % exceeds the
        # absolute USD ceiling — collapse both to the ceiling.
        lo = hi
    return lo, hi


def suggest_amount(
    score: Optional[float] = None,
    balance: float = 0.0,
    *,
    net_edge_pct: Optional[float] = None,
    abs_z: Optional[float] = None,
    entry_price: Optional[float] = None,
) -> float:
    """Convenience — AUTO anchor (balance × band%) for UI pre-fill.

    When ``net_edge_pct`` (and optionally ``abs_z``) are provided, the
    edge-first driver picks the band.  Otherwise the legacy 0..100
    score picks it.
    """
    if balance is None or balance <= 0:
        return 0.0
    if net_edge_pct is not None:
        band = tier_from_edge(net_edge_pct, abs_z, entry_price)
    else:
        band = band_for_score(score or 0.0)
    return balance * band_pct(band) / 100.0


def compute_sizing(
    *,
    score: Optional[float] = None,
    balance: float,
    risk_pct: float,
    user_override: Optional[float] = None,
    max_trade_usd: Optional[float] = None,
    net_edge_pct: Optional[float] = None,
    abs_z: Optional[float] = None,
    entry_price: Optional[float] = None,
    ev_tier: Optional[str] = None,
) -> SizingQuote:
    """Build a :class:`SizingQuote` given the user context.

    Band selection priority (highest wins):
      1. ``ev_tier`` — EV estimator tier (core/mid/low/reject) → preferred.
      2. ``net_edge_pct`` + ``abs_z`` → legacy measurable tier.
      3. ``score`` (0..100) → back-compat legacy fallback.

    Sizing = **band% × balance**.  ``risk_pct`` acts as a user-level
    ceiling on the effective percentage (never widens it).  Absolute
    USD floor/ceiling still apply as last-resort guard-rails.
    """
    if ev_tier is not None and ev_tier != "reject":
        band = tier_from_ev(ev_tier, entry_price)
    elif net_edge_pct is not None:
        band = tier_from_edge(net_edge_pct, abs_z, entry_price)
    else:
        band = band_for_score(score if score is not None else 0.0)
    pct = band_pct(band)
    hard_cap = max_trade_usd if max_trade_usd is not None else settings.max_trade_usd

    # Effective %: band% unless the user's personal risk_pct is tighter.
    effective_pct = pct
    if risk_pct is not None and risk_pct > 0:
        effective_pct = min(pct, float(risk_pct))

    # AUTO anchor = balance × effective%.
    anchor = balance * effective_pct / 100.0 if balance and balance > 0 else 0.0
    band_min, band_max = band_bounds(band, balance)

    capped_by: Optional[str] = None

    if user_override is not None:
        # SEMI override: clamp to the *global* guard-rails only — the user
        # is explicitly choosing the amount and may legitimately want to
        # go below the band's lower edge (e.g. scale down a high-band
        # signal on a big account).  The floor is ``MIN_TRADE_USD`` to
        # avoid dust trades; the ceiling is ``MAX_TRADE_USD``.
        base = max(settings.min_trade_usd, min(float(user_override), hard_cap))
    else:
        base = min(anchor, hard_cap)
        if anchor > hard_cap:
            capped_by = "max_trade_usd"
        elif effective_pct < pct:
            # Band was tightened by the user's risk_pct ceiling.
            capped_by = "risk_pct"

    suggested = base

    if base > hard_cap:
        base = hard_cap
        capped_by = "max_trade_usd"

    if base < settings.min_trade_usd:
        # Account too small for the band% to clear the dust floor.  We
        # still propose the floor but flag it so the caller can decide
        # whether to skip the trade.
        floor = min(settings.min_trade_usd, hard_cap)
        if anchor > 0 and floor > anchor:
            capped_by = "min_trade_usd"
        base = floor

    if (
        settings.entry_implied_scale_enabled
        and entry_price is not None
        and float(entry_price) > 0
    ):
        mul = implied_entry_size_multiplier(float(entry_price))
        base = round(base * mul, 4)
        if base > hard_cap:
            base = hard_cap
            capped_by = "max_trade_usd"
            base = round(base, 2)
        elif base < settings.min_trade_usd:
            floor = min(settings.min_trade_usd, hard_cap)
            base = round(floor, 2)
            capped_by = capped_by or "min_trade_usd"
        else:
            base = round(base, 2)

    return SizingQuote(
        amount_usd=round(base, 2),
        band=band,
        band_min=round(band_min, 2),
        band_max=round(band_max, 2),
        anchor=round(anchor, 2),
        suggested=round(suggested, 2),
        capped_by=capped_by,
    )
