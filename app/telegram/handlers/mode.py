"""/mode — show the mode picker."""
from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.database.models import User
from app.telegram.auth import requires_auth
from app.telegram.formatters import escape_md
from app.telegram.keyboards import mode_keyboard


@requires_auth
async def mode_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: User
) -> None:
    assert update.effective_message is not None
    current = user.mode.value if hasattr(user.mode, "value") else str(user.mode)
    await update.effective_message.reply_text(
        f"Current mode: *{escape_md(current.upper())}*\n\nPick a new mode:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=mode_keyboard(),
    )
