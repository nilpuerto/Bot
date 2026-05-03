"""Inline-button callbacks: buy / ignore / mode / close.

Callback-data format: ``<action>:<target_id_or_value>``.
"""
from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from sqlalchemy.exc import SQLAlchemyError

from app.config.settings import settings
from app.database.models import (
    CloseReason,
    SignalStatus,
    TradeSide,
    User,
    UserMode,
)
from app.database.repositories.signals_repo import SignalsRepository
from app.database.repositories.trades_repo import TradesRepository
from app.database.repositories.users_repo import UsersRepository
from app.database.session import session_scope
from app.integrations.polymarket_client import MarketSnapshot
from app.services.strategy_engine import default_strategy
from app.services.entry_filters import entry_token_gate_fail_reason
from app.telegram.auth import _resolve_user
from app.telegram.formatters import escape_md, mode_changed
from app.utils.logger import get_logger


logger = get_logger(__name__)


async def callback_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Single entry point for ``CallbackQueryHandler``; dispatches by prefix."""
    query = update.callback_query
    if query is None:
        return
    # Defer answering until after we know what to do
    data = query.data or ""
    action, _, payload = data.partition(":")

    user = await _resolve_user(update)
    if user is None:
        await query.answer("Access denied.", show_alert=True)
        return

    handlers = {
        "buy": _handle_buy,
        "ignore": _handle_ignore,
        "mode": _handle_mode,
        "close": _handle_close_trade,
    }
    fn = handlers.get(action)
    if fn is None:
        await query.answer()
        return
    try:
        await fn(update, context, user, payload)
    except Exception as exc:  # defensive
        logger.exception("callback_error", action=action, payload=payload, error=str(exc))
        await query.answer("Something went wrong, check logs.", show_alert=True)


# ---- individual handlers ---------------------------------------------------

async def _handle_buy(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: User, payload: str
) -> None:
    query = update.callback_query
    assert query is not None

    try:
        signal_id = int(payload)
    except ValueError:
        await query.answer("Invalid signal.", show_alert=True)
        return

    async with session_scope() as session:
        signal = await SignalsRepository(session).get(signal_id)
    if signal is None:
        await query.answer("Signal not found.", show_alert=True)
        return

    executor = context.application.bot_data.get("trade_executor")
    polymarket = context.application.bot_data.get("polymarket_client")
    if executor is None or polymarket is None:
        await query.answer("Bot not ready yet.", show_alert=True)
        return

    if not signal.market_id:
        await query.answer("Signal has no market bound.", show_alert=True)
        return

    if not user.is_active:
        await query.answer(
            "Your account is Paused — toggle it in /settings.", show_alert=True
        )
        return

    market = await polymarket.get_market(signal.market_id)
    if market is None:
        await query.answer("Market unavailable right now.", show_alert=True)
        return

    side = "yes" if signal.impact.value == "bullish" else "no"
    price = market.best_yes_price if side == "yes" else market.best_no_price
    if price is None:
        await query.answer("No live price.", show_alert=True)
        return

    rej = entry_token_gate_fail_reason(price)
    if rej:
        await query.answer(
            f"Entry blocked ({rej}) — tune ENTRY_MAX_PRICE / IMPLIED bounds in .env.",
            show_alert=True,
        )
        return

    # Use the LiveBalanceProvider when available so SEMI-mode approvals
    # size against the *real* on-chain USDC (capped by the user-set
    # ``user.balance`` if configured) — exactly like AUTO mode does.
    balance_provider = context.application.bot_data.get("balance_provider")
    if balance_provider is not None:
        breakdown = await balance_provider.effective_balance(user)
        effective_balance = float(breakdown.effective)
    else:
        effective_balance = float(user.balance or 0)

    strategy = default_strategy()
    plan = strategy.sizing(
        balance=effective_balance,
        risk_pct=float(user.risk_pct or settings.default_risk_pct),
        entry_price=price,
        stop_loss_enabled=bool(user.stop_loss_enabled),
        score=float(signal.score or 0),
    )
    result = await executor.open_trade(
        user=user, signal=signal, market=market, side=side, plan=plan
    )

    if result.ok:
        badge = "🧪 SIM" if result.is_simulated else "✅ LIVE"
        await query.answer(f"{badge} — trade opened", show_alert=False)
        if query.message is not None:
            await query.message.reply_text(
                f"{badge} Opened trade `#{result.trade_id}` at `"
                f"{escape_md(f'{price:.3f}')}`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
    else:
        await query.answer(f"Blocked: {result.reason}", show_alert=True)


async def _handle_ignore(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: User, payload: str
) -> None:
    query = update.callback_query
    assert query is not None
    try:
        signal_id = int(payload)
    except ValueError:
        await query.answer("Invalid signal.", show_alert=True)
        return
    async with session_scope() as session:
        await SignalsRepository(session).set_status(signal_id, SignalStatus.IGNORED)
    await query.answer("Ignored.")


async def _handle_mode(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: User, payload: str
) -> None:
    query = update.callback_query
    assert query is not None
    try:
        mode = UserMode(payload)
    except ValueError:
        await query.answer("Invalid mode.", show_alert=True)
        return
    try:
        async with session_scope() as session:
            await UsersRepository(session).set_mode(user.id, mode)
    except SQLAlchemyError as exc:
        origin = getattr(exc, "orig", None)
        diag = str(origin or exc).lower()
        if mode is UserMode.CRYPTO and (
            "user_mode" in diag or "invalid input value for enum" in diag
        ):
            logger.warning(
                "mode_switch_enum_missing_crypto",
                user_id=user.id,
                error=str(origin or exc),
            )
            await query.answer(
                "Falta migrar Postgres: en SQL ejecuta ALTER TYPE user_mode ADD VALUE IF NOT EXISTS 'crypto';",
                show_alert=True,
            )
            return
        logger.exception(
            "mode_switch_db_error", user_id=user.id, mode=mode.value
        )
        await query.answer(
            "Error de base de datos; revisa logs (journalctl).", show_alert=True
        )
        return
    await query.answer(f"Mode → {mode.value.upper()}")
    if query.message is not None:
        await query.message.reply_text(mode_changed(mode), parse_mode=ParseMode.MARKDOWN_V2)


async def _handle_close_trade(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: User, payload: str
) -> None:
    query = update.callback_query
    assert query is not None
    executor = context.application.bot_data.get("trade_executor")
    if executor is None:
        await query.answer("Executor not ready.", show_alert=True)
        return
    try:
        trade_id = int(payload)
    except ValueError:
        await query.answer("Invalid trade id.", show_alert=True)
        return
    async with session_scope() as session:
        t = await TradesRepository(session).get(trade_id)
    if t is None or t.user_id != user.id:
        await query.answer("Not your trade.", show_alert=True)
        return
    result = await executor.close_trade(trade_id, reason=CloseReason.MANUAL)
    if result.ok:
        await query.answer("Closed.")
    else:
        await query.answer(f"Failed: {result.reason}", show_alert=True)


# ensure imports are used to avoid lint warnings
_ = (MarketSnapshot, TradeSide)
