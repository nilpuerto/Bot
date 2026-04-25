"""/close <id> — close one of the user's open trades."""
from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.database.models import CloseReason, User
from app.database.repositories.trades_repo import TradesRepository
from app.database.session import session_scope
from app.telegram.auth import requires_auth
from app.telegram.formatters import escape_md


async def _do_close(user: User, trade_id: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    executor = context.application.bot_data.get("trade_executor")
    if executor is None:
        return "Executor not ready, try again in a moment\\."

    async with session_scope() as session:
        trade = await TradesRepository(session).get(trade_id)
    if trade is None or trade.user_id != user.id:
        return "Trade not found\\."

    result = await executor.close_trade(trade_id, reason=CloseReason.MANUAL)
    if not result.ok:
        return f"Close failed: `{escape_md(result.reason)}`"
    return f"✅ Trade `#{trade_id}` closed\\."


@requires_auth
async def close_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: User
) -> None:
    assert update.effective_message is not None
    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            "Usage: `/close <trade_id>`", parse_mode=ParseMode.MARKDOWN_V2
        )
        return
    try:
        trade_id = int(args[0])
    except ValueError:
        await update.effective_message.reply_text(
            "Invalid trade id\\.", parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    text = await _do_close(user, trade_id, context)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
