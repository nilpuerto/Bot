"""Markdown-V2 formatters — a premium "apple dark" look.

All user-facing text from the bot flows through this module so the visual
language stays consistent.  Uses Markdown V2 with careful escaping.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from app.database.models import Signal, Trade, TradeSide, UserMode
from app.services.portfolio import PortfolioSnapshot


_MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"


def escape_md(text: str | int | float | Decimal | None) -> str:
    if text is None:
        return ""
    s = str(text)
    return "".join("\\" + ch if ch in _MDV2_SPECIAL else ch for ch in s)


# ---------- Signal card -----------------------------------------------------

def _ai_confidence_label(
    urgency: int, score: float, ai_confidence: int | None = None
) -> str:
    """Map raw AI confidence (if provided) or urgency/score to a human label.

    Prefers the AI's own 0-100 confidence when available; falls back to
    a blend of urgency + composite score for older signals.
    """
    if ai_confidence is not None:
        if ai_confidence >= 85 and (urgency >= 9 or score >= 80):
            return "VERY HIGH"
        if ai_confidence >= 70:
            return "HIGH"
        if ai_confidence >= 50:
            return "MEDIUM"
        return "LOW"
    if urgency >= 9 and score >= 80:
        return "VERY HIGH"
    if urgency >= 8 or score >= 70:
        return "HIGH"
    if urgency >= 6 or score >= 55:
        return "MEDIUM"
    return "LOW"


def _pillar_line(signal: Signal) -> str | None:
    """Render the 4-pillar breakdown line when the signal carries v2
    fields.  Returns ``None`` for legacy rows so the card stays compact.
    """
    fv = signal.feature_vector if isinstance(signal.feature_vector, dict) else None
    if not fv:
        return None

    # Weighted components (news/liq/misp/timing) are recoverable from the
    # raw 0..1 values + caps.  We prefer the raw numbers so the line
    # remains readable even if weights drift.
    def _pct(raw_key: str, cap: float) -> str:
        raw = fv.get(raw_key)
        if raw is None:
            return "—"
        try:
            val = float(raw) * cap
        except (TypeError, ValueError):
            return "—"
        return f"{val:.0f}/{cap:.0f}"

    # Edge-first caps (news is a hard gate, 0 points).
    news = _pct("news_raw", 0.0)
    liq = _pct("liquidity_raw", 25.0)
    misp = _pct("mispricing_raw", 60.0)
    timing = _pct("timing_raw", 15.0)

    phase = signal.timing_phase or fv.get("phase")
    edge = signal.expected_edge_pct
    z = signal.mispricing_z
    slip = signal.slippage_bps

    extras: list[str] = []
    if phase is not None:
        extras.append(f"phase `{phase}`")
    if z is not None:
        extras.append(f"z `{escape_md(f'{float(z):+.2f}')}`")
    if edge is not None:
        extras.append(f"edge `{escape_md(f'{float(edge):.2f}%')}`")
    if slip is not None:
        extras.append(f"slip `{escape_md(f'{float(slip):.0f}bps')}`")

    extras_str = ("  •  " + "  •  ".join(extras)) if extras else ""
    return (
        f"▸ *Pillars*  News `{escape_md(news)}` · Liq `{escape_md(liq)}` · "
        f"Misp `{escape_md(misp)}` · Time `{escape_md(timing)}`{extras_str}"
    )


def signal_card(
    *,
    signal: Signal,
    score: float,
    trader_aligned: int,
    trader_conviction_usd: float,
    high_confidence: bool = False,
    ai_confidence: int | None = None,
    volume_delta_pct: float | None = None,
) -> str:
    """Render the premium signal card — edge-first layout.

    Edge-first refactor: ``net_edge_pct``, ``|z|`` and ``phase`` are
    the PRIMARY lines because they are the measurable quantities that
    justify the trade.  The 0..100 composite score is shown only as a
    secondary cosmetic metric alongside urgency.
    """
    price = signal.market_price or Decimal("0")
    urgency = signal.urgency
    impact = signal.impact.value if hasattr(signal.impact, "value") else str(signal.impact)
    side = "YES" if impact == "bullish" else "NO" if impact == "bearish" else "—"

    header = "🚨 *NEW PRYM SIGNAL*"
    if high_confidence:
        header = "⚡ *HIGH\\-CONFIDENCE PRYM SIGNAL*"

    volume_part = ""
    if volume_delta_pct is not None:
        volume_part = f"  •  Vol ↑ {volume_delta_pct:.0f}%"

    # Measurable edge triad — primary content.
    edge_str = (
        f"{float(signal.expected_edge_pct):+.2f}%"
        if signal.expected_edge_pct is not None
        else "—"
    )
    z_str = (
        f"{float(signal.mispricing_z):+.2f}"
        if signal.mispricing_z is not None
        else "—"
    )
    phase_str = (
        str(signal.timing_phase) if signal.timing_phase is not None else "—"
    )

    lines = [
        header,
        "",
        f"▸ *Net edge*  `{escape_md(edge_str)}`",
        f"▸ *Mispricing z*  `{escape_md(z_str)}`",
        f"▸ *Phase*  `{escape_md(phase_str)}`",
        "",
        f"▸ *News*  {escape_md(signal.news_title)}",
        f"▸ *Market*  {escape_md(signal.market_question or '—')}",
        f"▸ *Price*  `{escape_md(f'{float(price):.3f}')}`{escape_md(volume_part)}",
        f"▸ *Meta*  urg `{urgency}/10`  •  score `{escape_md(f'{score:.0f}/100')}`",
    ]
    if trader_aligned > 0:
        lines.append(
            f"▸ *Wallets*  `{trader_aligned} aligned` "
            f"\\(${escape_md(f'{trader_conviction_usd:,.0f}')}\\)"
        )
    pillar_line = _pillar_line(signal)
    if pillar_line is not None:
        lines.append(pillar_line)
    lines.extend(
        [
            "",
            f"▸ *Suggested action*  *BUY {escape_md(side)}*",
        ]
    )
    if signal.news_url:
        lines.append("")
        lines.append(f"[Source]({escape_md(signal.news_url)})")
    return "\n".join(lines)


# ---------- /info -----------------------------------------------------------

def portfolio_card(snapshot: PortfolioSnapshot) -> str:
    usdc = float(snapshot.usdc_available)
    cap = float(snapshot.configured_cap)
    effective = float(snapshot.effective_balance)
    in_pos = float(snapshot.in_bot_positions_usd)
    marks = float(snapshot.holdings_mark_usd)
    est_pf = float(snapshot.estimated_portfolio_usd)
    pnl = float(snapshot.total_pnl)
    pnl_sign = "+" if pnl >= 0 else ""
    mode_emoji = {
        "safe": "🛡",
        "semi": "⚖",
        "auto": "⚡",
        "crypto": "₿",
    }.get(snapshot.mode, "•")

    # --- Balance block -----------------------------------------------------
    if usdc > 0:
        usdc_line = (
            f"▸ *Liquid USDC*   `${escape_md(f'{usdc:,.2f}')}` "
            "\\(on\\-chain, auto\\)"
        )
    elif snapshot.balance_status == "simulation":
        usdc_line = "▸ *Liquid USDC*   `—` \\(simulation mode\\)"
    else:
        usdc_line = (
            "▸ *Liquid USDC*   `—` "
            "\\(live mode but balance unavailable: check wallet/funder/RPC\\)"
        )

    cap_line = (
        f"▸ *Your cap*      `${escape_md(f'{cap:,.2f}')}` \\(manual ceiling\\)"
        if cap > 0
        else "▸ *Your cap*      `none` \\(uses full liquid USDC\\)"
    )
    effective_line = (
        f"▸ *Will deploy*   `${escape_md(f'{effective:,.2f}')}` "
        "\\(next trade sizing\\)"
    )

    return "\n".join(
        [
            "◆ *PORTFOLIO*",
            "",
            "💰 *Balance*",
            usdc_line,
            cap_line,
            effective_line,
            f"▸ *In positions*  `${escape_md(f'{in_pos:,.2f}')}` "
            "\\(committed notional\\)",
            f"▸ *Marks \\(~\\)*     `${escape_md(f'{marks:,.2f}')}` "
            "\\(open shares × mid, approx\\.\\)",
            f"▸ *Est\\. total*     `${escape_md(f'{est_pf:,.2f}')}` "
            "\\(liquid \\+ marks\\)",
            "",
            "📊 *Performance*",
            f"▸ *Total PnL*     `{escape_md(pnl_sign + f'{pnl:,.2f}')}$`",
            f"▸ *Win rate*      `{escape_md(f'{snapshot.winrate_pct:.1f}%')}`",
            f"▸ *Open trades*   `{snapshot.open_trades}`",
            f"▸ *Today*         `{snapshot.trades_today} trades`",
            "",
            f"▸ *Mode*          {mode_emoji}  *{escape_md(snapshot.mode.upper())}*",
            "",
            "_Liquid from RPC\\. Marks \\(shares×mid\\) from DB snapshots\\._",
        ]
    )


# ---------- /trades ---------------------------------------------------------

def trades_list(trades: Iterable[Trade]) -> str:
    lines = ["◆ *OPEN TRADES*", ""]
    found = False
    for t in trades:
        found = True
        side = t.side.value.upper() if isinstance(t.side, TradeSide) else str(t.side).upper()
        sim_badge = " `SIM`" if t.is_simulated else ""
        pnl = float(t.pnl or 0)
        pnl_sign = "+" if pnl >= 0 else ""
        lines.append(
            f"`#{t.id}`  *{escape_md((t.market_question or '—')[:48])}*{sim_badge}\n"
            f"  {side}  entry `{escape_md(f'{float(t.entry_price):.3f}')}` "
            f"→ now `{escape_md(f'{float(t.current_price or t.entry_price):.3f}')}` "
            f"  PnL `{escape_md(pnl_sign + f'{pnl:.2f}')}$`"
        )
    if not found:
        lines.append("_No open trades\\._")
    return "\n\n".join(lines) if found else "\n".join(lines)


# ---------- /signals --------------------------------------------------------

def signals_list(signals: Iterable[Signal]) -> str:
    lines = ["◆ *RECENT SIGNALS*", ""]
    any_found = False
    for s in signals:
        any_found = True
        badge = {
            "new": "•",
            "sent": "📤",
            "acted": "✅",
            "ignored": "❌",
            "expired": "⌛",
        }.get(s.status.value, "•")
        lines.append(
            f"{badge} `#{s.id}` *{escape_md(s.news_title[:60])}*\n"
            f"  {escape_md(s.market_question or '—')} • "
            f"score `{escape_md(f'{float(s.score):.0f}')}` • "
            f"urg `{s.urgency}`"
        )
    if not any_found:
        lines.append("_No recent signals\\._")
    return "\n\n".join(lines) if any_found else "\n".join(lines)


# ---------- /start ----------------------------------------------------------

START_MESSAGE = (
    "◆ *PRYM SIGNALS*\n\n"
    "An intelligent news\\-driven trading assistant for prediction markets\\.\n\n"
    "*How it works*\n"
    "▸ Monitors breaking news in real time\n"
    "▸ AI filters only actionable, high\\-impact events\n"
    "▸ Cross\\-confirms with top Polymarket traders\n"
    "▸ Alerts or trades for you — your rules, your risk\n\n"
    "*Modes*\n"
    "🛡 *SAFE*  \\- alerts only\n"
    "⚖ *SEMI*  \\- you confirm each trade\n"
    "⚡ *AUTO*  \\- only very high\\-confidence signals are executed\n\n"
    "*Commands*\n"
    "Type `/help` to see the full list\\.\n\n"
    "⚠️ *Risk disclaimer*: trading prediction markets carries risk\\. "
    "Never deploy funds you cannot afford to lose\\."
)


# ---------- Mode switch ----------------------------------------------------

def mode_changed(mode: UserMode | str) -> str:
    value = mode.value if hasattr(mode, "value") else str(mode)
    emoji = {"safe": "🛡", "semi": "⚖", "auto": "⚡", "crypto": "₿", "max": "🚀"}.get(value, "•")
    return f"Mode set to {emoji} *{escape_md(value.upper())}*\\."


# ---------- Crypto Mode -----------------------------------------------------

def crypto_mode_switched() -> str:
    """Sent right after the user flips ``/mode -> CRYPTO``."""
    return (
        "₿ *CRYPTO MODE ACTIVE*\n\n"
        "▸ Watching BTC `5m / 1h / 1d` Polymarket binaries\\.\n"
        "▸ Real\\-time spot from Binance \\+ Coinbase\\.\n"
        "▸ News pipeline muted — overlay only for `1h / 1d`\\.\n"
        "▸ No auto SL/TP — close manually with `/close <id>`\\."
    )


def _horizon_emoji(horizon: str) -> str:
    return {"5m": "⚡", "1h": "🕐", "1d": "📅"}.get(horizon, "•")


def crypto_entry_card(
    *,
    horizon: str,
    side: str,
    entry_price: float,
    size_usd: float,
    balance_pct: float,
    edge_pct: float,
    p_fair: float,
    spot: float,
    seconds_left: int,
    reasons: list[str],
    sentiment: float | None = None,
) -> str:
    em = _horizon_emoji(horizon)
    side_str = side.upper()
    reasons_str = (
        ", ".join(escape_md(r) for r in reasons[:6]) if reasons else "lag_arb_only"
    )
    minutes = seconds_left // 60
    seconds = seconds_left % 60
    sentiment_line = ""
    if sentiment is not None and abs(sentiment) >= 0.05:
        sign = "\\+" if sentiment > 0 else "\\-"
        sentiment_line = (
            f"\n▸ *News overlay*  `{sign}{escape_md(f'{abs(sentiment):.2f}')}`"
        )
    return (
        f"{em} *BTC {escape_md(horizon.upper())} — {escape_md(side_str)} OPENED*\n\n"
        f"▸ *Entry*       `{escape_md(f'{entry_price:.3f}')}` "
        f"\\(\\${escape_md(f'{size_usd:,.2f}')}, "
        f"{escape_md(f'{balance_pct:.1f}')}% bal\\)\n"
        f"▸ *Edge*        `{escape_md(f'{edge_pct:+.2f}%')}`  •  "
        f"*Fair* `{escape_md(f'{p_fair:.3f}')}`  •  "
        f"*Spot* `\\${escape_md(f'{spot:,.0f}')}`\n"
        f"▸ *Reasons*     {reasons_str}\n"
        f"▸ *Closes in*   `{minutes:02d}:{seconds:02d}`"
        f"{sentiment_line}"
    )


def crypto_late_scoop_card(
    *,
    horizon: str,
    side: str,
    entry_price: float,
    size_usd: float,
    balance_pct: float,
    seconds_left: int,
) -> str:
    minutes = seconds_left // 60
    seconds = seconds_left % 60
    return (
        f"💧 *BTC {escape_md(horizon.upper())} LATE SCOOP — {escape_md(side.upper())}*\n\n"
        f"▸ Imbalance scoop at `{escape_md(f'{entry_price:.3f}')}` "
        f"\\(\\${escape_md(f'{size_usd:,.2f}')}, "
        f"{escape_md(f'{balance_pct:.1f}')}% bal\\)\n"
        f"▸ Closes in `{minutes:02d}:{seconds:02d}`"
    )


def crypto_exit_suggestion(
    *,
    trade_id: int,
    horizon: str,
    side: str,
    entry_price: float,
    current_price: float,
    edge_pct_now: float,
) -> str:
    return (
        f"⚠️ *EXIT SUGGESTION — BTC {escape_md(horizon.upper())} {escape_md(side.upper())}*\n\n"
        f"▸ Trade `#{trade_id}` entry `{escape_md(f'{entry_price:.3f}')}` "
        f"→ now `{escape_md(f'{current_price:.3f}')}`\n"
        f"▸ Live edge `{escape_md(f'{edge_pct_now:+.2f}%')}` — consider `/close {trade_id}`\\."
    )


def crypto_skip_log(reason: str, **fields: object) -> str:
    """Compact, non-Markdown debugging line (logger only)."""
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    return f"crypto_skip reason={reason} {parts}"


# ---------- MAX Mode -------------------------------------------------------

def max_mode_switched() -> str:
    """Sent right after the user flips ``/mode -> MAX``."""
    return (
        "🚀 *MAX MODE ACTIVE*\n\n"
        "▸ BTC `5m` Up/Down sniper — fires at *T\\-10s* before close\\.\n"
        "▸ Window\\-delta dominant signal \\(7 weighted indicators\\)\\.\n"
        "▸ *Aggressive sizing*: bets only your accumulated MAX profit\\. "
        "If profit ≤ 0, falls back to `30%` of bankroll\\.\n"
        "▸ News pipeline muted — no auto SL/TP\\.\n"
        "▸ Use `/close <id>` to exit early\\."
    )


def max_entry_card(
    *,
    side: str,
    entry_price: float,
    size_usd: float,
    balance: float,
    cumulative_profit: float,
    confidence: float,
    window_delta_pct: float,
    reasons: list[str],
    seconds_left: int,
    fallback_used: bool,
    slug: str | None,
) -> str:
    minutes = seconds_left // 60
    seconds = seconds_left % 60
    bal_pct = (size_usd / balance * 100.0) if balance > 0 else 0.0
    reasons_str = (
        ", ".join(escape_md(r) for r in reasons) if reasons else "no_reasons"
    )
    profit_line = (
        f"▸ *Funding* `30% bankroll` \\(no profit yet\\)\n"
        if fallback_used
        else f"▸ *Funding* profits `\\${escape_md(f'{cumulative_profit:,.2f}')}`\n"
    )
    slug_line = (
        f"▸ *Market*  `{escape_md(slug)}`\n" if slug else ""
    )
    return (
        f"🚀 *MAX BTC 5m — {escape_md(side.upper())} OPENED*\n\n"
        f"{slug_line}"
        f"▸ *Entry*    `{escape_md(f'{entry_price:.3f}')}` "
        f"\\(\\${escape_md(f'{size_usd:,.2f}')}, "
        f"{escape_md(f'{bal_pct:.1f}')}% bal\\)\n"
        f"▸ *Window Δ* `{escape_md(f'{window_delta_pct:+.3f}%')}`  •  "
        f"*Conf* `{escape_md(f'{confidence:.2f}')}`\n"
        f"{profit_line}"
        f"▸ *Reasons*  {reasons_str}\n"
        f"▸ *Closes in* `{minutes:02d}:{seconds:02d}`"
    )


# ---------- /settings header -----------------------------------------------

def settings_header(user) -> str:
    """Short recap shown at the top of the live settings panel."""
    from app.config.settings import settings

    mode = user.mode.value if hasattr(user.mode, "value") else str(user.mode)
    notif = "ON" if user.notifications_enabled else "OFF"
    active = "Active" if user.is_active else "Paused"
    # Trailing stop is mandatory (no longer user-toggleable) — surface its
    # parameters so the user can see the protection rule at a glance.
    trail = (
        f"+{settings.trailing_activation_pct:g}% arm / "
        f"\\-{settings.trailing_pct:g}% drop"
    )
    return (
        "◆ *SETTINGS*\n\n"
        f"▸ Mode           *{escape_md(mode.upper())}*\n"
        f"▸ Trailing stop  `{trail}`\n"
        f"▸ Notifications  *{notif}*\n"
        f"▸ Status         *{escape_md(active)}*\n"
        f"▸ Risk / trade   `{escape_md(str(user.risk_pct))}%`\n"
        f"▸ Max / day      `{user.max_trades_per_day}`\n"
        f"▸ Auto urgency   `{user.auto_urgency_threshold}`\n\n"
        "_Tap any row to change it\\._"
    )
