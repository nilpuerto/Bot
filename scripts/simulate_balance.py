"""Prym forward simulation — *"what happens if I start with N euros?"*

This is **not** the backtester.  The backtester replays a real tape
of recorded news → market → book → outcome events.  This script, in
contrast, builds a synthetic but *realistic* stream of prediction-
market opportunities, runs every one through the **production**
pipeline (same hard gates, same scorer, same sizing, same cost model),
and tracks the resulting account balance trade by trade.

The value is not a forecast of PnL — real edge comes from real news
and real mispricings, not from Monte Carlo rolls.  The value is:

* **sanity-checking** the edge-first gates produce the expected
  pass/reject rates;
* **stress-testing** the sizing engine against the configured
  ``BAND_*_PCT`` and ``MAX_TRADE_USD`` rails;
* **visualising** how a balance like 200 € evolves under plausible
  monthly throughput and a reasonable win-rate / payoff mix.

The outcome model intentionally biases toward **prediction-market
reality** on Polymarket, not forex:

* Trades that clear every hard gate still lose ~40 % of the time —
  the edge is statistical, not deterministic.
* Winners are dominated by the fixed take-profit (``+TP_PCT``) because
  the trailing stop (``+TRAILING_ACTIVATION_PCT`` arm, ``-TRAILING_PCT``
  drawdown) only fires when the trade keeps running past TP.
* Losers can lose the full stake when the market resolves against us
  — there is no fixed SL under the edge-first refactor.

Usage::

    python -m scripts.simulate_balance --balance 200 --days 30 --seed 42
"""
from __future__ import annotations

import argparse
import random
import statistics
from dataclasses import dataclass
from typing import Optional

from app.config.settings import settings
from app.integrations.mistral_client import AIAnalysis
from app.integrations.polymarket_client import (
    MarketSnapshot,
    OrderBook,
    OrderBookLevel,
)
from app.services.execution_cost import ExecutionCostModel
from app.services.microstructure import MicrostructureService
from app.services.mispricing import MispricingResult
from app.services.signal_scoring import SignalScoringSystem
from app.services.sizing import compute_sizing
from app.services.timing import TimingDecision


# ---------------------------------------------------------------------------
# Synthetic generators
# ---------------------------------------------------------------------------


@dataclass
class GateLog:
    reason: str
    z: float
    net_edge: float
    phase: int
    impact: str
    fill_ratio: float


def _rand_book(entry: float, depth_usd: float, spread_bps: int) -> OrderBook:
    """Build a two-sided book centred on ``entry`` with the given depth."""
    ask = min(0.99, entry * (1 + spread_bps / 10_000))
    bid = max(0.01, entry * (1 - spread_bps / 10_000))
    levels_ask = [
        OrderBookLevel(price=round(ask + 0.01 * i, 4), size=depth_usd / (ask + 0.01 * i))
        for i in range(5)
    ]
    levels_bid = [
        OrderBookLevel(price=round(max(0.01, bid - 0.01 * i), 4),
                       size=depth_usd / max(0.01, bid - 0.01 * i))
        for i in range(5)
    ]
    return OrderBook(token_id="sim", bids=levels_bid, asks=levels_ask)


def _rand_scenario(rng: random.Random) -> dict:
    """Return one synthetic scenario representative of the RSS tape.

    The distributions approximate what a live 5-minute window looks
    like: the vast majority of news are neutral or off-phase; a few
    percent clear every hard gate.
    """
    impact = rng.choices(
        ["bullish", "bearish", "neutral"], weights=[0.12, 0.10, 0.78]
    )[0]
    phase = rng.choices([1, 2, 3, 4, 5], weights=[0.04, 0.12, 0.40, 0.30, 0.14])[0]
    z = rng.gauss(0.0, 1.2)  # mostly inside ±2σ; tails hit the Z gate
    news_age_s = rng.uniform(10, 900)  # 10 s .. 15 min
    entry = rng.uniform(0.05, 0.32)
    depth_usd = rng.choice([50, 150, 400, 1_000, 3_000])
    spread_bps = rng.choice([20, 50, 100, 200])
    samples = rng.randint(20, 60)
    return dict(
        impact=impact,
        phase=phase,
        z=z,
        news_age_s=news_age_s,
        entry=entry,
        depth_usd=depth_usd,
        spread_bps=spread_bps,
        samples=samples,
    )


def _score_one(rng: random.Random, scenario: dict):
    """Run the scenario through the real pipeline. Returns (score, cost, side, entry, book) or None."""
    book = _rand_book(scenario["entry"], scenario["depth_usd"], scenario["spread_bps"])

    micro_svc = MicrostructureService(polymarket=None)  # type: ignore[arg-type]
    micro = micro_svc.from_book(book)

    mp = MispricingResult(
        market_id="sim",
        z=scenario["z"],
        mean=scenario["entry"],
        stddev=0.05,
        samples=scenario["samples"],
        adj_vol_score=0.5,
        current_price=scenario["entry"],
        current_volume_24h=scenario["depth_usd"] * 4,
    )
    timing = TimingDecision(
        phase=scenario["phase"],
        score=15.0 if scenario["phase"] == 1 else (12.0 if scenario["phase"] == 2 else 0.0),
        label="sim",
        reason="sim",
    )

    side = "yes" if scenario["impact"] == "bullish" else "no"
    entry = scenario["entry"]
    tp_price = min(0.99, entry * (1 + settings.take_profit_pct / 100))

    # Probe the cost model with the max size to get an honest edge estimate.
    probe = settings.max_trade_usd
    cost = ExecutionCostModel().evaluate(
        book=book,
        size_usd=probe,
        side=side,
        target_price=tp_price,
    )

    ai = AIAnalysis(
        market="sim",
        category="other",
        impact=scenario["impact"],  # type: ignore[arg-type]
        urgency=5,
        entities=[],
    )
    scorer = SignalScoringSystem()
    # news_published_at as "now - age_s" so the freshness gate reads correctly.
    from datetime import datetime, timedelta, timezone
    published = datetime.now(timezone.utc) - timedelta(seconds=scenario["news_age_s"])

    score = scorer.score(
        ai=ai,
        market=MarketSnapshot(
            id="sim", slug="sim", question="sim",
            outcomes=["Yes", "No"], outcome_prices=[entry, 1 - entry],
            volume_24h=scenario["depth_usd"] * 4,
            liquidity=scenario["depth_usd"],
            best_yes_price=entry, best_no_price=1 - entry,
            end_date=None, yes_token_id=None, no_token_id=None,
        ),
        micro=micro,
        mispricing=mp,
        timing=timing,
        news_published_at=published,
        side=side,
        net_edge_pct=cost.net_edge_pct if cost else None,
        fill_ratio=cost.fill_ratio if cost else None,
    )
    return score, cost, side, entry, scenario


def _sample_outcome(rng: random.Random, entry: float) -> float:
    """Return PnL % for one trade drawn from a realistic distribution.

    Breakdown (empirical priors for Polymarket event-driven edge):

    * 55 % winners.  Of those:
        – 70 % take profit at ``+TAKE_PROFIT_PCT`` (fixed TP).
        – 30 % keep running, arm the trailing stop, exit between
          +20 % and +50 % depending on how far past +TP they ran.
    * 45 % losers.  Of those:
        – 55 % take an opportunistic retrace that pulls out at
          −5 % … −20 % (trade manager / manual exit).
        – 45 % ride to resolution and lose the full entry value
          (``-100 %``) — no fixed stop-loss under edge-first.
    """
    if rng.random() < 0.55:
        if rng.random() < 0.70:
            return settings.take_profit_pct
        return rng.uniform(20.0, 50.0)
    if rng.random() < 0.55:
        return -rng.uniform(5.0, 20.0)
    return -100.0


def run_simulation(*, balance: float, days: int, seed: Optional[int]) -> dict:
    rng = random.Random(seed)
    # Synthetic throughput: RSS drops ~100 fresh items per 5-min window,
    # of which a few % are "news-worthy".  Use ~40 headline candidates
    # per simulated day.  Each hits the scorer; only the edge-first
    # conjunction yields a real trade.
    scenarios_per_day = 40

    trades: list[dict] = []
    rejects_by_gate: dict[str, int] = {}
    equity_curve: list[float] = [balance]
    running_balance = balance
    daily_trades = 0
    last_day = 0

    total_scored = days * scenarios_per_day
    for step in range(total_scored):
        day = step // scenarios_per_day
        if day != last_day:
            daily_trades = 0
            last_day = day
        if daily_trades >= settings.max_trades_per_day:
            continue

        scenario = _rand_scenario(rng)
        score, cost, side, entry, sc = _score_one(rng, scenario)

        if not score.passes_trade:
            raw = score.gate_reason or "unknown"
            # Collapse noisy per-value reasons into a family bucket so the
            # histogram stays readable ("stale_news_age_312.5" → "stale_news").
            if raw.startswith("stale_news"):
                reason = "stale_news"
            elif raw.startswith("z_below_min"):
                reason = "z_below_min"
            elif raw.startswith("net_edge_below_min") or raw.startswith("no_cost_model"):
                reason = "net_edge_below_min"
            elif raw.startswith("fill_below_min"):
                reason = "fill_below_min"
            elif raw.startswith("phase_"):
                reason = "phase_late"
            else:
                reason = raw
            rejects_by_gate[reason] = rejects_by_gate.get(reason, 0) + 1
            continue

        # Real sizing path — uses current running balance.
        abs_z = abs(sc["z"])
        sz = compute_sizing(
            score=score.total,
            balance=running_balance,
            risk_pct=settings.default_risk_pct,
            net_edge_pct=cost.net_edge_pct if cost else None,
            abs_z=abs_z,
        )
        stake = sz.amount_usd
        if stake < settings.min_trade_usd:
            rejects_by_gate["below_min_stake"] = (
                rejects_by_gate.get("below_min_stake", 0) + 1
            )
            continue

        pnl_pct = _sample_outcome(rng, entry)
        pnl_usd = stake * pnl_pct / 100.0
        running_balance += pnl_usd
        daily_trades += 1
        trades.append(
            dict(
                day=day,
                band=sz.band,
                score=round(score.total, 1),
                z=round(abs_z, 2),
                net_edge=round(cost.net_edge_pct if cost else 0.0, 2),
                stake=round(stake, 2),
                pnl_pct=round(pnl_pct, 2),
                pnl_usd=round(pnl_usd, 2),
                balance=round(running_balance, 2),
            )
        )
        equity_curve.append(running_balance)

    # Metrics
    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] <= 0]
    total_pnl = running_balance - balance
    win_rate = (len(wins) / len(trades) * 100.0) if trades else 0.0

    profit = sum(t["pnl_usd"] for t in wins) or 0.0
    loss = abs(sum(t["pnl_usd"] for t in losses)) or 1e-9
    pf = profit / loss

    # Max drawdown on equity curve
    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)

    return dict(
        starting_balance=balance,
        final_balance=round(running_balance, 2),
        total_pnl=round(total_pnl, 2),
        roi_pct=round(total_pnl / balance * 100.0, 2),
        days=days,
        scenarios_scored=total_scored,
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate_pct=round(win_rate, 2),
        profit_factor=round(pf, 3),
        max_drawdown_usd=round(max_dd, 2),
        band_histogram={
            b: sum(1 for t in trades if t["band"] == b)
            for b in ("low", "mid", "high")
        },
        rejects_by_gate=dict(
            sorted(rejects_by_gate.items(), key=lambda x: -x[1])
        ),
        sample_trades=trades[:10],
        worst_trade=min(trades, key=lambda t: t["pnl_usd"], default=None),
        best_trade=max(trades, key=lambda t: t["pnl_usd"], default=None),
    )


def _main() -> int:
    parser = argparse.ArgumentParser(description="Prym forward balance simulation")
    parser.add_argument("--balance", type=float, default=200.0,
                        help="Starting balance in €/USDC (default 200)")
    parser.add_argument("--days", type=int, default=30,
                        help="Simulated days (default 30)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for reproducibility")
    parser.add_argument("--runs", type=int, default=1,
                        help="How many independent runs (Monte Carlo) to average")
    args = parser.parse_args()

    if args.runs == 1:
        r = run_simulation(balance=args.balance, days=args.days, seed=args.seed)
        _print_report(r)
    else:
        results = []
        for i in range(args.runs):
            seed = (args.seed or 0) + i if args.seed is not None else None
            results.append(
                run_simulation(balance=args.balance, days=args.days, seed=seed)
            )
        _print_monte_carlo(results, starting=args.balance, days=args.days)
    return 0


def _print_report(r: dict) -> None:
    print("== PRYM BALANCE SIMULATION ==")
    print("-" * 50)
    keys = [
        "starting_balance", "final_balance", "total_pnl", "roi_pct",
        "days", "scenarios_scored", "trades", "wins", "losses",
        "win_rate_pct", "profit_factor", "max_drawdown_usd",
    ]
    for k in keys:
        print(f"  {k:<22s} {r[k]}")
    print(f"  band_histogram         {r['band_histogram']}")
    print("\n  gates that rejected signals:")
    for reason, n in r["rejects_by_gate"].items():
        print(f"    {reason:<32s} {n}")
    if r["best_trade"]:
        print(f"\n  best trade  +{r['best_trade']['pnl_usd']:.2f} EUR "
              f"(day {r['best_trade']['day']}, band {r['best_trade']['band']})")
    if r["worst_trade"]:
        print(f"  worst trade {r['worst_trade']['pnl_usd']:.2f} EUR "
              f"(day {r['worst_trade']['day']}, band {r['worst_trade']['band']})")
    print("\n  first trades:")
    for t in r["sample_trades"]:
        print(
            f"    d{t['day']:02d}  band={t['band']:<4s}  z={t['z']:<5.2f}  "
            f"edge={t['net_edge']:<5.2f}%  stake={t['stake']:>5.2f}EUR  "
            f"pnl={t['pnl_pct']:>7.2f}%  bal={t['balance']:>7.2f}EUR"
        )


def _print_monte_carlo(results: list[dict], starting: float, days: int) -> None:
    finals = [r["final_balance"] for r in results]
    trades = [r["trades"] for r in results]
    roi = [r["roi_pct"] for r in results]
    dd = [r["max_drawdown_usd"] for r in results]
    wr = [r["win_rate_pct"] for r in results if r["trades"] > 0]

    def q(xs: list[float], p: float) -> float:
        if not xs:
            return 0.0
        xs_sorted = sorted(xs)
        k = int(round((len(xs_sorted) - 1) * p))
        return xs_sorted[k]

    print(f"== MONTE CARLO  -  {len(results)} runs  -  "
          f"{starting} EUR / {days} days each ==")
    print("-" * 50)
    print(f"  final balance (median)   {statistics.median(finals):.2f} EUR")
    print(f"  final balance (mean)     {statistics.fmean(finals):.2f} EUR")
    print(f"  final balance p10 / p90  {q(finals, 0.10):.2f} / {q(finals, 0.90):.2f} EUR")
    print(f"  final balance min / max  {min(finals):.2f} / {max(finals):.2f} EUR")
    print(f"  ROI median               {statistics.median(roi):.2f} %")
    print(f"  trades/run median        {statistics.median(trades):.0f}")
    print(f"  win rate median          "
          f"{statistics.median(wr) if wr else 0:.2f} %")
    print(f"  max drawdown median      {statistics.median(dd):.2f} EUR")
    bust = sum(1 for f in finals if f < starting * 0.5)
    double = sum(1 for f in finals if f > starting * 2)
    print(f"  runs that lost >50%      {bust}/{len(results)}")
    print(f"  runs that doubled        {double}/{len(results)}")


if __name__ == "__main__":
    raise SystemExit(_main())
