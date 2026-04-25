"""/start — onboarding + risk disclaimer."""
from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.database.models import User
from app.telegram.auth import requires_auth
from app.telegram.formatters import START_MESSAGE


@requires_auth
async def start_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: User
) -> None:
    assert update.effective_message is not None
    await update.effective_message.reply_text(
        START_MESSAGE,
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )
