"""Backtester — replay a JSONL "tape" through the scoring pipeline in
dry mode and report performance metrics.

Tape format (one JSON object per line)::

    {
      "news": {"title": "...", "source": "...", "published_at": "ISO",
                "summary": "..."},
      "ai":   {"category": "...", "impact": "bullish"|"bearish"|"neutral",
                "urgency": 0..10, "confidence": 0..100, "magnitude": 0..10,
                "rarity": 0..10, "entities": [...], "second_order": [...],
                "causal_chain": "..."},
      "market": {"id": "...", "question": "...", "volume_24h": 12345,
                 "best_yes_price": 0.12, "best_no_price": 0.88,
                 "outcomes": ["Yes","No"]},
      "book":   {"bids": [{"price":0.12,"size":200},...],
                 "asks": [{"price":0.13,"size":300},...]},
      "mispricing": {"z": -2.3, "adj_vol_score": 0.8, "samples": 50},
      "timing_features": {"news_age_s": 40, "dvol_1m": 0, ...},
      "outcome": {"exit_price": 0.18, "hit": "take_profit"}
    }

The backtester is intentionally self-contained — it never touches the
network or the database.  Output: ``{trades, wins, losses, win_rate,
expectancy, profit_factor, sharpe, max_drawdown}``.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

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
from app.services.timing import TimingFeatures, detect_phase


@dataclass
class BacktestTrade:
    score: float
    pnl_pct: float
    pnl_usd: float
    size_usd: float
    passed_strategy: bool
    reason: str


def _load_tape(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as fh:
        for idx, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"skip line {idx}: {exc}", file=sys.stderr)


def _build_market(raw: dict) -> MarketSnapshot:
    return MarketSnapshot(
        id=str(raw.get("id") or ""),
        slug=raw.get("slug"),
        question=raw.get("question") or "",
        outcomes=list(raw.get("outcomes") or []),
        outcome_prices=list(raw.get("outcome_prices") or []),
        volume_24h=float(raw.get("volume_24h") or 0),
        liquidity=float(raw.get("liquidity") or 0),
        best_yes_price=raw.get("best_yes_price"),
        best_no_price=raw.get("best_no_price"),
        end_date=raw.get("end_date"),
        yes_token_id=raw.get("yes_token_id"),
        no_token_id=raw.get("no_token_id"),
    )


def _build_book(raw: Optional[dict]) -> Optional[OrderBook]:
    if not raw:
        return None
    bids = [
        OrderBookLevel(price=float(lv["price"]), size=float(lv["size"]))
        for lv in raw.get("bids", [])
    ]
    asks = [
        OrderBookLevel(price=float(lv["price"]), size=float(lv["size"]))
        for lv in raw.get("asks", [])
    ]
    return OrderBook(token_id=raw.get("token_id", ""), bids=bids, asks=asks)


def _simulate_trade(raw: dict) -> Optional[BacktestTrade]:
    ai_raw = raw.get("ai") or {}
    ai = AIAnalysis.model_validate(
        {
            "market": raw.get("market", {}).get("question"),
            **ai_raw,
        }
    )
    market = _build_market(raw.get("market", {}))
    book = _build_book(raw.get("book"))

    micro = None
    if book is not None:
        micro_svc = MicrostructureService(polymarket=None)  # type: ignore[arg-type]
        micro = micro_svc.from_book(book)

    mp_raw = raw.get("mispricing") or {}
    mp = MispricingResult(
        market_id=market.id,
        z=mp_raw.get("z"),
        mean=mp_raw.get("mean"),
        stddev=mp_raw.get("stddev"),
        samples=int(mp_raw.get("samples", 0)),
        adj_vol_score=float(mp_raw.get("adj_vol_score", 0.0)),
        current_price=market.best_yes_price,
        current_volume_24h=market.volume_24h,
    )

    tf_raw = raw.get("timing_features") or {}
    timing = detect_phase(
        TimingFeatures(
            news_age_s=tf_raw.get("news_age_s"),
            dvol_1m=float(tf_raw.get("dvol_1m", 0)),
            dvol_5m=float(tf_raw.get("dvol_5m", 0)),
            avg_vol_1m=float(tf_raw.get("avg_vol_1m", 1)),
            dprice_1m=float(tf_raw.get("dprice_1m", 0)),
        )
    )

    side = "yes" if ai.impact == "bullish" else "no"
    scorer = SignalScoringSystem()
    score = scorer.score(
        ai=ai,
        market=market,
        traders=None,
        dq=None,
        micro=micro,
        mispricing=mp,
        timing=timing,
        news_published_at=None,
        side=side,
    )

    # Strategy gate (simplified — we reuse the cost model but not the
    # price-range checks, so tape curators control eligibility).
    if score.total < settings.score_threshold_trade or score.phase not in (1, 2):
        return BacktestTrade(
            score=score.total,
            pnl_pct=0.0,
            pnl_usd=0.0,
            size_usd=0.0,
            passed_strategy=False,
            reason=f"score={score.total:.1f}/phase={score.phase}",
        )

    size = compute_sizing(
        score=score.total, balance=1000.0, risk_pct=settings.default_risk_pct
    ).amount_usd

    entry = market.best_yes_price if side == "yes" else market.best_no_price
    if entry is None or entry <= 0:
        return None

    # Edge gate (optional when a book is present).
    if book is not None:
        cost = ExecutionCostModel().evaluate(
            book=book,
            size_usd=size,
            side=side,
            target_price=min(0.99, entry * (1 + settings.take_profit_pct / 100)),
        )
        if not cost.passes:
            return BacktestTrade(
                score=score.total,
                pnl_pct=0.0,
                pnl_usd=0.0,
                size_usd=size,
                passed_strategy=False,
                reason=f"cost:{cost.reason}",
            )

    outcome = raw.get("outcome") or {}
    exit_price = float(outcome.get("exit_price") or entry)
    pnl_pct = (exit_price - entry) / entry * 100.0
    if side == "no":
        pnl_pct = -pnl_pct
    pnl_usd = size * (pnl_pct / 100.0)

    return BacktestTrade(
        score=score.total,
        pnl_pct=pnl_pct,
        pnl_usd=pnl_usd,
        size_usd=size,
        passed_strategy=True,
        reason=str(outcome.get("hit") or "manual"),
    )


def run_backtest(tape_path: str) -> dict[str, Any]:
    path = Path(tape_path)
    if not path.is_file():
        raise FileNotFoundError(f"Tape not found: {tape_path}")

    trades: list[BacktestTrade] = []
    for raw in _load_tape(path):
        t = _simulate_trade(raw)
        if t is not None and t.passed_strategy:
            trades.append(t)

    return _report(trades)


def _report(trades: list[BacktestTrade]) -> dict[str, Any]:
    if not trades:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "expectancy_usd": 0.0,
            "profit_factor": 0.0,
            "sharpe": 0.0,
            "max_drawdown_usd": 0.0,
        }

    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    total_profit = sum(t.pnl_usd for t in wins)
    total_loss = abs(sum(t.pnl_usd for t in losses)) or 1e-9

    win_rate = len(wins) / len(trades) * 100.0
    expectancy = statistics.fmean([t.pnl_usd for t in trades])
    profit_factor = total_profit / total_loss

    returns = [t.pnl_pct / 100.0 for t in trades]
    mean_r = statistics.fmean(returns) if returns else 0.0
    try:
        stdev_r = statistics.stdev(returns) if len(returns) > 1 else 0.0
    except statistics.StatisticsError:
        stdev_r = 0.0
    sharpe = (mean_r / stdev_r) * math.sqrt(len(returns)) if stdev_r > 1e-9 else 0.0

    # Max drawdown on running equity.
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for t in trades:
        equity += t.pnl_usd
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 2),
        "expectancy_usd": round(expectancy, 4),
        "profit_factor": round(profit_factor, 3),
        "sharpe": round(sharpe, 3),
        "max_drawdown_usd": round(max_dd, 4),
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Prym Signals backtester")
    parser.add_argument("tape", help="Path to the JSONL tape file")
    parser.add_argument(
        "--json", action="store_true", help="Emit the report as JSON (machine-readable)"
    )
    args = parser.parse_args()

    report = run_backtest(args.tape)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("◆ BACKTEST REPORT")
        for k, v in report.items():
            print(f"  {k:<20} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
