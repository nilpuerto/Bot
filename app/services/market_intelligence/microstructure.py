"""Microstructure context — derives a "book health" signal.

We already have :mod:`app.services.microstructure` which snapshots the
order book into :class:`MicrostructureFeatures`.  What that module does
*not* do is compare the current book against the recent past, i.e.
detect liquidity expansion/contraction and imbalance pressure over
time.  That is the job of this thin adapter.

Outputs:

* ``spread_bps``           — instantaneous half-spread, basis points.
* ``spread_trend_bps``     — change in ``spread_bps`` versus the last
                             sample (+ = widening = worse).
* ``top_depth_usd``        — sum of top-5 bid + ask USD depth.
* ``imbalance``            — ``bid_depth / (bid_depth + ask_depth) * 2 - 1``
                             in ``[-1, +1]``.  Positive = more bids.
* ``context_score``        — ``0..1`` summary used by the aggregator.
                             Tight spread, deep top, non-extreme
                             imbalance push toward 1.

All fields are best-effort.  Missing inputs degrade to ``None`` and the
context score falls back to ``0.5`` (neutral).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.services.microstructure import MicrostructureFeatures


@dataclass(frozen=True)
class MicrostructureContext:
    spread_bps: Optional[float]
    spread_trend_bps: Optional[float]
    top_depth_usd: Optional[float]
    imbalance: Optional[float]
    context_score: float  # 0..1, higher = healthier book


def _spread_bps(mid: Optional[float], spread: Optional[float]) -> Optional[float]:
    if mid is None or spread is None or mid <= 0:
        return None
    # Polymarket prices are probabilities (0..1), so bps = spread/mid*10 000.
    return max(0.0, (spread / mid) * 10_000.0)


def _imbalance(bid_depth: float, ask_depth: float) -> Optional[float]:
    total = bid_depth + ask_depth
    if total <= 0:
        return None
    return (bid_depth - ask_depth) / total  # [-1, +1]


def compute_microstructure(
    *,
    micro: Optional[MicrostructureFeatures],
    previous_spread_bps: Optional[float] = None,
    max_spread_bps_for_health: float = 400.0,
    min_depth_usd_for_health: float = 500.0,
) -> MicrostructureContext:
    """Turn raw book features into a bounded context.

    ``previous_spread_bps`` is the spread measured on the *previous*
    tick for the same market — when absent, trend is ``0``.

    ``max_spread_bps_for_health`` and ``min_depth_usd_for_health`` drive
    the score normalisation.  Defaults are conservative: a 4 % spread
    is fully "unhealthy" (score 0 for the spread pillar), and 500 $
    top-of-book depth is treated as "good enough".
    """
    if micro is None or not micro.has_book:
        return MicrostructureContext(
            spread_bps=None,
            spread_trend_bps=None,
            top_depth_usd=None,
            imbalance=None,
            context_score=0.5,
        )

    spread_bps = _spread_bps(micro.mid, micro.spread)
    trend = (
        (spread_bps - previous_spread_bps)
        if spread_bps is not None and previous_spread_bps is not None
        else 0.0
    )
    top_depth_usd = float(micro.top5_bid_depth or 0.0) + float(
        micro.top5_ask_depth or 0.0
    )
    imbalance = _imbalance(
        float(micro.top5_bid_depth or 0.0),
        float(micro.top5_ask_depth or 0.0),
    )

    # --- Sub-scores (all in [0, 1]) -----------------------------------
    # Spread: 0 bps => 1.0; >= max => 0.0; linear in between.
    spread_score = (
        1.0 - min(1.0, (spread_bps or max_spread_bps_for_health) / max_spread_bps_for_health)
        if spread_bps is not None
        else 0.5
    )
    # Depth: saturating — doubling the min takes us to ~1.0.
    depth_score = (
        min(1.0, top_depth_usd / max(1.0, min_depth_usd_for_health * 2))
        if top_depth_usd > 0
        else 0.0
    )
    # Imbalance: a tiny lean is fine, a stampede is not.  Penalise
    # |imbalance| > 0.8 (one side is 9× the other).
    imb_score = (
        1.0 - max(0.0, (abs(imbalance) - 0.5)) * 2.0
        if imbalance is not None
        else 0.5
    )
    # Trend: widening spreads are a red flag (news uncertainty /
    # liquidity withdrawal).
    trend_penalty = max(0.0, min(1.0, trend / max_spread_bps_for_health))
    trend_score = 1.0 - trend_penalty

    context = 0.40 * spread_score + 0.30 * depth_score + 0.15 * imb_score + 0.15 * trend_score
    return MicrostructureContext(
        spread_bps=round(spread_bps, 2) if spread_bps is not None else None,
        spread_trend_bps=round(trend, 2) if spread_bps is not None else None,
        top_depth_usd=round(top_depth_usd, 2),
        imbalance=round(imbalance, 4) if imbalance is not None else None,
        context_score=round(max(0.0, min(1.0, context)), 4),
    )
