"""/signals — recent signals, regardless of user (signals are global)."""
from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.database.models import User
from app.database.repositories.signals_repo import SignalsRepository
from app.database.session import session_scope
from app.telegram.auth import requires_auth
from app.telegram.formatters import signals_list


@requires_auth
async def signals_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: User
) -> None:
    assert update.effective_message is not None
    async with session_scope() as session:
        recent = await SignalsRepository(session).recent(limit=10)
    await update.effective_message.reply_text(
        signals_list(recent), parse_mode=ParseMode.MARKDOWN_V2
    )
