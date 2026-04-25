"""/trades — list open trades for the user."""
from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.database.models import User
from app.database.repositories.trades_repo import TradesRepository
from app.database.session import session_scope
from app.telegram.auth import requires_auth
from app.telegram.formatters import trades_list


@requires_auth
async def trades_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: User
) -> None:
    assert update.effective_message is not None
    async with session_scope() as session:
        open_trades = await TradesRepository(session).list_open(user.id)
    await update.effective_message.reply_text(
        trades_list(open_trades), parse_mode=ParseMode.MARKDOWN_V2
    )
