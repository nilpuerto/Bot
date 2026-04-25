"""/help — list every command available to the user.

The user-visible list is always shown; admin-only commands are appended
when the caller's Telegram ID is in ``ADMIN_TELEGRAM_IDS`` (or when no
admin list is configured — "owner of the bot" fallback).
"""
from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.config.settings import settings
from app.database.models import User
from app.telegram.auth import requires_auth


_USER_HELP = (
    "◆ *PRYM SIGNALS — COMMANDS*\n\n"
    "*Core*\n"
    "`/start`       welcome \\+ disclaimer\n"
    "`/help`        this menu\n"
    "`/info`        balance, mode, open PnL\n"
    "`/trades`      your open positions\n"
    "`/signals`     last 10 opportunities\n\n"
    "*Actions*\n"
    "`/mode`        switch SAFE \\/ SEMI \\/ AUTO\n"
    "`/settings`    risk %, SL, notifications, limits\n"
    "`/close <id>`  close a trade manually\n"
    "`/scanner`     live smart\\-money clusters\n"
    "`/cancel`      abort the current dialog\n\n"
    "*Modes explained*\n"
    "🛡 *SAFE*  \\- alerts only, no orders\n"
    "⚖ *SEMI*  \\- you approve each trade inline\n"
    "⚡ *AUTO*  \\- high\\-confidence signals auto\\-executed\n"
)

_ADMIN_HELP = (
    "\n*Admin*\n"
    "`/weights`          show learned pillar weights\n"
    "`/backtest <file>`  replay a tape through the pipeline\n"
    "`/secondary on|off` toggle the low\\-attention scout\n"
)


def _is_admin(user: User) -> bool:
    admins = set(settings.admin_telegram_ids)
    if not admins:
        return True
    return user.telegram_id in admins


@requires_auth
async def help_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: User
) -> None:
    assert update.effective_message is not None
    body = _USER_HELP + (_ADMIN_HELP if _is_admin(user) else "")
    await update.effective_message.reply_text(
        body, parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True
    )
