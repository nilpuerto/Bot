"""Smoke-test Markdown V2 escaping and card rendering."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.database.models import (
    Signal,
    SignalImpact,
    SignalStatus,
    Trade,
    TradeSide,
    TradeStatus,
    UserMode,
)
from app.services.portfolio import PortfolioSnapshot
from app.telegram.formatters import (
    escape_md,
    mode_changed,
    portfolio_card,
    signal_card,
    signals_list,
    trades_list,
)


def test_escape_md_escapes_special_chars() -> None:
    assert escape_md("a.b") == "a\\.b"
    assert escape_md("(hello)") == "\\(hello\\)"
    assert escape_md(None) == ""


def test_signal_card_contains_key_pieces() -> None:
    signal = Signal(
        id=1,
        news_title="Breaking: election upset",
        news_url="https://example.com",
        news_hash="x" * 40,
        market_id="m1",
        market_question="Will party X win?",
        market_price=Decimal("0.24"),
        impact=SignalImpact.BULLISH,
        urgency=9,
        score=Decimal("87"),
        status=SignalStatus.NEW,
    )
    card = signal_card(
        signal=signal,
        score=87.0,
        trader_aligned=3,
        trader_conviction_usd=42_000,
    )
    assert "PRYM SIGNAL" in card
    assert "0\\.24" in card or "0.24" in card  # depending on escaping


def test_portfolio_card_renders() -> None:
    snap = PortfolioSnapshot(
        configured_cap=Decimal("1000.00"),
        usdc_available=Decimal("60.00"),
        effective_balance=Decimal("60.00"),
        in_bot_positions_usd=Decimal("340.00"),
        holdings_mark_usd=Decimal("120.50"),
        estimated_portfolio_usd=Decimal("180.50"),
        balance_status="ok",
        total_pnl=Decimal("25.50"),
        winrate_pct=66.6,
        open_trades=2,
        trades_today=1,
        mode="semi",
    )
    text = portfolio_card(snap)
    assert "PORTFOLIO" in text
    assert "SEMI" in text
    # effective balance must surface clearly.
    assert "Will deploy" in text
    assert "60" in text


def test_portfolio_card_no_cap_shows_auto() -> None:
    """When the user has no manual cap, /info must state that the bot
    auto-sizes against the full liquid USDC."""
    snap = PortfolioSnapshot(
        configured_cap=Decimal("0"),
        usdc_available=Decimal("60.00"),
        effective_balance=Decimal("60.00"),
        in_bot_positions_usd=Decimal("0"),
        holdings_mark_usd=Decimal("0"),
        estimated_portfolio_usd=Decimal("60.00"),
        balance_status="ok",
        total_pnl=Decimal("0"),
        winrate_pct=0.0,
        open_trades=0,
        trades_today=0,
        mode="auto",
    )
    text = portfolio_card(snap)
    assert "none" in text  # "Your cap   none (uses full liquid USDC)"
    assert "AUTO" in text


def test_portfolio_card_live_unavailable_balance_message() -> None:
    snap = PortfolioSnapshot(
        configured_cap=Decimal("0"),
        usdc_available=Decimal("0"),
        effective_balance=Decimal("0"),
        in_bot_positions_usd=Decimal("0"),
        holdings_mark_usd=Decimal("0"),
        estimated_portfolio_usd=Decimal("0"),
        balance_status="unavailable",
        total_pnl=Decimal("0"),
        winrate_pct=0.0,
        open_trades=0,
        trades_today=0,
        mode="auto",
    )
    text = portfolio_card(snap)
    assert "live mode but balance unavailable" in text


def test_portfolio_card_simulation_balance_message() -> None:
    snap = PortfolioSnapshot(
        configured_cap=Decimal("100"),
        usdc_available=Decimal("0"),
        effective_balance=Decimal("100"),
        in_bot_positions_usd=Decimal("0"),
        holdings_mark_usd=Decimal("0"),
        estimated_portfolio_usd=Decimal("0"),
        balance_status="simulation",
        total_pnl=Decimal("0"),
        winrate_pct=0.0,
        open_trades=0,
        trades_today=0,
        mode="safe",
    )
    text = portfolio_card(snap)
    assert "simulation mode" in text


def test_trades_list_empty_and_filled() -> None:
    assert "No open trades" in trades_list([])
    t = Trade(
        id=10,
        user_id=1,
        market_id="m",
        market_question="Will X happen?",
        side=TradeSide.YES,
        entry_price=Decimal("0.2"),
        current_price=Decimal("0.22"),
        amount_usd=Decimal("30"),
        shares=Decimal("150"),
        status=TradeStatus.OPEN,
        pnl=Decimal("3.0"),
        is_simulated=True,
        opened_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    rendered = trades_list([t])
    assert "OPEN TRADES" in rendered
    assert "#10" in rendered
    assert "SIM" in rendered


def test_signals_list_empty() -> None:
    assert "No recent signals" in signals_list([])


def test_mode_changed_contains_value() -> None:
    assert "SAFE" in mode_changed(UserMode.SAFE)
