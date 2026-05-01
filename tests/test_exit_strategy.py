"""Repricing exit strategy — state machine + PnL aggregation.

These tests lock in the exit contract used by both the production
:class:`TradeMonitor` and the Monte-Carlo simulator:

* when ``HARD_SL_ALLOW_IMMEDIATE`` is True, **hard −HARD_SL_PCT floor**
  wins first; default is **False** so cheap positions are not chopped
  before trailing arms;
* the partial-TP ladder fires one rung per tick, lowest un-hit first,
  tightening the trailing pullback as it climbs;
* the trailing stop is the *only* top-side exit — there is no hard TP
  ceiling, so runners are unbounded;
* the time-exit gate only triggers when the trailing never armed AND
  no meaningful move materialised.

They also cover the book-keeping on ``record_partial`` (realized PnL
accumulator + ``partials[]`` audit trail) so downstream code can rely
on ``exit_state`` as the single source of truth.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config.settings import PartialTier, settings
from app.database.models import CloseReason
from app.services.exit_strategy import (
    ExitActionKind,
    TradeExitView,
    empty_exit_state,
    evaluate_exit,
    record_partial,
)


# ---------------------------------------------------------------------------
#  Fixtures
# ---------------------------------------------------------------------------

ENTRY = 0.05  # entry price — keeps pnl_pct math in small round numbers.
SHARES = 100.0

TIERS = [
    PartialTier(pnl_threshold_pct=40.0, close_fraction_pct=25.0, new_trailing_pct=25.0),
    PartialTier(pnl_threshold_pct=100.0, close_fraction_pct=25.0, new_trailing_pct=20.0),
    PartialTier(pnl_threshold_pct=200.0, close_fraction_pct=25.0, new_trailing_pct=15.0),
]


def _view(
    *,
    shares: float = SHARES,
    opened_at: datetime | None = None,
    peak_price: float | None = None,
    trailing_active: bool = False,
    state: dict | None = None,
) -> TradeExitView:
    return TradeExitView(
        entry_price=ENTRY,
        current_shares=shares,
        opened_at=opened_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
        peak_price=peak_price,
        trailing_active=trailing_active,
        exit_state=state if state is not None else empty_exit_state(),
    )


def _pnl_pct_for(price: float) -> float:
    return (price - ENTRY) / ENTRY * 100.0


# ---------------------------------------------------------------------------
#  Hard stop-loss
# ---------------------------------------------------------------------------


def test_hard_sl_fires_at_exact_threshold() -> None:
    # Price at -30 % from entry.
    price = ENTRY * 0.70
    pnl = _pnl_pct_for(price)
    ev = evaluate_exit(
        _view(),
        price=price,
        pnl_pct_value=pnl,
        tiers=TIERS,
        hard_sl_pct=30.0,
        hard_sl_allow_immediate=True,
    )
    assert ev.action.kind is ExitActionKind.CLOSE
    assert ev.action.close_reason is CloseReason.STOP_LOSS


def test_hard_sl_fires_below_threshold() -> None:
    price = ENTRY * 0.60  # -40 %
    pnl = _pnl_pct_for(price)
    ev = evaluate_exit(
        _view(),
        price=price,
        pnl_pct_value=pnl,
        tiers=TIERS,
        hard_sl_pct=30.0,
        hard_sl_allow_immediate=True,
    )
    assert ev.action.kind is ExitActionKind.CLOSE
    assert ev.action.close_reason is CloseReason.STOP_LOSS


def test_small_drawdown_does_not_close() -> None:
    opened = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = opened + timedelta(minutes=1)
    price = ENTRY * 0.85  # -15 %, well above the floor
    pnl = _pnl_pct_for(price)
    ev = evaluate_exit(
        _view(opened_at=opened),
        price=price,
        pnl_pct_value=pnl,
        now=now,
        tiers=TIERS,
        hard_sl_pct=30.0,
        hard_sl_allow_immediate=True,
    )
    assert ev.action.kind is ExitActionKind.HOLD


# ---------------------------------------------------------------------------
#  Partial-TP ladder
# ---------------------------------------------------------------------------


def test_first_tier_fires_at_40_pct_and_arms_trailing() -> None:
    # Pin the price comfortably above +40 % (0.05 * 1.40 has float drift
    # to 0.0699999... which is just below the tier threshold).
    price = 0.071
    pnl = _pnl_pct_for(price)
    ev = evaluate_exit(
        _view(),
        price=price,
        pnl_pct_value=pnl,
        tiers=TIERS,
        trailing_activation_pct=40.0,
    )
    assert ev.action.kind is ExitActionKind.PARTIAL
    assert ev.action.tier == 40.0
    assert ev.action.close_shares == SHARES * 0.25
    assert ev.action.new_trailing_pct == 25.0
    # Trailing is armed at the first tier.
    assert ev.new_trailing_active is True
    assert ev.new_peak_price == price
    assert ev.new_exit_state["trailing_pct"] == 25.0


def test_ladder_fires_one_tier_per_tick() -> None:
    # Price spikes past +100 % in one tick but we only close the FIRST
    # un-hit tier this tick; tier 2 will fire on the next tick.
    price = ENTRY * 2.00  # +100 %
    pnl = _pnl_pct_for(price)
    state = empty_exit_state()

    ev1 = evaluate_exit(
        _view(state=state), price=price, pnl_pct_value=pnl, tiers=TIERS
    )
    assert ev1.action.kind is ExitActionKind.PARTIAL
    assert ev1.action.tier == 40.0
    state_after_tier1 = ev1.new_exit_state
    # tier 1 not recorded in tiers_hit until record_partial is called by
    # the caller; here we emulate that:
    state_after_tier1 = record_partial(
        state=state_after_tier1,
        tier=40.0,
        close_shares=ev1.action.close_shares or 0.0,
        close_price=price,
        entry_price=ENTRY,
    )

    # Next tick — tier 2 should now fire.
    ev2 = evaluate_exit(
        _view(
            shares=SHARES - (ev1.action.close_shares or 0.0),
            state=state_after_tier1,
            peak_price=ev1.new_peak_price,
            trailing_active=ev1.new_trailing_active,
        ),
        price=price,
        pnl_pct_value=pnl,
        tiers=TIERS,
    )
    assert ev2.action.kind is ExitActionKind.PARTIAL
    assert ev2.action.tier == 100.0
    assert ev2.action.new_trailing_pct == 20.0
    # Tier 2 closes 25 % of the REMAINING shares (75 of original 100).
    assert ev2.action.close_shares == 75.0 * 0.25


def test_higher_tier_tightens_trailing_pct() -> None:
    state = empty_exit_state()
    # Pretend tier 1 already fired so this tick should pick tier 2.
    state["tiers_hit"] = [40.0]
    state["trailing_pct"] = 25.0
    price = ENTRY * 2.00  # +100 %
    pnl = _pnl_pct_for(price)
    ev = evaluate_exit(
        _view(state=state, trailing_active=True, peak_price=price),
        price=price,
        pnl_pct_value=pnl,
        tiers=TIERS,
    )
    assert ev.action.kind is ExitActionKind.PARTIAL
    assert ev.action.tier == 100.0
    assert ev.new_exit_state["trailing_pct"] == 20.0


def test_third_tier_tightens_to_15_pct() -> None:
    state = empty_exit_state()
    state["tiers_hit"] = [40.0, 100.0]
    state["trailing_pct"] = 20.0
    price = ENTRY * 3.00  # +200 %
    pnl = _pnl_pct_for(price)
    ev = evaluate_exit(
        _view(state=state, trailing_active=True, peak_price=price),
        price=price,
        pnl_pct_value=pnl,
        tiers=TIERS,
    )
    assert ev.action.kind is ExitActionKind.PARTIAL
    assert ev.action.tier == 200.0
    assert ev.new_exit_state["trailing_pct"] == 15.0


def test_runner_is_uncapped_above_top_tier() -> None:
    state = empty_exit_state()
    state["tiers_hit"] = [40.0, 100.0, 200.0]
    state["trailing_pct"] = 15.0
    price = ENTRY * 10.00  # +900 %
    pnl = _pnl_pct_for(price)
    ev = evaluate_exit(
        _view(state=state, trailing_active=True, peak_price=price),
        price=price,
        pnl_pct_value=pnl,
        tiers=TIERS,
    )
    # No more tiers, no trailing trigger — runner keeps holding.
    assert ev.action.kind is ExitActionKind.HOLD
    assert ev.new_peak_price == price


# ---------------------------------------------------------------------------
#  Trailing stop
# ---------------------------------------------------------------------------


def test_trailing_triggers_after_pullback_from_peak() -> None:
    state = empty_exit_state()
    state["tiers_hit"] = [40.0]
    state["trailing_pct"] = 25.0
    peak = ENTRY * 1.50
    # 25 % pullback from the peak.
    price = peak * (1 - 0.25) - 1e-6
    pnl = _pnl_pct_for(price)
    ev = evaluate_exit(
        _view(state=state, trailing_active=True, peak_price=peak),
        price=price,
        pnl_pct_value=pnl,
        tiers=TIERS,
    )
    assert ev.action.kind is ExitActionKind.CLOSE
    assert ev.action.close_reason is CloseReason.TRAILING_STOP


def test_trailing_does_not_trigger_within_pullback() -> None:
    state = empty_exit_state()
    state["tiers_hit"] = [40.0]
    state["trailing_pct"] = 25.0
    peak = ENTRY * 1.50
    # Only 10 % pullback — still inside the 25 % tolerance.
    price = peak * 0.90
    pnl = _pnl_pct_for(price)
    ev = evaluate_exit(
        _view(state=state, trailing_active=True, peak_price=peak),
        price=price,
        pnl_pct_value=pnl,
        tiers=TIERS,
    )
    assert ev.action.kind is ExitActionKind.HOLD


# ---------------------------------------------------------------------------
#  Time exit
# ---------------------------------------------------------------------------


def test_time_exit_fires_when_cold_trade_ages_out() -> None:
    opened = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    now = opened + timedelta(hours=9)
    # Tiny move, well below the 20 % min-move threshold.
    price = ENTRY * 1.05
    pnl = _pnl_pct_for(price)
    ev = evaluate_exit(
        _view(opened_at=opened),
        price=price,
        pnl_pct_value=pnl,
        now=now,
        tiers=TIERS,
        time_exit_hours=8.0,
        time_exit_min_move_pct=20.0,
    )
    assert ev.action.kind is ExitActionKind.CLOSE
    assert ev.action.close_reason is CloseReason.TIME_EXIT


def test_time_exit_does_not_fire_while_trailing_armed() -> None:
    opened = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    now = opened + timedelta(hours=20)
    state = empty_exit_state()
    state["tiers_hit"] = [40.0]
    state["trailing_pct"] = 25.0
    state["max_pnl_pct_seen"] = 120.0  # well past min-move
    peak = ENTRY * 2.00
    # Gentle pullback — trailing NOT triggered, but trailing is armed so
    # the time-exit gate must defer.
    price = peak * 0.92
    pnl = _pnl_pct_for(price)
    ev = evaluate_exit(
        _view(
            opened_at=opened,
            peak_price=peak,
            trailing_active=True,
            state=state,
        ),
        price=price,
        pnl_pct_value=pnl,
        now=now,
        tiers=TIERS,
        time_exit_hours=8.0,
        time_exit_min_move_pct=20.0,
    )
    assert ev.action.kind is ExitActionKind.HOLD


def test_time_exit_does_not_fire_when_min_move_exceeded() -> None:
    opened = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    now = opened + timedelta(hours=20)
    state = empty_exit_state()
    # Trade saw +25 % at some point but has now drifted back to flat.
    state["max_pnl_pct_seen"] = 25.0
    price = ENTRY * 1.01
    pnl = _pnl_pct_for(price)
    ev = evaluate_exit(
        _view(opened_at=opened, state=state),
        price=price,
        pnl_pct_value=pnl,
        now=now,
        tiers=TIERS,
        time_exit_hours=8.0,
        time_exit_min_move_pct=20.0,
    )
    assert ev.action.kind is ExitActionKind.HOLD


def test_time_exit_waits_until_window() -> None:
    opened = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    now = opened + timedelta(hours=4)  # still inside the 8h window
    price = ENTRY * 1.05
    pnl = _pnl_pct_for(price)
    ev = evaluate_exit(
        _view(opened_at=opened),
        price=price,
        pnl_pct_value=pnl,
        now=now,
        tiers=TIERS,
        time_exit_hours=8.0,
        time_exit_min_move_pct=20.0,
    )
    assert ev.action.kind is ExitActionKind.HOLD


# ---------------------------------------------------------------------------
#  record_partial / PnL aggregation
# ---------------------------------------------------------------------------


def test_record_partial_accumulates_realized_pnl() -> None:
    state = empty_exit_state()
    # Close 25 shares at 0.07 — realize 25 × (0.07 - 0.05) = 0.50 USD.
    state = record_partial(
        state=state,
        tier=40.0,
        close_shares=25.0,
        close_price=0.07,
        entry_price=ENTRY,
    )
    assert state["tiers_hit"] == [40.0]
    assert round(state["realized_pnl_usd"], 4) == 0.50
    assert len(state["partials"]) == 1

    # Close another 25 shares at 0.10 — realize 25 × 0.05 = 1.25 USD more.
    state = record_partial(
        state=state,
        tier=100.0,
        close_shares=25.0,
        close_price=0.10,
        entry_price=ENTRY,
    )
    assert state["tiers_hit"] == [40.0, 100.0]
    assert round(state["realized_pnl_usd"], 4) == round(0.50 + 1.25, 4)
    assert len(state["partials"]) == 2


def test_record_partial_is_idempotent_on_tier_threshold() -> None:
    state = empty_exit_state()
    state = record_partial(
        state=state,
        tier=40.0,
        close_shares=25.0,
        close_price=0.07,
        entry_price=ENTRY,
    )
    # Calling again with the same tier must NOT duplicate the entry in
    # ``tiers_hit`` (keeps _untriggered_tier() logic sound).
    state = record_partial(
        state=state,
        tier=40.0,
        close_shares=10.0,
        close_price=0.08,
        entry_price=ENTRY,
    )
    assert state["tiers_hit"].count(40.0) == 1


# ---------------------------------------------------------------------------
#  max_pnl_pct_seen monotonicity
# ---------------------------------------------------------------------------


def test_max_pnl_pct_seen_is_monotonic() -> None:
    # First tick: +30 %.
    ev1 = evaluate_exit(
        _view(), price=ENTRY * 1.30, pnl_pct_value=30.0, tiers=TIERS
    )
    assert ev1.new_exit_state["max_pnl_pct_seen"] == 30.0

    # Next tick: price pulls back to +10 % — max stays at 30.
    ev2 = evaluate_exit(
        _view(state=ev1.new_exit_state),
        price=ENTRY * 1.10,
        pnl_pct_value=10.0,
        tiers=TIERS,
    )
    assert ev2.new_exit_state["max_pnl_pct_seen"] == 30.0


# ---------------------------------------------------------------------------
#  Default ladder smoke-test (picks up parsed settings)
# ---------------------------------------------------------------------------


def test_default_partial_tp_tiers_parse() -> None:
    tiers = settings.partial_tp_tiers
    thresholds = [t.pnl_threshold_pct for t in tiers]
    assert thresholds == sorted(thresholds)
    if tiers:
        trailings = [t.new_trailing_pct for t in tiers]
        assert trailings == sorted(trailings, reverse=True) or len(set(trailings)) == 1


def test_deep_loss_holds_when_immediate_hard_sl_disabled() -> None:
    """Without immediate hard SL, −40 % unrealized stays open (repricing chop)."""
    opened = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    now = opened + timedelta(minutes=2)
    price = ENTRY * 0.60  # −40 %
    pnl = _pnl_pct_for(price)
    ev = evaluate_exit(
        _view(opened_at=opened),
        price=price,
        pnl_pct_value=pnl,
        tiers=[],
        hard_sl_pct=30.0,
        hard_sl_allow_immediate=False,
        now=now,
        time_exit_hours=48.0,
    )
    assert ev.action.kind is ExitActionKind.HOLD


def test_trailing_exit_after_arm_with_no_partial_tiers() -> None:
    """+50 % arms trailing; −20 % peak pullback closes (PARTIAL_TP empty)."""
    opened = datetime(2026, 3, 1, 14, 0, tzinfo=timezone.utc)
    now1 = opened + timedelta(minutes=5)
    price_hi = ENTRY * 1.50
    pnl_hi = _pnl_pct_for(price_hi)
    ev1 = evaluate_exit(
        _view(opened_at=opened),
        price=price_hi,
        pnl_pct_value=pnl_hi,
        tiers=[],
        trailing_activation_pct=40.0,
        hard_sl_allow_immediate=False,
        now=now1,
        time_exit_hours=48.0,
    )
    assert ev1.action.kind is ExitActionKind.HOLD
    assert ev1.new_trailing_active is True

    peak = price_hi
    trailing_pull = settings.trailing_pct / 100.0
    price_lo = peak * (1.0 - trailing_pull) - 1e-9
    pnl_lo = _pnl_pct_for(price_lo)
    now2 = opened + timedelta(minutes=6)
    ev2 = evaluate_exit(
        _view(
            opened_at=opened,
            state=ev1.new_exit_state,
            trailing_active=ev1.new_trailing_active,
            peak_price=peak,
        ),
        price=price_lo,
        pnl_pct_value=pnl_lo,
        tiers=[],
        hard_sl_allow_immediate=False,
        now=now2,
        time_exit_hours=48.0,
    )
    assert ev2.action.kind is ExitActionKind.CLOSE
    assert ev2.action.close_reason is CloseReason.TRAILING_STOP
