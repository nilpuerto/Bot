"""15-day Monte-Carlo simulation of the Prym Signals pipeline.

Design goals
------------
1. **Honest** — price paths are driftless log-normal walks.  Over an
   infinite population the expected unrealized return of a random-walk
   trade is zero; any positive expectancy the simulator reports comes
   *entirely* from the asymmetric exit rules (partial TP + trailing
   runner + hard SL + time exit) capturing positive paths more
   efficiently than they clip negative ones.
2. **Realistic** — we drive the simulation through the *actual*
   :class:`PrymStrategy`, :class:`SignalScoringSystem`, and
   :class:`TradeLimiter`-equivalent rules.  Exits are managed by the
   *exact same* :func:`evaluate_exit` helper the production
   :class:`TradeMonitor` uses, so the simulator and live bot share the
   state machine.
3. **Reproducible** — pass ``--seed N`` for a deterministic run;
   otherwise every invocation is a fresh roll.

Run it::

    python -m scripts.simulate_15d
    python -m scripts.simulate_15d --seed 42
    python -m scripts.simulate_15d --mc 500    # 500-run Monte Carlo summary

Output
------
* Daily table: news seen / filtered / signals / trades / PnL / balance
* Final breakdown: total trades, win-rate, PnL ($), PnL (%), max
  drawdown, ladder hit-rates, runner multiples, close-reason mix
* Monte-Carlo mode (``--mc N``): percentile distribution over N seeds
"""
from __future__ import annotations

import argparse
import math
import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from app.config.settings import settings
from app.database.models import CloseReason
from app.integrations.mistral_client import AIAnalysis
from app.integrations.polymarket_client import MarketSnapshot
from app.services.exit_strategy import (
    ExitActionKind,
    TradeExitView,
    empty_exit_state,
    evaluate_exit,
    record_partial,
)
from app.services.mispricing import MispricingResult
from app.services.signal_scoring import SignalScoringSystem
from app.services.timing import PHASE_LABEL, TimingDecision
from app.strategies.prym_strategy import PrymStrategy


# ---------------------------------------------------------------------------
#  Tunables — the "realistic" distribution of news + markets.
# ---------------------------------------------------------------------------

NEWS_PER_DAY_MEAN = 350          # how many RSS items we see per day
HARD_FILTER_PASS_RATE = 0.10     # share of items that contain a hot keyword
MARKET_MATCH_RATE = 0.40         # AI-relevant news that also match a market

URGENCY_WEIGHTS = {  # urgency -> probability
    3: 0.05, 4: 0.10, 5: 0.15, 6: 0.15,
    7: 0.20, 8: 0.15, 9: 0.12, 10: 0.08,
}
CONFIDENCE_MEAN = 62
CONFIDENCE_STD = 18


def _sample_market_price(rng: random.Random) -> float:
    r = rng.random()
    if r < 0.35:
        return round(rng.uniform(0.03, 0.10), 3)
    if r < 0.65:
        return round(rng.uniform(0.10, 0.35), 3)
    return round(rng.uniform(0.35, 0.90), 3)


VOLUME_MIN, VOLUME_MAX = 1_000, 200_000
STARTING_BALANCE = 400.0

PHASE_WEIGHTS = {1: 0.12, 2: 0.33, 3: 0.28, 4: 0.17, 5: 0.10}
Z_MEAN = 1.6
Z_STD = 1.2
NET_EDGE_MEAN = 5.0
NET_EDGE_STD = 6.0
FILL_RATIO_MEAN = 0.84
FILL_RATIO_STD = 0.10

# --- Price-path model -------------------------------------------------------
#
# Polymarket small-cap binaries are noisy but mean-reverting over short
# horizons.  We model the post-entry mid-price as a zero-drift Geometric
# Brownian Motion with a soft-clip at (0.001, 0.999) so the walk stays
# in the open probability interval.
#
# ``PRICE_PATH_HOURLY_SIGMA``: hourly log-return volatility.  3–5 %
#   is typical for small-cap event markets; configurable via CLI.
# ``PRICE_PATH_STEP_SECONDS``: granularity; 60 s matches the production
#   monitor tick.
# ``PRICE_PATH_MAX_HOURS_MULT``: hard cap to bound simulator runtime —
#   we rely on the time-exit rule to close cold trades within
#   ``TIME_EXIT_HOURS``, so 2× that window is always sufficient.
PRICE_PATH_HOURLY_SIGMA = 0.04
PRICE_PATH_STEP_SECONDS = 60
PRICE_PATH_MAX_HOURS_MULT = 2.0


# ---------------------------------------------------------------------------
#  Data structures
# ---------------------------------------------------------------------------

@dataclass
class DayStats:
    day: int
    news_seen: int = 0
    news_filtered: int = 0
    signals_scored: int = 0
    trades_opened: int = 0
    trades_won: int = 0
    trades_lost: int = 0
    pnl_usd: float = 0.0
    balance_eod: float = 0.0


@dataclass
class TradeResult:
    day: int
    side: str
    entry_price: float
    amount_usd: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    won: bool
    pnl_usd: float
    high_confidence: bool
    band: str = "mid"
    tiers_hit: List[float] = field(default_factory=list)
    close_reason: str = "unknown"
    max_pnl_pct_seen: float = 0.0
    close_pnl_pct: float = 0.0


@dataclass
class SimResult:
    days: List[DayStats] = field(default_factory=list)
    trades: List[TradeResult] = field(default_factory=list)
    starting_balance: float = STARTING_BALANCE
    ending_balance: float = STARTING_BALANCE

    @property
    def pnl_usd(self) -> float:
        return self.ending_balance - self.starting_balance

    @property
    def pnl_pct(self) -> float:
        return (self.pnl_usd / self.starting_balance) * 100.0

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.won)
        return 100.0 * wins / len(self.trades)

    @property
    def max_drawdown_pct(self) -> float:
        if not self.days:
            return 0.0
        peak = self.starting_balance
        worst = 0.0
        for d in self.days:
            peak = max(peak, d.balance_eod)
            dd = (d.balance_eod - peak) / peak * 100
            worst = min(worst, dd)
        return worst


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _sample_urgency(rng: random.Random) -> int:
    r = rng.random()
    cumulative = 0.0
    for urg, w in URGENCY_WEIGHTS.items():
        cumulative += w
        if r <= cumulative:
            return urg
    return 5


def _sample_confidence(rng: random.Random) -> int:
    v = rng.gauss(CONFIDENCE_MEAN, CONFIDENCE_STD)
    return max(0, min(100, int(round(v))))


def _build_ai(rng: random.Random) -> AIAnalysis:
    urgency = _sample_urgency(rng)
    confidence = _sample_confidence(rng)
    impact = rng.choices(
        ["bullish", "bearish", "neutral"], weights=[0.45, 0.35, 0.20]
    )[0]
    return AIAnalysis(
        market="synthetic", impact=impact, urgency=urgency, confidence=confidence
    )


def _build_market(rng: random.Random) -> MarketSnapshot:
    price = _sample_market_price(rng)
    volume = rng.uniform(VOLUME_MIN, VOLUME_MAX)
    return MarketSnapshot(
        id=f"m-{rng.randint(1, 10_000)}",
        slug="sim",
        question="Synthetic market",
        outcomes=["YES", "NO"],
        outcome_prices=[price, 1 - price],
        volume_24h=volume,
        liquidity=rng.uniform(500, 50_000),
        best_yes_price=price,
        best_no_price=1 - price,
    )


def _sample_phase(rng: random.Random) -> int:
    r = rng.random()
    cum = 0.0
    for phase, w in PHASE_WEIGHTS.items():
        cum += w
        if r <= cum:
            return phase
    return 3


def _build_mispricing(rng: random.Random, price: float) -> MispricingResult:
    abs_z = max(0.0, abs(rng.gauss(Z_MEAN, Z_STD)))
    sign = rng.choice([-1.0, 1.0])
    z = sign * abs_z
    adj_vol = max(0.0, min(1.0, rng.gauss(0.55, 0.2)))
    return MispricingResult(
        market_id="sim",
        z=z,
        mean=price,
        stddev=0.05,
        samples=200,
        adj_vol_score=adj_vol,
        current_price=price,
        current_volume_24h=None,
    )


def _build_timing(rng: random.Random) -> TimingDecision:
    phase = _sample_phase(rng)
    phase_score = {1: 20.0, 2: 16.0, 3: 6.0, 4: 0.0, 5: 0.0}[phase]
    return TimingDecision(
        phase=phase,
        score=phase_score,
        label=PHASE_LABEL[phase],
        reason="synthetic",
    )


def _sample_net_edge(rng: random.Random) -> float:
    return rng.gauss(NET_EDGE_MEAN, NET_EDGE_STD)


def _sample_fill_ratio(rng: random.Random) -> float:
    return max(0.0, min(1.0, rng.gauss(FILL_RATIO_MEAN, FILL_RATIO_STD)))


# ---------------------------------------------------------------------------
#  Path-driven outcome (new repricing exit engine)
# ---------------------------------------------------------------------------


def _simulate_path_outcome(
    *,
    entry_price: float,
    amount_usd: float,
    opened_at: datetime,
    rng: random.Random,
    hourly_sigma: float = PRICE_PATH_HOURLY_SIGMA,
) -> dict:
    """Walk the mid-price forward and drive the shared exit machine.

    Returns a dict with the realised + unrealised PnL, the close
    reason, whether the trade was a winner, max pnl % seen, and the
    list of tiers that fired.

    The simulator does **not** need a DB — it keeps an in-memory
    ``exit_state`` dict and mutates ``shares`` / ``peak_price`` /
    ``trailing_active`` exactly the way the production monitor +
    executor do.
    """
    shares = amount_usd / entry_price if entry_price > 0 else 0.0
    if shares <= 0:
        return {
            "pnl_usd": 0.0,
            "pnl_pct": 0.0,
            "won": False,
            "close_reason": CloseReason.ERROR.value,
            "tiers_hit": [],
            "max_pnl_pct_seen": 0.0,
        }

    state = empty_exit_state()
    peak_price: Optional[float] = None
    trailing_active = False

    max_hours = settings.time_exit_hours * PRICE_PATH_MAX_HOURS_MULT
    step_seconds = PRICE_PATH_STEP_SECONDS
    total_steps = int((max_hours * 3600) / step_seconds)
    step_sigma = hourly_sigma * math.sqrt(step_seconds / 3600.0)

    price = entry_price
    now = opened_at

    for step in range(1, total_steps + 1):
        now = opened_at + timedelta(seconds=step * step_seconds)
        log_return = rng.gauss(0.0, step_sigma)
        price = max(0.001, min(0.999, price * math.exp(log_return)))
        pnl_pct_value = (price - entry_price) / entry_price * 100.0

        view = TradeExitView(
            entry_price=entry_price,
            current_shares=shares,
            opened_at=opened_at,
            peak_price=peak_price,
            trailing_active=trailing_active,
            exit_state=state,
        )
        evaluation = evaluate_exit(
            view, price=price, pnl_pct_value=pnl_pct_value, now=now
        )

        state = evaluation.new_exit_state
        peak_price = evaluation.new_peak_price
        trailing_active = evaluation.new_trailing_active
        action = evaluation.action

        if action.kind is ExitActionKind.PARTIAL:
            assert action.close_shares is not None
            assert action.tier is not None
            assert action.new_trailing_pct is not None
            state = record_partial(
                state=state,
                tier=action.tier,
                close_shares=action.close_shares,
                close_price=price,
                entry_price=entry_price,
                at=now,
            )
            state["trailing_pct"] = float(action.new_trailing_pct)
            shares = max(0.0, shares - action.close_shares)
            if shares <= 0:
                return _finalize(
                    state=state,
                    close_price=price,
                    entry_price=entry_price,
                    remaining_shares=0.0,
                    original_amount_usd=amount_usd,
                    reason=CloseReason.TAKE_PROFIT,
                )
            continue

        if action.kind is ExitActionKind.CLOSE:
            assert action.close_reason is not None
            return _finalize(
                state=state,
                close_price=price,
                entry_price=entry_price,
                remaining_shares=shares,
                original_amount_usd=amount_usd,
                reason=action.close_reason,
            )

    reason = (
        CloseReason.TRAILING_STOP if trailing_active else CloseReason.TIME_EXIT
    )
    return _finalize(
        state=state,
        close_price=price,
        entry_price=entry_price,
        remaining_shares=shares,
        original_amount_usd=amount_usd,
        reason=reason,
    )


def _finalize(
    *,
    state: dict,
    close_price: float,
    entry_price: float,
    remaining_shares: float,
    original_amount_usd: float,
    reason: CloseReason,
) -> dict:
    realized = float(state.get("realized_pnl_usd", 0.0))
    unrealized = (close_price - entry_price) * remaining_shares
    total_pnl = realized + unrealized
    pnl_pct_value = (
        (total_pnl / original_amount_usd) * 100.0
        if original_amount_usd > 0
        else 0.0
    )
    return {
        "pnl_usd": round(total_pnl, 6),
        "pnl_pct": round(pnl_pct_value, 4),
        "won": total_pnl > 0,
        "close_reason": reason.value,
        "tiers_hit": [float(t) for t in state.get("tiers_hit", [])],
        "max_pnl_pct_seen": float(state.get("max_pnl_pct_seen", 0.0)),
    }


# ---------------------------------------------------------------------------
#  Core simulation
# ---------------------------------------------------------------------------


def run_simulation(
    *,
    days: int = 15,
    seed: Optional[int] = None,
    starting_balance: float = STARTING_BALANCE,
    risk_pct: float = 3.0,
    max_trades_per_day: int = 4,
    stop_loss_enabled: bool = True,
    hourly_sigma: float = PRICE_PATH_HOURLY_SIGMA,
    verbose: bool = True,
) -> SimResult:
    rng = random.Random(seed)
    strategy = PrymStrategy()
    scorer = SignalScoringSystem()

    balance = starting_balance
    result = SimResult(starting_balance=starting_balance)

    now = datetime.now(timezone.utc)

    for day_idx in range(1, days + 1):
        day = DayStats(day=day_idx)
        last_trade_time: Optional[datetime] = None

        news_today = max(1, int(rng.gauss(NEWS_PER_DAY_MEAN, 25)))
        tick_seconds = max(1, 86400 // news_today)
        sim_clock = now + timedelta(days=day_idx - 1)
        for tick_idx in range(news_today):
            sim_clock += timedelta(seconds=tick_seconds)
            day.news_seen += 1

            if rng.random() >= HARD_FILTER_PASS_RATE:
                continue
            day.news_filtered += 1

            ai = _build_ai(rng)
            if ai.impact == "neutral":
                continue

            if rng.random() >= MARKET_MATCH_RATE:
                continue

            market = _build_market(rng)
            mispricing = _build_mispricing(rng, market.best_yes_price or 0.5)
            timing = _build_timing(rng)
            net_edge_pct = _sample_net_edge(rng)
            fill_ratio = _sample_fill_ratio(rng)

            news_time = sim_clock - timedelta(seconds=rng.randint(10, 180))
            side_hint = "yes" if ai.impact == "bullish" else "no"
            entry_hint = (
                market.best_yes_price if side_hint == "yes" else market.best_no_price
            ) or 0.0
            breakdown = scorer.score(
                ai=ai,
                market=market,
                mispricing=mispricing,
                timing=timing,
                news_published_at=news_time,
                side=side_hint,
                net_edge_pct=net_edge_pct,
                fill_ratio=fill_ratio,
                entry_price=entry_hint,
            )
            day.signals_scored += 1

            decision = strategy.evaluate(ai=ai, market=market, score=breakdown)
            if not decision.should_enter:
                continue

            if day.trades_opened >= max_trades_per_day:
                continue
            if last_trade_time is not None:
                cooldown = getattr(settings, "trade_cooldown_seconds", 900)
                if (sim_clock - last_trade_time).total_seconds() < cooldown:
                    continue

            side = decision.side or "yes"
            price = market.best_yes_price if side == "yes" else market.best_no_price
            if price is None:
                continue
            plan = strategy.sizing(
                balance=balance,
                risk_pct=risk_pct,
                entry_price=price,
                high_confidence=breakdown.high_confidence,
                stop_loss_enabled=stop_loss_enabled,
                net_edge_pct=net_edge_pct,
                abs_z=abs(mispricing.z or 0.0),
            )
            if plan.amount_usd <= 0 or plan.amount_usd > balance:
                continue

            outcome = _simulate_path_outcome(
                entry_price=plan.entry_price,
                amount_usd=plan.amount_usd,
                opened_at=sim_clock,
                rng=rng,
                hourly_sigma=hourly_sigma,
            )
            pnl = outcome["pnl_usd"]
            won = outcome["won"]

            balance += pnl
            day.trades_opened += 1
            if won:
                day.trades_won += 1
            else:
                day.trades_lost += 1
            day.pnl_usd += pnl
            last_trade_time = sim_clock

            result.trades.append(
                TradeResult(
                    day=day_idx,
                    side=side,
                    entry_price=plan.entry_price,
                    amount_usd=plan.amount_usd,
                    stop_loss=plan.stop_loss,
                    take_profit=plan.take_profit,
                    won=won,
                    pnl_usd=pnl,
                    high_confidence=plan.high_confidence,
                    band=str(getattr(plan, "band", "mid")),
                    tiers_hit=outcome["tiers_hit"],
                    close_reason=outcome["close_reason"],
                    max_pnl_pct_seen=outcome["max_pnl_pct_seen"],
                    close_pnl_pct=outcome["pnl_pct"],
                )
            )

        day.balance_eod = balance
        result.days.append(day)

        if verbose:
            pnl_sign = "+" if day.pnl_usd >= 0 else ""
            print(
                f"Day {day_idx:2d} | news {day.news_seen:4d} -> filt "
                f"{day.news_filtered:3d} -> sig {day.signals_scored:3d} | "
                f"trades {day.trades_opened} "
                f"(W{day.trades_won}/L{day.trades_lost}) | "
                f"PnL {pnl_sign}{day.pnl_usd:6.2f}$ | bal {balance:7.2f}$"
            )

    result.ending_balance = balance
    return result


# ---------------------------------------------------------------------------
#  Monte-Carlo wrapper
# ---------------------------------------------------------------------------

def monte_carlo(n_runs: int, **kwargs) -> None:
    kwargs.setdefault("verbose", False)
    pnls = []
    winrates = []
    trade_counts = []
    runner_shares_pct = []
    for i in range(n_runs):
        r = run_simulation(seed=i, **kwargs)
        pnls.append(r.pnl_pct)
        winrates.append(r.win_rate)
        trade_counts.append(len(r.trades))
        if r.trades:
            runners = sum(1 for t in r.trades if t.max_pnl_pct_seen >= 200)
            runner_shares_pct.append(100.0 * runners / len(r.trades))
        else:
            runner_shares_pct.append(0.0)

    pnls.sort()

    def pct(p: float) -> float:
        if not pnls:
            return 0.0
        idx = max(0, min(len(pnls) - 1, int(round(p * (len(pnls) - 1)))))
        return pnls[idx]

    print(f"\n--- Monte Carlo over {n_runs} runs ({kwargs.get('days', 15)} days each) ---")
    print(f"avg trades / run    : {statistics.mean(trade_counts):6.1f}")
    print(f"avg win-rate        : {statistics.mean(winrates):6.2f}%")
    print(f"avg PnL             : {statistics.mean(pnls):+6.2f}%")
    print(f"median PnL          : {statistics.median(pnls):+6.2f}%")
    if len(pnls) > 1:
        print(f"stdev PnL           : {statistics.stdev(pnls):6.2f}%")
    print(f"p10 / p50 / p90     : {pct(0.10):+6.2f}% / {pct(0.50):+6.2f}% / {pct(0.90):+6.2f}%")
    print(f"best / worst        : {max(pnls):+6.2f}% / {min(pnls):+6.2f}%")
    winning_runs = sum(1 for p in pnls if p > 0)
    print(f"profitable runs     : {winning_runs}/{n_runs} ({100 * winning_runs / n_runs:.1f}%)")
    print(f"avg runners (>=200%): {statistics.mean(runner_shares_pct):6.2f}% of trades")


# ---------------------------------------------------------------------------
#  Reporting helpers
# ---------------------------------------------------------------------------

def _print_final(r: SimResult) -> None:
    print("\n--- FINAL -----------------------------------------------------------")
    print(f"Total trades        : {len(r.trades)}")
    wins = sum(1 for t in r.trades if t.won)
    print(
        f"Win-rate            : {r.win_rate:6.2f}%  "
        f"({wins}W / {len(r.trades) - wins}L)"
    )
    print(f"Total PnL           : {r.pnl_usd:+7.2f}$   ({r.pnl_pct:+.2f}%)")
    print(f"Max drawdown        : {r.max_drawdown_pct:.2f}%")
    print(f"Final balance       : ${r.ending_balance:,.2f}")

    if not r.trades:
        return

    tier_hits: dict[float, int] = {}
    for t in r.trades:
        for tier in t.tiers_hit:
            tier_hits[tier] = tier_hits.get(tier, 0) + 1
    if tier_hits:
        print("\nPartial-TP ladder hits:")
        for tier in sorted(tier_hits):
            share = 100.0 * tier_hits[tier] / len(r.trades)
            print(
                f"  tier +{tier:>5.1f}%       : {tier_hits[tier]:4d} "
                f"({share:5.1f}% of trades)"
            )

    reasons: dict[str, int] = {}
    for t in r.trades:
        reasons[t.close_reason] = reasons.get(t.close_reason, 0) + 1
    print("\nClose reasons:")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        share = 100.0 * count / len(r.trades)
        print(f"  {reason:<15s}   : {count:4d} ({share:5.1f}%)")

    max_seen = [t.max_pnl_pct_seen for t in r.trades]
    runners = [x for x in max_seen if x >= 200.0]
    p90_idx = int(0.9 * (len(max_seen) - 1))
    p90 = sorted(max_seen)[p90_idx]
    print(
        f"\nMax PnL% seen       : "
        f"avg {statistics.mean(max_seen):+6.1f}%, "
        f"p90 {p90:+6.1f}%, "
        f"max {max(max_seen):+6.1f}%"
    )
    print(
        f"Runners (>= +200%)  : {len(runners)} "
        f"({100.0 * len(runners) / len(r.trades):.1f}% of trades)"
    )

    hc = [t for t in r.trades if t.high_confidence]
    if hc:
        hc_pnl = sum(t.pnl_usd for t in hc)
        hc_wins = sum(1 for t in hc if t.won)
        print(
            f"High-conf trades    : {len(hc)} "
            f"({hc_wins}W / {len(hc) - hc_wins}L, PnL {hc_pnl:+.2f}$)"
        )

    band_order = ["low_prob", "low", "mid", "high"]
    band_counts: dict[str, int] = {b: 0 for b in band_order}
    band_pnl: dict[str, float] = {b: 0.0 for b in band_order}
    band_wins: dict[str, int] = {b: 0 for b in band_order}
    for t in r.trades:
        band = t.band if t.band in band_counts else "mid"
        band_counts[band] += 1
        band_pnl[band] += t.pnl_usd
        if t.won:
            band_wins[band] += 1
    if any(band_counts.values()):
        print("\nBand breakdown:")
        for b in band_order:
            c = band_counts[b]
            if c == 0:
                continue
            wr = 100.0 * band_wins[b] / c
            print(
                f"  {b:<10s}       : {c:4d} trades, "
                f"WR {wr:5.1f}%, PnL {band_pnl[b]:+7.2f}$"
            )


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="15-day Prym Signals simulation")
    parser.add_argument("--days", type=int, default=15)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--balance", type=float, default=STARTING_BALANCE)
    parser.add_argument("--risk", type=float, default=3.0)
    parser.add_argument("--max-per-day", type=int, default=4)
    parser.add_argument(
        "--no-stop-loss",
        action="store_true",
        help="Legacy flag (hard-SL in the exit state machine is still active)",
    )
    parser.add_argument(
        "--hourly-sigma",
        type=float,
        default=PRICE_PATH_HOURLY_SIGMA,
        help="Hourly log-return volatility for the synthetic price path "
        "(default 0.04 = 4%%).",
    )
    parser.add_argument(
        "--mc",
        type=int,
        default=0,
        help="Monte-Carlo: run N independent simulations and report percentiles",
    )
    args = parser.parse_args()

    if args.mc > 0:
        monte_carlo(
            args.mc,
            days=args.days,
            starting_balance=args.balance,
            risk_pct=args.risk,
            max_trades_per_day=args.max_per_day,
            stop_loss_enabled=not args.no_stop_loss,
            hourly_sigma=args.hourly_sigma,
        )
        return

    bar = "=" * 69
    print(bar)
    print(f"  PRYM SIGNALS -- {args.days}-DAY SIMULATION")
    print(
        f"  Starting balance: ${args.balance:,.2f}  |  Risk: {args.risk}%  |  "
        f"Max/day: {args.max_per_day}"
    )
    seed_str = str(args.seed) if args.seed is not None else "random"
    tiers = settings.partial_tp_tiers
    tier_str = ", ".join(
        f"+{t.pnl_threshold_pct:.0f}%:{t.close_fraction_pct:.0f}%:{t.new_trailing_pct:.0f}%tr"
        for t in tiers
    )
    print(
        f"  Seed: {seed_str}  |  hard SL: -{settings.hard_sl_pct:.0f}%  |  "
        f"time exit: {settings.time_exit_hours:.0f}h"
    )
    print(f"  Partial ladder: {tier_str}")
    print(f"  Hourly sigma: {args.hourly_sigma * 100:.2f}%")
    print(bar + "\n")

    r = run_simulation(
        days=args.days,
        seed=args.seed,
        starting_balance=args.balance,
        risk_pct=args.risk,
        max_trades_per_day=args.max_per_day,
        stop_loss_enabled=not args.no_stop_loss,
        hourly_sigma=args.hourly_sigma,
    )

    _print_final(r)


if __name__ == "__main__":
    main()
