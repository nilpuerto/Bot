"""/info — portfolio snapshot for the calling user."""
from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.database.models import User
from app.services.portfolio import PortfolioService
from app.telegram.auth import requires_auth
from app.telegram.formatters import portfolio_card


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
    await update.effective_message.reply_text(
        portfolio_card(snapshot), parse_mode=ParseMode.MARKDOWN_V2
    )
