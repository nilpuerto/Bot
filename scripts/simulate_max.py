"""Monte Carlo MAX mode simulator.

Honest disclaimer
-----------------
This is **not** a backtest against historical Polymarket fills.  It is a
parametric Monte Carlo that combines:

* The exact gate / sizing logic in ``app.services.max_sizer`` and the
  early-fire / deadline rules in ``app.services.max_sniper`` (translated
  to plain Python below — pure functions, no asyncio).
* Plausible distributions for confidence, |window_delta|, fill prices,
  and conditional win-rates per tier.

Each assumption is configurable.  The output is a *range of outcomes*
under those assumptions — useful for reasoning about risk/reward, not as
a profit forecast.

Usage::

    python scripts/simulate_max.py
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from typing import Iterable

from app.config.settings import settings


WINDOWS_PER_DAY = 288  # 5-minute windows in 24h


@dataclass
class Scenario:
    name: str
    # Window-resolution / data quality
    p_market_found: float
    p_oracle_live: float
    p_decisive_signal: float
    # Confidence buckets (must sum to 1.0)
    p_strong_conf: float
    p_weak_conf: float
    p_subweak_conf: float
    # |window_delta| % distribution (mean per bucket)
    delta_strong_mean: float
    delta_weak_mean: float
    delta_subweak_mean: float
    # Liquidity / asks
    p_no_asks: float
    ask_decisive_mean: float
    ask_normal_mean: float
    # Order-book depth per snapshot per side (USD).  Real BTC 5m books on
    # Polymarket are TINY — total volume per market is often <$200, so a
    # single fill rarely takes more than $30-60 at the best ask.  The
    # simulator caps each bet at this so compounding can't run infinite.
    book_depth_usd_mean: float
    book_depth_usd_std: float
    # Edge in *percentage points* over the implied probability of the
    # picked side.  Implied prob of winning when buying at ``ask`` = ``ask``
    # (Polymarket pays $1 if you're right, $0 otherwise; efficient price
    # = probability).  edge_pp = 0 -> coin-flip predictor (~ break-even).
    # edge_pp = 3 -> 3 pp above implied (e.g. 51 % when ask = 0.48).
    edge_pp_strong: float
    edge_pp_weak: float
    edge_pp_deadline: float


# ----------------------------------------------------------------------
# Calibration notes
#
# A 5-min BTC window is "strong" only when delta is decisive *and*
# indicators agree.  In practice score >= 3.5 (confidence >= 0.50)
# happens in 3-7 % of windows.  "Weak" tier covers another 8-15 %.
# The rest are flat or sub-weak and the bot skips them.
#
# Edge is expressed in *percentage points above implied probability*
# (1 - ask).  edge_pp = 0 -> coin flip (no skill).  Empirically nobody
# publishes verified live numbers on these; we reason about ranges.
# ----------------------------------------------------------------------

NO_EDGE = Scenario(
    name="no_edge",
    p_market_found=0.95,
    p_oracle_live=0.90,
    p_decisive_signal=0.07,
    p_strong_conf=0.06,
    p_weak_conf=0.13,
    p_subweak_conf=0.81,
    delta_strong_mean=0.12,
    delta_weak_mean=0.06,
    delta_subweak_mean=0.013,
    p_no_asks=0.20,
    ask_decisive_mean=0.58,
    ask_normal_mean=0.48,
    book_depth_usd_mean=28.0,
    book_depth_usd_std=12.0,
    # No skill: win_rate == implied prob (1 - ask).  Slight expected
    # negative EV after slippage / no-edge noise.
    edge_pp_strong=0.0,
    edge_pp_weak=0.0,
    edge_pp_deadline=0.0,
)

CONSERVATIVE = Scenario(
    name="conservative",
    p_market_found=0.92,
    p_oracle_live=0.85,
    p_decisive_signal=0.04,
    p_strong_conf=0.04,
    p_weak_conf=0.10,
    p_subweak_conf=0.86,
    delta_strong_mean=0.10,
    delta_weak_mean=0.05,
    delta_subweak_mean=0.012,
    p_no_asks=0.25,
    ask_decisive_mean=0.62,
    ask_normal_mean=0.52,
    book_depth_usd_mean=18.0,
    book_depth_usd_std=8.0,
    edge_pp_strong=2.0,
    edge_pp_weak=1.0,
    edge_pp_deadline=0.0,
)

BASE = Scenario(
    name="base",
    p_market_found=0.95,
    p_oracle_live=0.90,
    p_decisive_signal=0.07,
    p_strong_conf=0.06,
    p_weak_conf=0.13,
    p_subweak_conf=0.81,
    delta_strong_mean=0.12,
    delta_weak_mean=0.06,
    delta_subweak_mean=0.013,
    p_no_asks=0.20,
    ask_decisive_mean=0.58,
    ask_normal_mean=0.48,
    book_depth_usd_mean=28.0,
    book_depth_usd_std=12.0,
    edge_pp_strong=4.0,
    edge_pp_weak=2.0,
    edge_pp_deadline=1.0,
)

OPTIMISTIC = Scenario(
    name="optimistic",
    p_market_found=0.97,
    p_oracle_live=0.95,
    p_decisive_signal=0.10,
    p_strong_conf=0.08,
    p_weak_conf=0.16,
    p_subweak_conf=0.76,
    delta_strong_mean=0.14,
    delta_weak_mean=0.07,
    delta_subweak_mean=0.014,
    p_no_asks=0.15,
    ask_decisive_mean=0.55,
    ask_normal_mean=0.45,
    book_depth_usd_mean=40.0,
    book_depth_usd_std=18.0,
    edge_pp_strong=7.0,
    edge_pp_weak=3.0,
    edge_pp_deadline=1.5,
)


def _sample_confidence(scn: Scenario) -> float:
    """Return confidence in (0, 1)."""
    r = random.random()
    if r < scn.p_strong_conf:
        m = max(settings.max_min_confidence + 0.10, 0.55)
        return min(0.95, max(settings.max_min_confidence, random.gauss(m, 0.07)))
    elif r < scn.p_strong_conf + scn.p_weak_conf:
        lo, hi = settings.max_weak_confidence_floor, settings.max_min_confidence
        return min(hi - 1e-3, max(lo, random.gauss((lo + hi) / 2, (hi - lo) / 3)))
    else:
        return max(0.0, min(settings.max_weak_confidence_floor - 1e-3, random.gauss(0.10, 0.05)))


def _sample_delta(conf: float, scn: Scenario) -> float:
    """Sample |window_delta| % with realistic dispersion per tier."""
    if conf >= settings.max_min_confidence:
        m = scn.delta_strong_mean
    elif conf >= settings.max_weak_confidence_floor:
        m = scn.delta_weak_mean
    else:
        m = scn.delta_subweak_mean
    return max(0.0, abs(random.gauss(m, m * 0.6)))


def _sample_ask(decisive: bool, scn: Scenario) -> float | None:
    """Return ask price or None when book is empty (no asks)."""
    if random.random() < scn.p_no_asks:
        if not settings.max_use_limit_fallback:
            return None
        return float(settings.max_limit_fallback_price)
    m = scn.ask_decisive_mean if decisive else scn.ask_normal_mean
    return min(0.99, max(0.05, random.gauss(m, 0.07)))


def _confidence_multiplier(conf: float, deadline_forced: bool, ad: float) -> float:
    if conf >= settings.max_min_confidence:
        return 1.0
    if conf >= settings.max_weak_confidence_floor:
        return float(settings.max_weak_trade_fraction)
    if deadline_forced and ad >= float(settings.max_deadline_delta_abs_pct):
        return float(settings.max_deadline_trade_fraction)
    return 0.0


def _gates_pass(
    conf: float,
    ad: float,
    ask: float | None,
    deadline_forced: bool,
    decisive: bool,
) -> tuple[bool, str]:
    """Mirror the orchestrator + sniper skip rules."""
    if ask is None:
        return False, "no_liquidity"

    if deadline_forced:
        if conf < settings.max_min_confidence and ad < float(
            settings.max_flat_deadline_skip_abs_pct
        ):
            return False, "flat_deadline"
        if conf < settings.max_weak_confidence_floor and ad < float(
            settings.max_deadline_delta_abs_pct
        ):
            return False, "deadline_micro_edge"

    upside = 1.0 - ask
    if upside < float(settings.max_min_token_upside):
        return False, "bad_token_upside"

    if settings.max_relaxed_entry_decisive_only:
        cap = (
            float(settings.max_relaxed_max_entry_price)
            if decisive
            else float(settings.max_max_entry_price)
        )
    else:
        cap = float(settings.max_relaxed_max_entry_price)
    if ask >= cap:
        return False, "ask_too_high"

    if _confidence_multiplier(conf, deadline_forced, ad) <= 0.0:
        return False, "low_confidence"

    return True, "ok"


@dataclass
class Result:
    trades: int
    wins: int
    losses: int
    skips: dict
    pnl: float
    win_rate: float
    avg_size: float
    biggest_win: float
    biggest_loss: float
    drawdown_max: float
    final_balance: float


def simulate(
    scenario: Scenario,
    *,
    starting_balance: float = 200.0,
    days: int = 30,
    seed: int | None = None,
) -> Result:
    rng = random.Random(seed)
    random.seed(seed)

    balance = float(starting_balance)
    cum_profit = 0.0
    open_usd = 0.0
    skips: dict[str, int] = {}
    trades = wins = losses = 0
    pnl = 0.0
    biggest_win = 0.0
    biggest_loss = 0.0
    peak = balance
    max_dd = 0.0

    for _ in range(days * WINDOWS_PER_DAY):
        if rng.random() > scenario.p_market_found:
            skips["no_market"] = skips.get("no_market", 0) + 1
            continue

        conf = _sample_confidence(scenario)
        decisive = rng.random() < scenario.p_decisive_signal
        ad = _sample_delta(conf, scenario)
        deadline_forced = conf < settings.max_min_confidence and not (
            conf >= settings.max_weak_confidence_floor
            and ad >= float(settings.max_early_delta_abs_pct)
        )
        ask = _sample_ask(decisive, scenario)

        ok, reason = _gates_pass(conf, ad, ask, deadline_forced, decisive)
        if not ok:
            skips[reason] = skips.get(reason, 0) + 1
            continue

        mult = _confidence_multiplier(conf, deadline_forced, ad)
        if cum_profit > 0:
            nominal = cum_profit
            fb = False
        else:
            nominal = balance * float(settings.max_bankroll_fallback_pct) / 100.0
            fb = True
        bet = nominal * mult
        cap = balance * float(settings.max_per_trade_cap_pct) / 100.0
        if decisive:
            cap *= 1.25
        bet = min(bet, cap)
        bet = min(bet, balance * float(settings.max_concurrent_cap_pct) / 100.0 - open_usd)

        depth = max(
            float(settings.min_trade_usd),
            rng.gauss(scenario.book_depth_usd_mean, scenario.book_depth_usd_std),
        )
        if bet > depth:
            bet = depth

        if bet < float(settings.min_trade_usd):
            skips["below_min"] = skips.get("below_min", 0) + 1
            continue

        upside = 1.0 - ask  # USD per share if YES, but we trade in USD-of-shares
        # In Polymarket, buying $bet at ask buys $bet / ask shares.
        # If win → share resolves at $1 → payout = bet/ask. PnL = bet*(1/ask - 1) = bet*upside/ask.
        # If loss → loses entire bet.
        # Edge model: p_win = ask + edge_pp/100, clipped to (0.05, 0.95).
        # On Polymarket buying at price=ask means implied P(win) = ask
        # for an efficient book.  edge_pp is the predictor's expected
        # outperformance vs the implied probability.
        if conf >= float(settings.max_min_confidence):
            edge_pp = scenario.edge_pp_strong
        elif conf >= float(settings.max_weak_confidence_floor):
            edge_pp = scenario.edge_pp_weak
        else:
            edge_pp = scenario.edge_pp_deadline
        if decisive:
            edge_pp += 1.5
        p_win = max(0.05, min(0.95, ask + edge_pp / 100.0))

        if rng.random() < p_win:
            payout = bet * (1.0 / ask)  # full win: token resolves at 1
            profit = payout - bet
            wins += 1
            biggest_win = max(biggest_win, profit)
        else:
            profit = -bet
            losses += 1
            biggest_loss = min(biggest_loss, profit)

        trades += 1
        balance += profit
        cum_profit += profit
        pnl += profit

        peak = max(peak, balance)
        max_dd = max(max_dd, peak - balance)

        # Hard floor: stop trading if balance drops below MIN_TRADE_USD.
        if balance < float(settings.min_trade_usd):
            break

    win_rate = wins / trades if trades else 0.0
    avg_size = pnl / trades if trades else 0.0
    return Result(
        trades=trades,
        wins=wins,
        losses=losses,
        skips=skips,
        pnl=pnl,
        win_rate=win_rate,
        avg_size=avg_size,
        biggest_win=biggest_win,
        biggest_loss=biggest_loss,
        drawdown_max=max_dd,
        final_balance=balance,
    )


def aggregate(scn: Scenario, *, days: int, runs: int = 200, balance: float = 200.0):
    pnls: list[float] = []
    finals: list[float] = []
    trade_counts: list[int] = []
    win_rates: list[float] = []
    drawdowns: list[float] = []
    for i in range(runs):
        r = simulate(scn, starting_balance=balance, days=days, seed=10_000 + i)
        pnls.append(r.pnl)
        finals.append(r.final_balance)
        trade_counts.append(r.trades)
        win_rates.append(r.win_rate)
        drawdowns.append(r.drawdown_max)

    def pct(xs: Iterable[float], q: float) -> float:
        s = sorted(xs)
        if not s:
            return 0.0
        k = max(0, min(len(s) - 1, int(q * len(s))))
        return s[k]

    return {
        "scenario": scn.name,
        "days": days,
        "runs": runs,
        "trades_per_period_p50": int(statistics.median(trade_counts)),
        "trades_per_day_p50": round(statistics.median(trade_counts) / days, 1),
        "win_rate_mean": round(statistics.mean(win_rates), 3),
        "pnl_p10": round(pct(pnls, 0.10), 2),
        "pnl_p50": round(pct(pnls, 0.50), 2),
        "pnl_p90": round(pct(pnls, 0.90), 2),
        "pnl_mean": round(statistics.mean(pnls), 2),
        "final_balance_p50": round(pct(finals, 0.50), 2),
        "max_drawdown_p50": round(pct(drawdowns, 0.50), 2),
        "max_drawdown_p90": round(pct(drawdowns, 0.90), 2),
        "ruined_pct": round(100.0 * sum(1 for f in finals if f < float(settings.min_trade_usd) * 1.5) / runs, 1),
    }


def break_even_analysis() -> None:
    """For each scenario, print the edge required for EV/trade = 0."""
    print("\nBreak-even analysis (Polymarket pays $1 on win, $0 on loss):")
    for scn in (NO_EDGE, CONSERVATIVE, BASE, OPTIMISTIC):
        print(
            f"  {scn.name:<13}: ask ~ {scn.ask_normal_mean:.2f} ; edge_pp = "
            f"{scn.edge_pp_strong:.1f}/{scn.edge_pp_weak:.1f}/"
            f"{scn.edge_pp_deadline:.1f} (strong/weak/deadline)"
        )


if __name__ == "__main__":
    print("\nMAX mode Monte Carlo — read the file docstring for caveats.")
    print("Starting balance per run: $200, runs/scenario: 200\n")

    print(f"{'scn':<13} | {'days':>4} | {'tr/d p50':>8} | {'win%':>5} | "
          f"{'pnl p10':>9} {'p50':>8} {'p90':>9} | {'mean':>9} | "
          f"{'DD p50':>7} {'p90':>7} | {'ruin%':>5}")
    print("-" * 130)
    for scn in (NO_EDGE, CONSERVATIVE, BASE, OPTIMISTIC):
        for days in (1, 30):
            r = aggregate(scn, days=days, runs=200, balance=200.0)
            print(
                f"{scn.name:<13} | "
                f"{days:>4}d | "
                f"{r['trades_per_day_p50']:>8} | "
                f"{r['win_rate_mean']*100:>4.1f}% | "
                f"{r['pnl_p10']:>+9.2f} {r['pnl_p50']:>+8.2f} {r['pnl_p90']:>+9.2f} | "
                f"{r['pnl_mean']:>+9.2f} | "
                f"{r['max_drawdown_p50']:>7.1f} {r['max_drawdown_p90']:>7.1f} | "
                f"{r['ruined_pct']:>4.1f}%"
            )
    break_even_analysis()
