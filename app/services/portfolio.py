"""Portfolio metrics for the Telegram ``/info`` command.

Keeps the math out of handlers so it's easy to unit-test.

``PortfolioSnapshot`` exposes balance + position concepts:

* ``usdc_available``     — actual on-chain USDC.e liquid balance.  0 in
                           simulation mode or when no provider is wired.
* ``configured_cap``     — optional user ceiling (``user.balance``).
                           When 0 the bot auto-sizes against the full
                           liquid USDC.
* ``effective_balance``  — what the bot will actually treat as the
                           bankroll for the next sizing decision.  It is
                           ``min(usdc_available, configured_cap)`` when
                           ``configured_cap > 0``, otherwise the full
                           ``usdc_available``.
* ``holdings_mark_usd``        — Σ (shares × current mid) for bot open trades,
                               a rough USD mark (conditional tokens proxy).
* ``estimated_portfolio_usd`` — ``usdc_available + holdings_mark_usd`` quick total.

The bot only touches what appears in its own ``trades`` table; manual
positions opened outside the bot are invisible to it and will never be
altered.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.config.settings import settings
from app.database.models import Trade, User
from app.database.repositories.trades_repo import TradesRepository
from app.database.session import session_scope


@dataclass
class PortfolioSnapshot:
    # Optional user-configured ceiling for sizing (stored in DB).
    configured_cap: Decimal
    # On-chain USDC.e liquid balance (0 without a live provider).
    usdc_available: Decimal
    # What the bot will actually deploy on next trade.
    effective_balance: Decimal
    # Sum of original notionals committed on open trades.
    in_bot_positions_usd: Decimal
    # Mark-to-mid approximation: Σ(shares × current_price) using DB snapshots.
    holdings_mark_usd: Decimal
    # Cash + approximate open marks (quick /info headline).
    estimated_portfolio_usd: Decimal
    # "ok" | "simulation" | "unavailable"
    balance_status: str
    total_pnl: Decimal
    winrate_pct: float
    open_trades: int
    trades_today: int
    mode: str


class PortfolioService:
    async def snapshot(
        self,
        user: User,
        *,
        balance_provider=None,
        polymarket_client=None,
    ) -> PortfolioSnapshot:
        """Build a snapshot of the user's funds and bot state.

        ``balance_provider`` should be a :class:`LiveBalanceProvider`.
        When omitted we gracefully fall back to the user-configured
        value — used by unit tests that do not wire the live provider.
        """
        configured_cap = Decimal(user.balance or 0)
        balance_status = "ok"
        if balance_provider is not None:
            breakdown = await balance_provider.effective_balance(user)
            usdc_available = breakdown.liquid_usdc
            effective_balance = breakdown.effective
            if settings.simulation_mode:
                balance_status = "simulation"
            elif usdc_available <= 0:
                # Live mode but we could not fetch / resolve a positive
                # on-chain balance (RPC, wallet/funder config, or empty wallet).
                balance_status = "unavailable"
        else:
            usdc_available = Decimal("0")
            effective_balance = configured_cap
            balance_status = "simulation" if settings.simulation_mode else "unavailable"

        async with session_scope() as session:
            repo = TradesRepository(session)
            total_pnl = await repo.total_pnl(user.id)
            winrate = await repo.winrate(user.id)
            open_count = await repo.count_open(user.id)
            today = await repo.get_today_count(user.id)
            open_trades: list[Trade] = await repo.list_open(user.id)

        in_bot_positions_usd = sum(
            (Decimal(str(t.amount_usd)) for t in open_trades if t.amount_usd),
            Decimal("0"),
        )

        holdings_mark_usd = Decimal("0")
        for t in open_trades:
            px = float(t.current_price or t.entry_price or 0)
            sh = float(t.shares or 0)
            holdings_mark_usd += Decimal(str(round(px * sh, 4)))

        # Best available positions mark:
        # 1) Polymarket /value (whole wallet; matches UI "Portfolio")
        # 2) fallback to bot-managed open trades mark from DB only.
        external_positions_mark = Decimal("0")
        if polymarket_client is not None and not settings.simulation_mode:
            try:
                external_positions_mark = await polymarket_client.get_positions_value_usd(
                    settings.effective_funder_address
                )
            except Exception:  # noqa: BLE001
                external_positions_mark = Decimal("0")

        marks_for_total = (
            external_positions_mark
            if external_positions_mark > Decimal("0")
            else holdings_mark_usd
        )
        est = Decimal(str(usdc_available)) + marks_for_total

        return PortfolioSnapshot(
            configured_cap=configured_cap,
            usdc_available=usdc_available,
            effective_balance=effective_balance,
            in_bot_positions_usd=in_bot_positions_usd,
            holdings_mark_usd=marks_for_total,
            estimated_portfolio_usd=est,
            balance_status=balance_status,
            total_pnl=total_pnl,
            winrate_pct=winrate,
            open_trades=open_count,
            trades_today=today,
            mode=user.mode.value,
        )
