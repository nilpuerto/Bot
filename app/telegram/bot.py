"""Telegram bot factory & broadcast helpers.

Constructs a ``telegram.ext.Application`` with every handler wired.  The
orchestrator starts the application's lifecycle; this module only owns
wiring and messaging primitives so tests can instantiate it without
network access.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from telegram import Bot, BotCommand, BotCommandScopeChat, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
)

from app.config.settings import settings
from app.database.models import Signal, Trade, User
from app.database.repositories.users_repo import UsersRepository
from app.database.session import session_scope
from app.services.trade_executor import TradeExecutor
from app.services.trade_monitor import TradeMonitor
from app.services.wallet_cluster import WalletClusterScanner
from app.telegram.formatters import (
    escape_md,
    signal_card,
)
from app.telegram.handlers.admin import (
    backtest_handler,
    secondary_handler,
    weights_handler,
)
from app.telegram.handlers.callbacks import callback_dispatcher
from app.telegram.handlers.close import close_handler
from app.telegram.handlers.help import help_handler
from app.telegram.handlers.info import info_handler
from app.telegram.handlers.mode import mode_handler
from app.telegram.handlers.modify import build_modify_conversation
from app.telegram.handlers.scanner import scanner_handler
from app.telegram.handlers.settings import build_settings_conversation
from app.telegram.handlers.signals import signals_handler
from app.telegram.handlers.start import start_handler
from app.telegram.handlers.trades import trades_handler
from app.telegram.keyboards import signal_actions_keyboard
from app.utils.logger import get_logger


logger = get_logger(__name__)


class _PollingNetworkBlipFilter(logging.Filter):
    """Drop the giant "Exception happened while polling for updates."
    traceback that ``telegram.ext.Updater`` prints whenever the long-poll
    HTTP connection drops.

    PTB always retries with exponential backoff on these — the bot keeps
    working — but the default ``logger.exception(...)`` call dumps the
    full ``httpx.ReadError`` / ``httpcore.*`` traceback to stderr, which
    is alarming and useless noise.  We replace it with a single warning
    line via the structlog logger.
    """

    _NETWORK_EXC_TYPES: tuple[type[BaseException], ...] = (
        # imported lazily to avoid circular imports at module load
    )

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        if record.exc_info and record.exc_info[1] is not None:
            from telegram.error import NetworkError, TimedOut

            if isinstance(record.exc_info[1], (NetworkError, TimedOut)):
                logger.warning(
                    "telegram_polling_network_blip",
                    error_type=type(record.exc_info[1]).__name__,
                    error=str(record.exc_info[1]) or "<empty>",
                )
                return False
        return True


_polling_filter_installed = False


def _install_polling_filter() -> None:
    global _polling_filter_installed
    if _polling_filter_installed:
        return
    flt = _PollingNetworkBlipFilter()
    for name in ("telegram.ext.Updater", "telegram.ext._updater", "telegram.updater"):
        logging.getLogger(name).addFilter(flt)
    _polling_filter_installed = True


def build_application(
    *,
    trade_executor: TradeExecutor,
    polymarket_client,
    trade_monitor: Optional[TradeMonitor] = None,
    cluster_scanner: Optional[WalletClusterScanner] = None,
    balance_provider=None,
) -> Application:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in the environment.")

    _install_polling_filter()

    defaults = Defaults(
        parse_mode=ParseMode.MARKDOWN_V2,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .defaults(defaults)
        .post_init(_register_commands_menu)
        .build()
    )

    # Share services with handlers via ``bot_data``
    app.bot_data["trade_executor"] = trade_executor
    app.bot_data["polymarket_client"] = polymarket_client
    app.bot_data["trade_monitor"] = trade_monitor
    app.bot_data["cluster_scanner"] = cluster_scanner
    app.bot_data["balance_provider"] = balance_provider

    # Conversation handlers *must* be installed before the generic
    # callback dispatcher — their patterns are more specific.
    app.add_handler(build_settings_conversation())
    app.add_handler(build_modify_conversation())

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler(["help", "commands"], help_handler))
    app.add_handler(CommandHandler("info", info_handler))
    app.add_handler(CommandHandler("trades", trades_handler))
    app.add_handler(CommandHandler("signals", signals_handler))
    app.add_handler(CommandHandler("mode", mode_handler))
    app.add_handler(CommandHandler("close", close_handler))
    app.add_handler(CommandHandler("scanner", scanner_handler))

    # Admin-only commands (weight inspection, backtester, scout toggle).
    app.add_handler(CommandHandler("weights", weights_handler))
    app.add_handler(CommandHandler("backtest", backtest_handler))
    app.add_handler(CommandHandler("secondary", secondary_handler))

    # Global callback dispatcher handles buy/ignore/mode/close but NOT
    # settings:* (own conversation) and NOT modify:* (own conversation).
    app.add_handler(
        CallbackQueryHandler(callback_dispatcher, pattern=r"^(buy|ignore|mode|close):")
    )

    # Silence transient long-poll network errors — PTB auto-reconnects,
    # but by default it dumps the full traceback to stderr which makes
    # the terminal look like the bot is dying when it isn't.  We log a
    # one-line warning and let PTB carry on.
    app.add_error_handler(_global_error_handler)

    return app


async def _global_error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Swallow transient httpx / Telegram network errors so the polling
    loop's auto-retry doesn't spam stderr with full tracebacks.

    Anything else (logic bugs, bad payloads, etc.) is re-logged at
    ``error`` level with the traceback so we don't lose real failures.
    """
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning(
            "telegram_network_blip",
            error_type=type(err).__name__,
            error=str(err) or "<empty>",
        )
        return
    logger.error(
        "telegram_handler_error",
        error_type=type(err).__name__ if err else "Unknown",
        error=str(err) if err else "",
        exc_info=err,
    )


# ---------------------------------------------------------------------------
#   Slash-menu registration (populates the "/" autocomplete in Telegram UI)
# ---------------------------------------------------------------------------

_USER_MENU: list[BotCommand] = [
    BotCommand("start", "Welcome + disclaimer"),
    BotCommand("help", "List every command"),
    BotCommand("info", "Balance, mode, open PnL"),
    BotCommand("trades", "Your open positions"),
    BotCommand("signals", "Last 10 opportunities"),
    BotCommand("mode", "Switch SAFE / SEMI / AUTO"),
    BotCommand("settings", "Risk, SL, notifications"),
    BotCommand("close", "Close a trade (/close <id>)"),
    BotCommand("scanner", "Live whale clusters"),
    BotCommand("cancel", "Abort the current dialog"),
]

_ADMIN_MENU: list[BotCommand] = _USER_MENU + [
    BotCommand("weights", "Show learned pillar weights"),
    BotCommand("backtest", "Replay a tape (/backtest <file>)"),
    BotCommand("secondary", "Toggle low-attention scout on|off"),
]


async def _register_commands_menu(app: Application) -> None:
    """Push the /-autocomplete to Telegram.

    Default scope gets the user-level list; admin IDs get an additional
    scoped list so they see privileged commands only in their own chat.
    """
    try:
        await app.bot.set_my_commands(_USER_MENU)
        for admin_id in settings.admin_telegram_ids:
            try:
                await app.bot.set_my_commands(
                    _ADMIN_MENU, scope=BotCommandScopeChat(chat_id=admin_id)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "admin_menu_failed", admin_id=admin_id, error=str(exc)
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("commands_menu_failed", error=str(exc))


# ---------------------------------------------------------------------------
#   Broadcast helpers (called from the orchestrator when a signal fires)
# ---------------------------------------------------------------------------

async def broadcast_signal(
    bot: Bot,
    *,
    signal: Signal,
    score: float,
    trader_aligned: int,
    trader_conviction_usd: float,
    recipients: Optional[Iterable[User]] = None,
    high_confidence: bool = False,
) -> None:
    """Send the signal card to all eligible users.

    SAFE users receive the card without buttons.
    SEMI users receive the ``[✅ Approve] [❌ Ignore] [✏️ Modify amount]``
    keyboard.
    AUTO users see an informational card (the orchestrator has already
    acted, and they get an additional "trade opened" notification).
    """
    text = signal_card(
        signal=signal,
        score=score,
        trader_aligned=trader_aligned,
        trader_conviction_usd=trader_conviction_usd,
        high_confidence=high_confidence,
    )

    if recipients is None:
        async with session_scope() as session:
            recipients = await UsersRepository(session).list_allowed()

    for user in recipients:
        try:
            if user.mode.value == "semi":
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=text,
                    reply_markup=signal_actions_keyboard(signal.id),
                )
            else:
                await bot.send_message(chat_id=user.telegram_id, text=text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "telegram_send_failed",
                telegram_id=user.telegram_id,
                error=str(exc),
            )


async def notify_trade_closed(
    bot: Bot,
    *,
    trade: Trade,
    reason: str,
    close_price: float,
) -> None:
    msg = (
        f"◆ *TRADE CLOSED*\n\n"
        f"▸ `#{trade.id}`  {escape_md((trade.market_question or '—')[:60])}\n"
        f"▸ Reason `{escape_md(reason)}`  •  price `{escape_md(f'{close_price:.3f}')}`\n"
        f"▸ PnL `{escape_md(f'{float(trade.pnl):+.2f}')}$` "
        f"\\({escape_md(f'{float(trade.pnl_pct):+.2f}')}%\\)"
    )
    from sqlalchemy import select

    from app.database.models import User as UserModel

    async with session_scope() as session:
        res = await session.execute(select(UserModel).where(UserModel.id == trade.user_id))
        user = res.scalar_one_or_none()
    if user is None or not user.notifications_enabled:
        return
    try:
        await bot.send_message(chat_id=user.telegram_id, text=msg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("notify_trade_closed_failed", error=str(exc))
