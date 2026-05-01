"""/info — portfolio snapshot for the calling user."""
from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.database.models import User
from app.services.portfolio import PortfolioService
from app.telegram.auth import requires_auth
from app.telegram.formatters import portfolio_card
from app.utils.logger import get_logger


logger = get_logger(__name__)


@requires_auth
async def info_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: User
) -> None:
    assert update.effective_message is not None
    # The LiveBalanceProvider is shared through ``bot_data``; it uses a
    # short TTL cache so calling /info does not spam the RPC.
    balance_provider = context.application.bot_data.get("balance_provider")
    snapshot = await PortfolioService().snapshot(
        user, balance_provider=balance_provider
    )
    text = portfolio_card(snapshot)
    try:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as exc:  # noqa: BLE001 - keep /info usable
        # If Markdown parsing breaks due an edge-case character, fall
        # back to plain text so users still get portfolio visibility.
        logger.warning("info_markdown_render_failed", error=str(exc))
        plain = (
            "PORTFOLIO\n\n"
            f"Liquid USDC: {float(snapshot.usdc_available):,.2f}\n"
            f"Cap: {float(snapshot.configured_cap):,.2f}\n"
            f"Will deploy: {float(snapshot.effective_balance):,.2f}\n"
            f"In positions (notional): {float(snapshot.in_bot_positions_usd):,.2f}\n"
            f"Marks (~): {float(snapshot.holdings_mark_usd):,.2f}\n"
            f"Est. total: {float(snapshot.estimated_portfolio_usd):,.2f}\n\n"
            f"Total PnL: {float(snapshot.total_pnl):,.2f}\n"
            f"Win rate: {snapshot.winrate_pct:.1f}%\n"
            f"Open trades: {snapshot.open_trades}\n"
            f"Today: {snapshot.trades_today}\n"
            f"Mode: {snapshot.mode.upper()}"
        )
        await update.effective_message.reply_text(plain)
