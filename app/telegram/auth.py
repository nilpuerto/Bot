"""Telegram auth helpers.

The bot exposes sensitive trading actions — every handler is wrapped in
:func:`requires_auth` so that an unlisted Telegram user is rejected before
any work happens.  Authorised users are auto-created in ``users`` and
returned to the caller.
"""
from __future__ import annotations

from functools import wraps
from typing import Awaitable, Callable, Optional

from telegram import Update
from telegram.ext import ContextTypes

from app.config.settings import settings
from app.database.models import User
from app.database.repositories.users_repo import UsersRepository
from app.database.session import session_scope
from app.utils.logger import get_logger


logger = get_logger(__name__)

HandlerFn = Callable[[Update, ContextTypes.DEFAULT_TYPE, User], Awaitable[None]]
RawHandler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


async def _resolve_user(update: Update) -> Optional[User]:
    tg_user = update.effective_user
    if tg_user is None:
        return None

    allowed = set(settings.allowed_telegram_ids)
    if allowed and tg_user.id not in allowed:
        logger.warning("telegram_denied", telegram_id=tg_user.id, username=tg_user.username)
        return None

    async with session_scope() as session:
        user = await UsersRepository(session).get_or_create(
            telegram_id=tg_user.id, username=tg_user.username
        )
        # Detach from the session so callers can freely read fields.
        session.expunge(user)
        return user


def requires_auth(fn: HandlerFn) -> RawHandler:
    """Decorator: reject if user not allowed, otherwise inject the ``User``."""

    @wraps(fn)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await _resolve_user(update)
        if user is None or not user.is_allowed:
            if update.effective_message:
                await update.effective_message.reply_text(
                    "Access denied. Ask the admin to whitelist your Telegram ID."
                )
            return
        await fn(update, context, user)

    return wrapped
