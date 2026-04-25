"""Inline-keyboard builders used by handlers.

Having all keyboards in one file keeps the visual layer consistent and
makes updating button text / order a one-file change.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.database.models import User


def signal_actions_keyboard(signal_id: int) -> InlineKeyboardMarkup:
    """Buttons shown next to every SEMI-mode signal card.

    Approve → executes trade with strategy default sizing
    Ignore  → marks signal ignored
    Modify  → opens a mini-conversation to set a custom USD size
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"buy:{signal_id}"),
                InlineKeyboardButton("❌ Ignore", callback_data=f"ignore:{signal_id}"),
            ],
            [
                InlineKeyboardButton(
                    "✏️ Modify amount", callback_data=f"modify:{signal_id}"
                ),
            ],
        ]
    )


def mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🛡 SAFE", callback_data="mode:safe"),
                InlineKeyboardButton("⚖ SEMI", callback_data="mode:semi"),
                InlineKeyboardButton("⚡ AUTO", callback_data="mode:auto"),
            ]
        ]
    )


def close_trade_keyboard(trade_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Close trade", callback_data=f"close:{trade_id}")]]
    )


# ---------------------------------------------------------------------------
#   Live settings panel — every row reflects current value / state.
# ---------------------------------------------------------------------------

def _onoff(enabled: bool) -> str:
    return "ON" if enabled else "OFF"


def settings_keyboard(user: User) -> InlineKeyboardMarkup:
    """Render a fully-live settings panel for ``user``.

    Rows 1-2 control the operating mode (AUTO / SEMI — SAFE = both off).
    Rows 3-5 are boolean toggles.  Rows 6-8 open a small prompt flow to
    set a numeric value.  The final row closes the panel.

    Every button's label encodes the *current* value so the user can
    always read the state without reopening the menu.
    """
    mode = user.mode.value if hasattr(user.mode, "value") else str(user.mode)
    auto_on = mode == "auto"
    semi_on = mode == "semi"

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"⚡ Auto trading: {_onoff(auto_on)}",
                    callback_data="settings:toggle:auto",
                ),
                InlineKeyboardButton(
                    f"⚖ Manual mode: {_onoff(semi_on)}",
                    callback_data="settings:toggle:semi",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"🔔 Notifications: {_onoff(user.notifications_enabled)}",
                    callback_data="settings:toggle:notif",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"{'🟢 Active' if user.is_active else '⏸ Paused'}",
                    callback_data="settings:toggle:active",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"💰 Risk: {user.risk_pct}%",
                    callback_data="settings:set:risk",
                ),
                InlineKeyboardButton(
                    f"📊 Max/day: {user.max_trades_per_day}",
                    callback_data="settings:set:max",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"🎯 Auto urgency: {user.auto_urgency_threshold}",
                    callback_data="settings:set:urg",
                ),
            ],
            [
                InlineKeyboardButton("✖ Close", callback_data="settings:close"),
            ],
        ]
    )
