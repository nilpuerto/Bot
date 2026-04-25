"""Momentum context — price velocity & acceleration as a timing signal.

The mispricing pillar already says "how wrong is this price?", and the
timing service says "which phase of repricing are we in?".  This module
adds the *derivative*: is the price actively moving in the direction
of the hypothesised edge **right now**?

It reads a short window of :class:`MarketPriceHistory` samples and
returns:

* ``velocity_pct_per_min`` — mean price change (% of price) per minute
                             over the window.
* ``acceleration``         — change in velocity between the first and
                             second half of the window.
* ``abnormal_z``           — |velocity| expressed in units of the stdev
                             of 1-minute returns in the window.  Used
                             as "this move is unusually big for this
                             market" signal.
* ``aligned``              — True when the velocity direction matches
                             the signal side (``yes`` ⇒ up, ``no`` ⇒ down).
* ``context_score``        — 0..1 summary combining magnitude + alignment.

A too-small sample (<= 3 points) returns a neutral context.  No hard
gates are imposed here: the scorer is still the sole arbiter.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence


@dataclass(frozen=True)
class PricePoint:
    """Lightweight view over a price sample.

    Accepting a protocol-like object instead of the ORM row keeps the
    pure logic decoupled from SQLAlchemy so it can be unit-tested with
    plain data.
    """

    price: float
    observed_epoch: float  # seconds since epoch, UTC


@dataclass(frozen=True)
class MomentumContext:
    velocity_pct_per_min: Optional[float]
    acceleration: Optional[float]
    abnormal_z: Optional[float]
    aligned: Optional[bool]
    context_score: float  # 0..1


def _as_points(rows: Iterable) -> list[PricePoint]:
    """Accept either ORM rows or bare PricePoint instances."""
    out: list[PricePoint] = []
    for r in rows:
        if isinstance(r, PricePoint):
            out.append(r)
            continue
        try:
            price = float(r.price) if r.price is not None else None
            if price is None:
                continue
            epoch = r.observed_at.timestamp()
            out.append(PricePoint(price=price, observed_epoch=epoch))
        except AttributeError:
            continue
    return out


def _pct_returns(points: Sequence[PricePoint]) -> list[tuple[float, float]]:
    """Return (dt_minutes, return_pct) between consecutive points."""
    out: list[tuple[float, float]] = []
    for a, b in zip(points, points[1:]):
        if a.price <= 0 or b.price <= 0:
            continue
        dt_min = (b.observed_epoch - a.observed_epoch) / 60.0
        if dt_min <= 0:
            continue
        ret_pct = (b.price - a.price) / a.price * 100.0
        out.append((dt_min, ret_pct))
    return out


def compute_momentum(
    *,
    history: Iterable,
    side: str,
    min_samples: int = 4,
) -> MomentumContext:
    """Derive velocity/acceleration/alignment from a price window.

    ``history`` is typically the last N rows of
    :class:`MarketPriceHistory` ordered ascending by ``observed_at``.
    ``side`` is either ``"yes"`` or ``"no"`` — aligns the direction so
    a rising price on a YES bet and a falling price on a NO bet both
    register as positive alignment.
    """
    points = _as_points(history)
    if len(points) < min_samples:
        return MomentumContext(
            velocity_pct_per_min=None,
            acceleration=None,
            abnormal_z=None,
            aligned=None,
            context_score=0.5,
        )

    returns = _pct_returns(points)
    if not returns:
        return MomentumContext(
            velocity_pct_per_min=None,
            acceleration=None,
            abnormal_z=None,
            aligned=None,
            context_score=0.5,
        )

    # --- Velocity: weighted by dt so irregular samples don't distort.
    total_dt = sum(dt for dt, _ in returns)
    if total_dt <= 0:
        return MomentumContext(
            velocity_pct_per_min=None,
            acceleration=None,
            abnormal_z=None,
            aligned=None,
            context_score=0.5,
        )
    # Mean of per-minute rates.
    per_min = [r / dt for dt, r in returns]
    velocity = sum(per_min) / len(per_min)

    # --- Acceleration: velocity(second half) - velocity(first half).
    half = max(1, len(per_min) // 2)
    first_half = sum(per_min[:half]) / half
    second_half = sum(per_min[half:]) / max(1, (len(per_min) - half))
    acceleration = second_half - first_half

    # --- abnormal_z: stdev of per-minute returns.
    if len(per_min) > 1:
        mean = sum(per_min) / len(per_min)
        var = sum((x - mean) ** 2 for x in per_min) / (len(per_min) - 1)
        std = math.sqrt(var) if var > 0 else 0.0
        abnormal_z = (velocity / std) if std > 0 else None
    else:
        abnormal_z = None

    # --- Alignment with signal direction.
    side_sign = 1.0 if side.lower() == "yes" else -1.0
    aligned = (velocity * side_sign) >= 0

    # --- Context score: 0..1
    # Base 0.5, add magnitude (clipped) when aligned, subtract when not.
    magnitude = min(1.0, abs(velocity) / 3.0)  # 3 %/min ⇒ full signal
    if aligned:
        base = 0.5 + 0.5 * magnitude
    else:
        base = 0.5 - 0.5 * magnitude
    # Acceleration aligned with velocity adds a small bonus.
    if acceleration * velocity > 0:
        base += 0.05
    context = max(0.0, min(1.0, base))

    return MomentumContext(
        velocity_pct_per_min=round(velocity, 4),
        acceleration=round(acceleration, 4),
        abnormal_z=round(abnormal_z, 4) if abnormal_z is not None else None,
        aligned=bool(aligned),
        context_score=round(context, 4),
    )
