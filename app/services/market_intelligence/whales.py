"""Whale context — is smart money flowing in the same direction?

Prym already records :class:`TraderPosition` rows every time a tracked
wallet moves on a market.  This adapter summarises *recent* activity
on ``market_id`` in a form the aggregator can consume:

* ``flow_usd``             — net USD flow in the direction of ``side``
                             over the lookback window (positive = whales
                             agree with the signal, negative = they
                             fade it).
* ``gross_usd``             — absolute USD moved by tracked wallets,
                             regardless of direction.  Used to detect
                             "unusual accumulation" events.
* ``alignment_ratio``      — ``aligned_usd / gross_usd`` in ``[0, 1]``
                             when there is activity, else ``None``.
* ``wallet_count``         — distinct wallet addresses that touched
                             the market in the window.
* ``unusual_accumulation`` — True when gross_usd clears
                             ``settings.mi_whale_unusual_usd``.
* ``context_score``        — 0..1 summary.

An absent signal side defaults to ``None``/``0`` and a neutral 0.5
context so markets with no whales don't get penalised.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from app.database.models import TraderPosition


@dataclass(frozen=True)
class WhaleContext:
    flow_usd: float
    gross_usd: float
    alignment_ratio: Optional[float]
    wallet_count: int
    unusual_accumulation: bool
    context_score: float  # 0..1


def _position_side(pos: TraderPosition) -> Optional[str]:
    """Normalise the side to ``"yes"`` / ``"no"`` / ``None``.

    ``TraderPosition.side`` is a :class:`TradeSide` enum in the ORM but
    occasionally appears as a bare string in tests, so we support both.
    """
    side_val = getattr(pos, "side", None)
    if side_val is None:
        return None
    if hasattr(side_val, "value"):
        side_val = side_val.value
    side_str = str(side_val).lower().strip()
    if side_str in ("yes", "no"):
        return side_str
    return None


def compute_whales(
    *,
    positions: Iterable[TraderPosition],
    side: str,
    unusual_usd: float = 20_000.0,
    flow_saturation_usd: float = 50_000.0,
) -> WhaleContext:
    """Summarise whale activity on a market for the given signal side.

    Parameters
    ----------
    positions
        Rows of :class:`TraderPosition` from the lookback window.
    side
        ``"yes"`` or ``"no"`` — the side of the *signal* we are scoring.
    unusual_usd
        Gross USD threshold above which a market is flagged as an
        unusual accumulation event.
    flow_saturation_usd
        Net flow magnitude that saturates the context score at the
        extremes of the [0, 1] range.  Tune via
        ``settings.mi_whale_flow_saturation_usd``.
    """
    side_lower = side.lower().strip()
    if side_lower not in ("yes", "no"):
        return WhaleContext(
            flow_usd=0.0,
            gross_usd=0.0,
            alignment_ratio=None,
            wallet_count=0,
            unusual_accumulation=False,
            context_score=0.5,
        )

    aligned_usd = 0.0
    opposite_usd = 0.0
    wallets: set = set()

    for pos in positions:
        size_raw = getattr(pos, "size_usd", None)
        if size_raw is None:
            continue
        try:
            size = float(size_raw)
        except (TypeError, ValueError):
            continue
        if size <= 0:
            continue
        pos_side = _position_side(pos)
        if pos_side is None:
            continue
        wallets.add(getattr(pos, "trader_id", None))
        if pos_side == side_lower:
            aligned_usd += size
        else:
            opposite_usd += size

    gross = aligned_usd + opposite_usd
    flow = aligned_usd - opposite_usd
    alignment_ratio = (aligned_usd / gross) if gross > 0 else None
    unusual = gross >= unusual_usd

    # --- Context score ------------------------------------------------
    if gross <= 0:
        context = 0.5  # No data ⇒ neutral, don't penalise.
    else:
        # Saturating sigmoid-like mapping of flow to [0,1].
        clipped = max(-1.0, min(1.0, flow / max(1.0, flow_saturation_usd)))
        context = 0.5 + 0.5 * clipped
    return WhaleContext(
        flow_usd=round(flow, 2),
        gross_usd=round(gross, 2),
        alignment_ratio=round(alignment_ratio, 4) if alignment_ratio is not None else None,
        wallet_count=len(wallets),
        unusual_accumulation=unusual,
        context_score=round(max(0.0, min(1.0, context)), 4),
    )
