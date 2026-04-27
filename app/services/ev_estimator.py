"""Expected-Value estimator for binary Polymarket positions.

Formula
-------
EV = P_edge_real × |net_edge_pct| − (1 − P_edge_real) × ev_loss_estimate

Where:
  P_edge_real   — probability that the observed mispricing is *real* and not
                  noise.  Estimated from a conservative prior (EV_BASE_P)
                  boosted by measurable signals:
                    * z-score deviation  (statistical strength of mispricing)
                    * context_score      (quality of news→market match)
  net_edge_pct  — measured EV after costs from the ExecutionCostModel.
  ev_loss_estimate — expected loss when the edge is noise, approximately equal
                  to one round-trip cost (spread + slippage proxy).

Tier mapping
------------
  core          EV ≥ EV_CORE_MIN   → strong confident play, full sizing
  mid           EV ≥ EV_OPP_MIN    → opportunistic, moderate sizing
  low           EV > 0 AND is_exploratory → asymmetric lottery ticket, tiny size
  reject        EV ≤ 0 or all other cases

Exploratory condition
---------------------
  entry_price ≤ LOW_PROB_ENTRY_PRICE   (cheap ticket)
  AND payout_ratio ≥ EV_EXPLORATORY_PAYOUT_MIN  (high asymmetry)
  AND ev > 0                            (still EV-positive)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from app.config.settings import settings


Tier = Literal["core", "mid", "low", "reject"]


@dataclass
class EVResult:
    ev: float
    p_edge_real: float
    z_boost: float
    context_boost: float
    payout_ratio: float
    is_exploratory: bool
    tier: Tier


def compute_ev(
    *,
    net_edge_pct: Optional[float],
    abs_z: float,
    context_score: float,
    entry_price: Optional[float],
) -> EVResult:
    """Compute expected value for a prospective trade.

    Parameters
    ----------
    net_edge_pct
        Measured net edge after spread, slippage and fees (from
        ExecutionCostModel).  Positive = we have an advantage.
    abs_z
        Absolute value of the mispricing z-score.
    context_score
        Match quality from the matcher (0..1).  Higher = more confident
        that the news actually affects this specific market.
    entry_price
        Current fill price (0..1).  Used to detect low-probability /
        high-payout exploratory setups.
    """
    edge = float(net_edge_pct or 0.0)

    # --- Probability that the edge is real, not noise --------------------
    base_p = float(settings.ev_base_p)
    z_boost = min(
        float(settings.ev_z_boost_max),
        abs_z * float(settings.ev_z_boost_per_unit),
    )
    ctx_boost = float(context_score) * float(settings.ev_context_max_boost)
    p_edge_real = min(0.95, base_p + z_boost + ctx_boost)

    # When z=0 (no measurable mispricing / no price history) apply a
    # confidence penalty so the EV model remains honest: we have no
    # statistical evidence of an edge, only context-based prior.
    # This is a soft penalty (not a gate): the trade can still proceed
    # if EV clears the floor after the haircut.
    if abs_z == 0.0:
        p_edge_real *= float(settings.ev_no_z_penalty)

    # --- EV formula -------------------------------------------------------
    # Expected gain  = P_real × net_edge
    # Expected loss  = (1 - P_real) × round-trip-cost-proxy
    loss_est = float(settings.ev_loss_estimate_pct)
    ev = p_edge_real * edge - (1.0 - p_edge_real) * loss_est
    ev = round(float(ev), 4)

    # --- Payout ratio (for exploratory detection) -------------------------
    price = float(entry_price or 0.5)
    price = max(0.01, min(0.99, price))
    payout_ratio = (1.0 - price) / price

    # --- Exploratory: tiny price + high asymmetry + EV above floor -------
    # EV_EXPLORATORY_MIN_EV defaults to 0 but can be set negative to allow
    # a small model-error budget for extreme lottery-ticket setups where
    # the payout asymmetry (≥ 4x) justifies entry despite a pessimistic EV.
    exploratory_ev_floor = float(settings.ev_exploratory_min_ev)
    is_exploratory = (
        entry_price is not None
        and entry_price > 0.0
        and entry_price <= float(settings.low_prob_entry_price)
        and payout_ratio >= float(settings.ev_exploratory_payout_min)
        and ev > exploratory_ev_floor
    )

    # --- Tier decision ----------------------------------------------------
    if ev >= float(settings.ev_core_min):
        tier: Tier = "core"
    elif ev >= float(settings.ev_opp_min):
        tier = "mid"
    elif is_exploratory:
        tier = "low"
    else:
        tier = "reject"

    return EVResult(
        ev=ev,
        p_edge_real=round(p_edge_real, 4),
        z_boost=round(z_boost, 4),
        context_boost=round(ctx_boost, 4),
        payout_ratio=round(payout_ratio, 4),
        is_exploratory=is_exploratory,
        tier=tier,
    )
