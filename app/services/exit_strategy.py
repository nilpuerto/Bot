"""Repricing exit strategy — pure state machine shared by the trade
monitor and the Monte-Carlo simulator.

The design is "asymmetric re-pricing":

* Hard floor: close immediately on ``pnl_pct <= -hard_sl_pct``.
* Partial take-profit ladder: every rung closes a fraction of the
  *remaining* position and tightens (or arms) the trailing stop.
* No hard TP ceiling: the runner rides the trailing stop — whatever
  survives tier N keeps going.
* Time exit: if the trade never crosses ``time_exit_min_move_pct`` in
  the configured window AND the trailing is not yet armed, close at
  mid to avoid letting small winners turn into losses.

All helpers here are pure (take plain dicts / floats, return dicts /
actions) so they can be unit-tested without any DB or asyncio state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable, List, Optional

from app.config.settings import PartialTier, settings
from app.database.models import CloseReason


# ---------------------------------------------------------------------------
#  Exit actions
# ---------------------------------------------------------------------------


class ExitActionKind(str, Enum):
    HOLD = "hold"
    PARTIAL = "partial"
    CLOSE = "close"


@dataclass(frozen=True)
class ExitAction:
    kind: ExitActionKind
    # CLOSE: populated with the reason.
    close_reason: Optional[CloseReason] = None
    # PARTIAL: which tier fired + how many shares to realise + the new
    # trailing pullback to persist on the trade.
    tier: Optional[float] = None
    close_shares: Optional[float] = None
    new_trailing_pct: Optional[float] = None

    @classmethod
    def hold(cls) -> "ExitAction":
        return cls(kind=ExitActionKind.HOLD)

    @classmethod
    def close(cls, reason: CloseReason) -> "ExitAction":
        return cls(kind=ExitActionKind.CLOSE, close_reason=reason)

    @classmethod
    def partial(
        cls, *, tier: float, close_shares: float, new_trailing_pct: float
    ) -> "ExitAction":
        return cls(
            kind=ExitActionKind.PARTIAL,
            tier=tier,
            close_shares=close_shares,
            new_trailing_pct=new_trailing_pct,
        )


# ---------------------------------------------------------------------------
#  Exit-state shape helpers
# ---------------------------------------------------------------------------


def empty_exit_state() -> dict:
    """The JSON blob stored on ``trades.exit_state`` at trade creation."""
    return {
        "tiers_hit": [],
        "trailing_pct": float(settings.trailing_pct),
        "max_pnl_pct_seen": 0.0,
        "realized_pnl_usd": 0.0,
        "partials": [],
    }


def _tiers_hit(state: dict) -> List[float]:
    return [float(x) for x in state.get("tiers_hit", [])]


def _trailing_pct(state: dict) -> float:
    return float(state.get("trailing_pct", settings.trailing_pct))


# ---------------------------------------------------------------------------
#  Core evaluator
# ---------------------------------------------------------------------------


@dataclass
class TradeExitView:
    """Minimal read model the evaluator needs about a trade.

    Kept separate from the ORM / simulator row so the same pure code
    works against both without pulling SQLAlchemy into tests.
    """

    entry_price: float
    current_shares: float
    opened_at: datetime
    peak_price: Optional[float]
    trailing_active: bool
    exit_state: dict


@dataclass
class ExitEvaluation:
    """The output of one tick: an action plus the *new* exit_state to
    persist (even on HOLD, because ``max_pnl_pct_seen`` advances).
    """

    action: ExitAction
    new_exit_state: dict
    new_peak_price: Optional[float]
    new_trailing_active: bool


def _untriggered_tier(
    pnl_pct_value: float, tiers: Iterable[PartialTier], tiers_hit: List[float]
) -> Optional[PartialTier]:
    """Return the first (lowest-threshold) tier whose threshold is
    crossed AND that hasn't fired yet.  ``None`` if nothing to do.
    """
    for tier in tiers:
        if tier.pnl_threshold_pct in tiers_hit:
            continue
        if pnl_pct_value >= tier.pnl_threshold_pct:
            return tier
    return None


def evaluate_exit(
    trade: TradeExitView,
    *,
    price: float,
    pnl_pct_value: float,
    now: Optional[datetime] = None,
    tiers: Optional[List[PartialTier]] = None,
    hard_sl_pct: Optional[float] = None,
    time_exit_hours: Optional[float] = None,
    time_exit_min_move_pct: Optional[float] = None,
    trailing_activation_pct: Optional[float] = None,
) -> ExitEvaluation:
    """Run one tick of the exit state machine.

    Priority:

    1. Hard SL (``pnl_pct <= -hard_sl_pct``).
    2. Partial-TP ladder (one rung per tick, lowest un-hit first).
    3. Trailing stop (only if armed).
    4. Time exit (only if trailing never armed AND no meaningful move).

    The helper never mutates ``trade``; it returns the new
    ``exit_state`` dict, the new peak price and whether the trailing
    should now be considered armed.
    """
    tiers = tiers if tiers is not None else settings.partial_tp_tiers
    hard_sl = hard_sl_pct if hard_sl_pct is not None else settings.hard_sl_pct
    t_hours = (
        time_exit_hours
        if time_exit_hours is not None
        else settings.time_exit_hours
    )
    t_min_move = (
        time_exit_min_move_pct
        if time_exit_min_move_pct is not None
        else settings.time_exit_min_move_pct
    )
    arm_pct = (
        trailing_activation_pct
        if trailing_activation_pct is not None
        else settings.trailing_activation_pct
    )
    now = now or datetime.now(timezone.utc)

    # ---- Copy state forward + update running max ------------------------
    state = dict(trade.exit_state or {})
    state.setdefault("tiers_hit", [])
    state.setdefault("trailing_pct", float(settings.trailing_pct))
    state.setdefault("max_pnl_pct_seen", 0.0)
    state.setdefault("realized_pnl_usd", 0.0)
    state.setdefault("partials", [])
    state["max_pnl_pct_seen"] = max(
        float(state["max_pnl_pct_seen"]), float(pnl_pct_value)
    )

    peak_price = trade.peak_price
    trailing_active = bool(trade.trailing_active)

    # ---- 1. Hard stop-loss ---------------------------------------------
    if pnl_pct_value <= -hard_sl:
        return ExitEvaluation(
            action=ExitAction.close(CloseReason.STOP_LOSS),
            new_exit_state=state,
            new_peak_price=peak_price,
            new_trailing_active=trailing_active,
        )

    # ---- 2. Partial-TP ladder ------------------------------------------
    tier = _untriggered_tier(pnl_pct_value, tiers, _tiers_hit(state))
    if tier is not None:
        close_shares = trade.current_shares * (tier.close_fraction_pct / 100.0)
        # First tier also arms the trailing — or, more generally, any
        # tier with threshold >= trailing_activation_pct arms it.  We
        # also update the peak so subsequent ticks track from here.
        if tier.pnl_threshold_pct >= arm_pct:
            trailing_active = True
            peak_price = max(peak_price or price, price)
        state["trailing_pct"] = float(tier.new_trailing_pct)
        return ExitEvaluation(
            action=ExitAction.partial(
                tier=tier.pnl_threshold_pct,
                close_shares=close_shares,
                new_trailing_pct=tier.new_trailing_pct,
            ),
            new_exit_state=state,
            new_peak_price=peak_price,
            new_trailing_active=trailing_active,
        )

    # ---- 3. Trailing stop ----------------------------------------------
    # Arm the trail once the standalone activation pct is crossed, even
    # if no ladder tier lives at that exact threshold.
    if not trailing_active and pnl_pct_value >= arm_pct:
        trailing_active = True
        peak_price = price

    if trailing_active:
        peak_price = max(peak_price or price, price)
        pullback = _trailing_pct(state) / 100.0
        trigger = peak_price * (1.0 - pullback)
        if price <= trigger:
            return ExitEvaluation(
                action=ExitAction.close(CloseReason.TRAILING_STOP),
                new_exit_state=state,
                new_peak_price=peak_price,
                new_trailing_active=trailing_active,
            )

    # ---- 4. Time exit --------------------------------------------------
    if (
        not trailing_active
        and float(state["max_pnl_pct_seen"]) < t_min_move
    ):
        age = now - trade.opened_at
        if age >= timedelta(hours=t_hours):
            return ExitEvaluation(
                action=ExitAction.close(CloseReason.TIME_EXIT),
                new_exit_state=state,
                new_peak_price=peak_price,
                new_trailing_active=trailing_active,
            )

    return ExitEvaluation(
        action=ExitAction.hold(),
        new_exit_state=state,
        new_peak_price=peak_price,
        new_trailing_active=trailing_active,
    )


# ---------------------------------------------------------------------------
#  Partial-close book-keeping (pure)
# ---------------------------------------------------------------------------


def record_partial(
    *,
    state: dict,
    tier: float,
    close_shares: float,
    close_price: float,
    entry_price: float,
    at: Optional[datetime] = None,
) -> dict:
    """Return a new ``exit_state`` that reflects the partial close.

    The caller is responsible for also reducing ``trade.shares`` and
    ``trade.amount_usd`` — this helper only touches the JSON bag.
    """
    realized = (close_price - entry_price) * close_shares
    new_state = dict(state or {})
    new_state.setdefault("tiers_hit", [])
    new_state.setdefault("partials", [])
    new_state.setdefault("realized_pnl_usd", 0.0)
    tiers_hit = list(new_state["tiers_hit"])
    if tier not in tiers_hit:
        tiers_hit.append(float(tier))
    new_state["tiers_hit"] = tiers_hit
    new_state["realized_pnl_usd"] = float(new_state["realized_pnl_usd"]) + float(
        realized
    )
    partials = list(new_state["partials"])
    partials.append(
        {
            "tier": float(tier),
            "price": float(close_price),
            "shares": float(close_shares),
            "pnl": float(realized),
            "at": (at or datetime.now(timezone.utc)).isoformat(),
        }
    )
    new_state["partials"] = partials
    return new_state


__all__ = [
    "ExitAction",
    "ExitActionKind",
    "ExitEvaluation",
    "TradeExitView",
    "empty_exit_state",
    "evaluate_exit",
    "record_partial",
]
