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
    try:
        # The LiveBalanceProvider is shared through ``bot_data``; it uses a
        # short TTL cache so calling /info does not spam the RPC.
        balance_provider = context.application.bot_data.get("balance_provider")
        snapshot = await PortfolioService().snapshot(
            user, balance_provider=balance_provider
        )
        text = portfolio_card(snapshot)
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as exc:  # noqa: BLE001 - keep /info usable
        # If snapshot collection or Markdown rendering breaks due
        # to edge-case runtime data, keep /info usable with plain text.
        logger.warning("info_render_failed_fallback_plain", error=str(exc))
        # Try to rebuild snapshot when available; if it also fails use
        # minimal placeholders so the command still answers.
        try:
            balance_provider = context.application.bot_data.get("balance_provider")
            snapshot = await PortfolioService().snapshot(
                user, balance_provider=balance_provider
            )
        except Exception as snap_exc:  # noqa: BLE001
            logger.error("info_snapshot_failed", error=str(snap_exc))
            await update.effective_message.reply_text(
                "No he podido cargar tu portfolio ahora mismo. Reintenta en 10-20 segundos."
            )
            return

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
