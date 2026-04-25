"""Modify-amount flow — triggered from the "✏️ Modify amount" button.

A minimal :class:`ConversationHandler`:

1. Entry: callback matching ``^modify:<signal_id>`` — remember the signal
   and prompt for a USD amount.
2. State: plain text message — parse, clamp to strategy bounds, open the
   trade via :class:`TradeExecutor`.
3. Fallback: ``/cancel`` clears the pending intent.

Keeping this isolated from the settings conversation avoids state cross
talk in PTB — both conversations can run independently on the same chat
because their entry point patterns do not overlap.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

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

from app.config.settings import settings
from app.database.repositories.signals_repo import SignalsRepository
from app.database.session import session_scope
from app.services.sizing import band_bounds, band_for_score, band_pct, suggest_amount
from app.services.strategy_engine import default_strategy
from app.telegram.auth import _resolve_user
from app.telegram.formatters import escape_md
from app.utils.logger import get_logger


logger = get_logger(__name__)

STATE_AWAIT_AMOUNT = 0


async def modify_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    data = query.data or ""
    _, _, payload = data.partition(":")
    try:
        signal_id = int(payload)
    except ValueError:
        await query.answer("Invalid signal.", show_alert=True)
        return ConversationHandler.END

    user = await _resolve_user(update)
    if user is None:
        await query.answer("Access denied.", show_alert=True)
        return ConversationHandler.END

    context.user_data["_prym_modify_signal_id"] = signal_id

    # Pre-fetch the signal so the prompt can show a band-aware anchor
    # anchored to the user's current balance (the band is now a % of it).
    balance = float(user.balance or 0)
    anchor: float = float(settings.min_trade_usd)
    band_label = "low"
    band_lo, band_hi = band_bounds(band_label, balance)
    pct = band_pct(band_label)
    try:
        async with session_scope() as session:
            signal = await SignalsRepository(session).get(signal_id)
        if signal is not None:
            score_val = float(signal.score or 0)
            band_label = band_for_score(score_val)
            band_lo, band_hi = band_bounds(band_label, balance)
            pct = band_pct(band_label)
            anchor = suggest_amount(score_val, balance)
    except Exception as exc:  # noqa: BLE001
        logger.warning("modify_prefetch_failed", error=str(exc))

    await query.answer()
    if query.message is not None:
        await query.message.reply_text(
            (
                "✏️ *Custom amount*\n\n"
                f"Signal `#{signal_id}` · band *{escape_md(band_label.upper())}* "
                f"\\({escape_md(f'{pct:g}')}% of balance\\)\n"
                f"Suggested: `${escape_md(f'{anchor:.2f}')}`  "
                f"\\(band `${escape_md(f'{band_lo:.2f}')}` – "
                f"`${escape_md(f'{band_hi:.2f}')}`\\)\n\n"
                f"Send the USD amount — overall floor `${settings.min_trade_usd:g}`, "
                f"ceiling `${settings.max_trade_usd:g}`\\. "
                "Or /cancel to abort\\."
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    return STATE_AWAIT_AMOUNT


async def _amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.effective_message is not None
    signal_id = context.user_data.pop("_prym_modify_signal_id", None)
    if signal_id is None:
        return ConversationHandler.END

    raw = (update.effective_message.text or "").strip().replace("$", "").replace(",", ".")
    try:
        amount = float(Decimal(raw))
    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text(
            "Invalid number\\.", parse_mode=ParseMode.MARKDOWN_V2
        )
        return ConversationHandler.END

    lo = float(settings.min_trade_usd)
    hi = float(settings.max_trade_usd)
    if amount < lo or amount > hi:
        await update.effective_message.reply_text(
            f"Amount must be between `${lo:g}` and `${hi:g}`\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return ConversationHandler.END

    user = await _resolve_user(update)
    if user is None:
        return ConversationHandler.END
    if not user.is_active:
        await update.effective_message.reply_text(
            "Your account is currently *Paused*\\. Reactivate it in /settings\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return ConversationHandler.END

    async with session_scope() as session:
        signal = await SignalsRepository(session).get(signal_id)
    if signal is None:
        await update.effective_message.reply_text(
            "Signal not found\\.", parse_mode=ParseMode.MARKDOWN_V2
        )
        return ConversationHandler.END

    executor = context.application.bot_data.get("trade_executor")
    polymarket = context.application.bot_data.get("polymarket_client")
    if executor is None or polymarket is None or not signal.market_id:
        await update.effective_message.reply_text(
            "Bot not ready\\.", parse_mode=ParseMode.MARKDOWN_V2
        )
        return ConversationHandler.END

    market = await polymarket.get_market(signal.market_id)
    if market is None:
        await update.effective_message.reply_text(
            "Market unavailable\\.", parse_mode=ParseMode.MARKDOWN_V2
        )
        return ConversationHandler.END

    side = "yes" if signal.impact.value == "bullish" else "no"
    price = market.best_yes_price if side == "yes" else market.best_no_price
    if price is None:
        await update.effective_message.reply_text(
            "No live price\\.", parse_mode=ParseMode.MARKDOWN_V2
        )
        return ConversationHandler.END

    # Use the strategy for SL/TP with the user's override passed through
    # ``user_override`` so the sizing engine applies the band guard-rails
    # and risk-% safety cap correctly.
    strategy = default_strategy()
    plan = strategy.sizing(
        balance=float(user.balance or 0),
        risk_pct=float(user.risk_pct or settings.default_risk_pct),
        entry_price=price,
        stop_loss_enabled=bool(user.stop_loss_enabled),
        score=float(signal.score or 0),
        user_override=amount,
    )

    result = await executor.open_trade(
        user=user, signal=signal, market=market, side=side, plan=plan
    )
    if result.ok:
        badge = "🧪 SIM" if result.is_simulated else "✅ LIVE"
        await update.effective_message.reply_text(
            f"{badge} Opened `#{result.trade_id}` at `{escape_md(f'{price:.3f}')}` "
            f"for `${escape_md(f'{amount:.2f}')}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await update.effective_message.reply_text(
            f"Blocked: `{escape_md(result.reason)}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    return ConversationHandler.END


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("_prym_modify_signal_id", None)
    if update.effective_message:
        await update.effective_message.reply_text(
            "Cancelled\\.", parse_mode=ParseMode.MARKDOWN_V2
        )
    return ConversationHandler.END


def build_modify_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(modify_entry, pattern=r"^modify:\d+$")],
        states={
            STATE_AWAIT_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _amount_received)
            ],
        },
        fallbacks=[CommandHandler("cancel", _cancel)],
        name="modify_conv",
        persistent=False,
        per_message=False,
    )
