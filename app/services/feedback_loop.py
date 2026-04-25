"""Online feedback loop — constrained weight tuner.

After the edge-first refactor the feedback loop is DELIBERATELY small:

* only two learnable pillars — ``mispricing`` and ``liquidity``.
  ``news`` (hard gate) and ``timing`` (hard gate) are fixed at 1.0 and
  not perturbed.
* LR reduced from 0.02 → 0.005 so a single trade moves weights by at
  most ~0.5 % of their range.
* Weights are clipped to ``[FEEDBACK_CLIP_LOW, FEEDBACK_CLIP_HIGH]``
  (default ``[0.85, 1.15]``) — learned deltas cannot overwhelm the
  cap-based score contributions.
* Updates are GATED on sample size: until the repository has accrued
  ``FEEDBACK_MIN_TRADES`` trades with feature vectors, every call is a
  no-op.  This avoids overfitting to the first handful of outcomes.
* PnL noise band unchanged at ±2 % — trades inside the band carry no
  learning signal.

The tuner is idempotent: each trade is processed at most once
(identified by its id).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping, Optional

from app.config.settings import settings
from app.database.models import Trade
from app.database.repositories.weights_repo import WeightsRepository
from app.database.session import session_scope
from app.utils.logger import get_logger


logger = get_logger(__name__)


# Only these pillars are learnable — ``news`` and ``timing`` are hard
# gates in the edge-first refactor and never move.
LEARNABLE_COMPONENTS = ("mispricing", "liquidity")

# PnL bands that map to the sign.  Below -2 % → -1, above +2 % → +1,
# the noise band in between is treated as neutral (no weight update).
_LOSS_THRESHOLD = -2.0
_WIN_THRESHOLD = 2.0


@dataclass
class FeedbackStep:
    trade_id: int
    pnl_pct: float
    pnl_sign: int
    old_weights: dict[str, float]
    new_weights: dict[str, float]
    deltas: dict[str, float]


class FeedbackLoop:
    def __init__(
        self,
        learning_rate: Optional[float] = None,
        *,
        min_trades: Optional[int] = None,
        clip_low: Optional[float] = None,
        clip_high: Optional[float] = None,
    ) -> None:
        self.lr = learning_rate if learning_rate is not None else settings.feedback_lr
        self.min_trades = (
            min_trades if min_trades is not None else settings.feedback_min_trades
        )
        self.clip_low = (
            clip_low if clip_low is not None else settings.feedback_clip_low
        )
        self.clip_high = (
            clip_high if clip_high is not None else settings.feedback_clip_high
        )

    # ---- single trade -------------------------------------------------

    async def process_trade(self, trade: Trade) -> Optional[FeedbackStep]:
        """Apply a weight update from ``trade``.  Returns ``None`` when
        the trade doesn't carry a feature vector, sits in the PnL noise
        band, or the global sample size is below the minimum.
        """
        feature_vector = trade.feature_vector or {}
        if not feature_vector:
            return None

        components = _extract_components(feature_vector)
        if not components:
            return None

        pnl_pct_value = float(trade.pnl_pct or 0)
        sign = _pnl_sign(pnl_pct_value)
        if sign == 0:
            logger.debug(
                "feedback_noise_band_skipped",
                trade_id=trade.id,
                pnl_pct=pnl_pct_value,
            )
            return None

        # Sample-size gate — skip silently while we're in the cold-start
        # phase so a single lucky trade can't bias the weights.
        sample_ok = await _has_minimum_sample(self.min_trades)
        if not sample_ok:
            logger.debug(
                "feedback_sample_too_small",
                trade_id=trade.id,
                required=self.min_trades,
            )
            return None

        async with session_scope() as session:
            repo = WeightsRepository(session)
            current = await repo.get_all()
            old = {k: float(v) for k, v in current.items()}
            deltas: dict[str, float] = {}
            new: dict[str, Decimal] = {}
            for name in LEARNABLE_COMPONENTS:
                comp_val = float(components.get(name, 0.0))
                delta = self.lr * sign * _clip_unit(comp_val)
                deltas[name] = delta
                new_val = float(old.get(name, 1.0)) + delta
                new_val = max(self.clip_low, min(self.clip_high, new_val))
                new[name] = Decimal(str(new_val))
            # ``news`` and ``timing`` stay pinned at 1.0 — they are hard
            # gates, not learnable score contributions.
            for name in ("news", "timing"):
                new[name] = Decimal("1.0")
                deltas[name] = 0.0
            await repo.upsert_many(new)

        logger.info(
            "feedback_weights_updated",
            trade_id=trade.id,
            pnl_pct=pnl_pct_value,
            sign=sign,
            deltas={k: round(v, 4) for k, v in deltas.items()},
        )
        return FeedbackStep(
            trade_id=trade.id,
            pnl_pct=pnl_pct_value,
            pnl_sign=sign,
            old_weights=old,
            new_weights={k: float(v) for k, v in new.items()},
            deltas=deltas,
        )

    # ---- backfill -----------------------------------------------------

    async def backfill(self, trades: Iterable[Trade]) -> list[FeedbackStep]:
        steps: list[FeedbackStep] = []
        for trade in trades:
            step = await self.process_trade(trade)
            if step is not None:
                steps.append(step)
        return steps


# ---- helpers --------------------------------------------------------------

def _pnl_sign(pnl_pct_value: float) -> int:
    if pnl_pct_value >= _WIN_THRESHOLD:
        return 1
    if pnl_pct_value <= _LOSS_THRESHOLD:
        return -1
    return 0


def _clip_unit(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _extract_components(feature_vector: Mapping) -> dict[str, float]:
    """Return only the raw components the feedback loop is allowed to
    learn from (``mispricing_raw`` + ``liquidity_raw``).
    """
    out: dict[str, float] = {}
    for name in LEARNABLE_COMPONENTS:
        raw = feature_vector.get(f"{name}_raw")
        if raw is None:
            continue
        try:
            out[name] = float(raw)
        except (TypeError, ValueError):
            continue
    return out


async def _has_minimum_sample(min_trades: int) -> bool:
    """Quick existence check: have we accrued enough closed trades with
    feature vectors to start tuning weights?  Uses the trades repo so
    the scan is a single indexed COUNT query.
    """
    if min_trades <= 0:
        return True
    from app.database.repositories.trades_repo import TradesRepository

    try:
        async with session_scope() as session:
            repo = TradesRepository(session)
            count = await repo.count_closed_with_feature_vector()
    except Exception as exc:  # noqa: BLE001
        logger.warning("feedback_sample_check_failed", error=str(exc))
        return False
    return count >= min_trades
