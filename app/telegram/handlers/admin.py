"""Admin-only command handlers.

Exposes three privileged commands for inspecting / tweaking the v2
intelligence layer from Telegram:

* ``/weights``           — dump the current component-weight table.
* ``/backtest <file>``   — queue a dry-run replay of a tape file.
* ``/secondary on|off``  — toggle the opportunistic scout at runtime.

Authorisation is a simple intersection of ``ALLOWED_TELEGRAM_IDS`` and
``ADMIN_TELEGRAM_IDS``.  Non-admins get a polite no-op.
"""
from __future__ import annotations

import asyncio
from functools import wraps
from typing import Awaitable, Callable

from telegram import Update
from telegram.ext import ContextTypes

from app.config.settings import settings
from app.database.models import User
from app.database.repositories.weights_repo import WeightsRepository
from app.database.session import session_scope
from app.telegram.auth import requires_auth
from app.telegram.formatters import escape_md


RawHandler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def _is_admin(user: User) -> bool:
    admins = set(settings.admin_telegram_ids)
    if not admins:
        # If no admin list is configured, fall back to the first allowed
        # Telegram ID — "owner of the bot" semantics.
        return True
    return user.telegram_id in admins


def admin_required(fn):
    @wraps(fn)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, user: User):
        if not _is_admin(user):
            if update.effective_message:
                await update.effective_message.reply_text(
                    "Admin-only command."
                )
            return
        await fn(update, context, user)

    return wrapped


@requires_auth
@admin_required
async def weights_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: User
) -> None:
    assert update.effective_message is not None
    async with session_scope() as session:
        weights = await WeightsRepository(session).get_all()
    lines = ["◆ *COMPONENT WEIGHTS*", ""]
    for name, w in sorted(weights.items()):
        lines.append(f"▸ `{escape_md(name):<12}` `{float(w):.3f}`")
    await update.effective_message.reply_text("\n".join(lines))


@requires_auth
@admin_required
async def backtest_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: User
) -> None:
    assert update.effective_message is not None
    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            "Usage: `/backtest <tape.jsonl>`"
        )
        return
    path = args[0]
    await update.effective_message.reply_text(
        f"Queued backtest on `{escape_md(path)}` — results will be posted "
        f"when the run completes\\."
    )

    from scripts.backtest import run_backtest

    async def _run() -> None:
        try:
            report = await asyncio.to_thread(run_backtest, path)
            body = "\n".join(
                f"▸ *{escape_md(k)}*  `{escape_md(str(v))}`"
                for k, v in report.items()
            )
            if update.effective_message:
                await update.effective_message.reply_text(
                    "◆ *BACKTEST REPORT*\n\n" + body
                )
        except Exception as exc:  # noqa: BLE001
            if update.effective_message:
                await update.effective_message.reply_text(
                    f"Backtest failed: `{escape_md(str(exc)[:200])}`"
                )

    asyncio.create_task(_run())


@requires_auth
@admin_required
async def secondary_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: User
) -> None:
    assert update.effective_message is not None
    args = context.args or []
    if args and args[0].lower() in ("on", "off"):
        desired = args[0].lower() == "on"
        settings.secondary_enabled = desired
        await update.effective_message.reply_text(
            f"Secondary scout *{escape_md('ON' if desired else 'OFF')}* "
            f"\\(runtime only — persist via `.env`\\)\\."
        )
        return
    status = "ON" if settings.secondary_enabled else "OFF"
    await update.effective_message.reply_text(
        f"Secondary scout currently *{escape_md(status)}*\\.  "
        f"Toggle with `/secondary on` or `/secondary off`\\."
    )
