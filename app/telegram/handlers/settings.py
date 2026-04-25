"""/settings — live control panel.

The panel is rendered from the current :class:`User` row and supports:

* **Toggles** (mode ⇄ auto/semi/safe, stop loss, notifications, active) —
  applied immediately, no text input, the message is re-rendered in-place.
* **Value setters** (risk %, max trades / day, auto urgency) — tap a row
  to enter a short text-input conversation.

All changes take effect instantly — no restart required.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.database.models import User, UserMode
from app.database.repositories.users_repo import UsersRepository
from app.database.session import session_scope
from app.telegram.auth import _resolve_user, requires_auth
from app.telegram.formatters import escape_md, settings_header
from app.telegram.keyboards import settings_keyboard


STATE_MENU, STATE_ASK_VALUE = range(2)

_FIELD_PROMPTS = {
    "risk": (
        "Send new *risk %* per trade \\(e\\.g\\. `3`, `2\\.5`\\)\\.\n"
        "Typical range: `1` – `5`\\."
    ),
    "max": "Send new *max trades per day* \\(1 – 50\\)\\.",
    "urg": "Send new *auto urgency threshold* \\(0 – 10\\)\\.",
}


async def _render_panel(user: User) -> tuple[str, Any]:
    return settings_header(user), settings_keyboard(user)


@requires_auth
async def settings_entry(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: User
) -> int:
    assert update.effective_message is not None
    context.user_data["_prym_user_id"] = user.id
    text, kb = await _render_panel(user)
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb
    )
    return STATE_MENU


async def _refresh_panel_inline(update: Update, user_id: int) -> None:
    """Re-render the panel on the existing callback message after a toggle."""
    query = update.callback_query
    assert query is not None and query.message is not None
    async with session_scope() as session:
        user = await session.get(User, user_id)
    if user is None:
        return
    text, kb = await _render_panel(user)
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb
        )
    except Exception:
        # Ignore "message is not modified" / edit races — panel still valid.
        pass


async def _menu_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()

    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 2:
        return STATE_MENU
    _, action, *rest = parts

    user_id: Optional[int] = context.user_data.get("_prym_user_id")
    # Fallback: settings panel may outlive context.user_data in test envs.
    if user_id is None:
        user = await _resolve_user(update)
        if user is None:
            await query.answer("Access denied.", show_alert=True)
            return ConversationHandler.END
        context.user_data["_prym_user_id"] = user.id
        user_id = user.id

    if action == "close":
        try:
            await query.edit_message_text(
                "Settings closed\\.", parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception:
            pass
        return ConversationHandler.END

    if action == "toggle":
        await _handle_toggle(rest[0] if rest else "", user_id)
        await _refresh_panel_inline(update, user_id)
        return STATE_MENU

    if action == "set":
        field = rest[0] if rest else ""
        prompt = _FIELD_PROMPTS.get(field)
        if prompt is None:
            return STATE_MENU
        context.user_data["_prym_field"] = field
        await query.edit_message_text(prompt, parse_mode=ParseMode.MARKDOWN_V2)
        return STATE_ASK_VALUE

    return STATE_MENU


async def _handle_toggle(flag: str, user_id: int) -> None:
    """Apply a toggle action, which may be a boolean flag or a mode change."""
    if flag == "auto":
        # Toggle auto on/off: ON = UserMode.AUTO, OFF = SAFE
        async with session_scope() as session:
            repo = UsersRepository(session)
            u = await session.get(User, user_id)
            if u is None:
                return
            new_mode = UserMode.SAFE if u.mode == UserMode.AUTO else UserMode.AUTO
            await repo.set_mode(user_id, new_mode)
        return
    if flag == "semi":
        async with session_scope() as session:
            repo = UsersRepository(session)
            u = await session.get(User, user_id)
            if u is None:
                return
            new_mode = UserMode.SAFE if u.mode == UserMode.SEMI else UserMode.SEMI
            await repo.set_mode(user_id, new_mode)
        return

    # Boolean flags on the User row.  ``stop_loss`` was retired — the
    # trailing stop is now mandatory and no longer user-toggleable.
    sql_flag = {
        "notif": "notifications_enabled",
        "active": "is_active",
    }.get(flag)
    if sql_flag is None:
        return
    async with session_scope() as session:
        await UsersRepository(session).toggle_flag(user_id, sql_flag)


async def _value_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.effective_message is not None
    field = context.user_data.get("_prym_field")
    user_id = context.user_data.get("_prym_user_id")
    raw = (update.effective_message.text or "").strip()
    if not field or not user_id:
        return ConversationHandler.END

    update_kwargs: dict[str, Any] = {}
    try:
        if field == "risk":
            value = Decimal(raw)
            if value <= 0 or value > 20:
                raise ValueError("risk must be in (0, 20]")
            update_kwargs["risk_pct"] = value
        elif field == "max":
            value = int(raw)
            if value <= 0 or value > 50:
                raise ValueError("max_trades must be 1-50")
            update_kwargs["max_trades_per_day"] = value
        elif field == "urg":
            value = int(raw)
            if value < 0 or value > 10:
                raise ValueError("urgency must be 0-10")
            update_kwargs["auto_urgency_threshold"] = value
    except (InvalidOperation, ValueError) as exc:
        await update.effective_message.reply_text(
            f"Invalid value: `{escape_md(str(exc))}`", parse_mode=ParseMode.MARKDOWN_V2
        )
        return ConversationHandler.END

    async with session_scope() as session:
        await UsersRepository(session).update_settings(user_id, **update_kwargs)

    # Re-render the panel from fresh state as a new message.
    async with session_scope() as session:
        user = await session.get(User, user_id)
    if user is None:
        return ConversationHandler.END
    text, kb = await _render_panel(user)
    await update.effective_message.reply_text(
        "✅ Updated\\.\n\n" + text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb,
    )
    return ConversationHandler.END


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message:
        await update.effective_message.reply_text(
            "Cancelled\\.", parse_mode=ParseMode.MARKDOWN_V2
        )
    return ConversationHandler.END


def build_settings_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("settings", settings_entry)],
        states={
            STATE_MENU: [CallbackQueryHandler(_menu_selected, pattern=r"^settings:")],
            STATE_ASK_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _value_received)
            ],
        },
        fallbacks=[CommandHandler("cancel", _cancel)],
        name="settings_conv",
        persistent=False,
        per_message=False,
    )
