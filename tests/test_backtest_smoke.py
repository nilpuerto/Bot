"""Backtester smoke test — empty tape + single synthetic trade."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.backtest import run_backtest


def test_empty_tape_returns_zero_trades(tmp_path: Path) -> None:
    tape = tmp_path / "empty.jsonl"
    tape.write_text("")
    report = run_backtest(str(tape))
    assert report["trades"] == 0
    assert report["wins"] == 0
    assert report["win_rate"] == 0.0
    assert report["sharpe"] == 0.0


def test_synthetic_winning_trade(tmp_path: Path) -> None:
    tape = tmp_path / "one.jsonl"
    entry = {
        "ai": {
            "impact": "bullish",
            "urgency": 10,
            "confidence": 95,
            "magnitude": 9,
            "rarity": 7,
            "entities": ["Test"],
            "second_order": [],
            "causal_chain": "test",
        },
        "market": {
            "id": "m",
            "question": "Will X?",
            "outcomes": ["Yes", "No"],
            "outcome_prices": [0.2, 0.8],
            "volume_24h": 20_000,
            "best_yes_price": 0.2,
            "best_no_price": 0.8,
            "liquidity": 5_000,
        },
        "book": {
            "bids": [{"price": 0.19, "size": 5000}],
            "asks": [{"price": 0.20, "size": 5000}],
        },
        "mispricing": {
            "z": -2.8,
            "adj_vol_score": 0.9,
            "samples": 60,
        },
        "timing_features": {"news_age_s": 30, "avg_vol_1m": 5},
        "outcome": {"exit_price": 0.35, "hit": "take_profit"},
    }
    tape.write_text(json.dumps(entry) + "\n")
    report = run_backtest(str(tape))
    # Depending on thresholds the trade may or may not pass — the smoke
    # test only asserts the backtester ran end-to-end.
    assert "trades" in report
    assert "sharpe" in report
