"""/scanner — live view of tracked-wallet clusters.

Shows the top ``(market, side)`` pairs where multiple top wallets
entered recently, ordered by wallet count × conviction.  The list
is informational only — no trade is placed here.  When a cluster
crosses the configured thresholds the orchestrator's
``_handle_cluster`` routes it through the full scoring pipeline and
may raise a regular signal in the normal alert stream.

Why expose it as a command?

* Users can *see* whale direction even for clusters below the auto
  threshold (e.g. 2 wallets with small conviction).
* It makes the smart-money layer auditable: you can always inspect
  what the bot is seeing, right now.
"""
from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.config.settings import settings
from app.database.models import User
from app.database.repositories.traders_repo import ClusterRow, TradersRepository
from app.database.session import session_scope
from app.telegram.auth import requires_auth
from app.telegram.formatters import escape_md
from app.utils.time import seconds_since


_TITLE = "◆ *SMART\\-MONEY SCANNER*"


def _emoji_for_side(side: str) -> str:
    return "🟢" if side == "yes" else "🔴"


def _age_str(row: ClusterRow) -> str:
    first_age_s = seconds_since(row.first_observed_at) or 0
    if first_age_s < 60:
        return f"{int(first_age_s)}s"
    if first_age_s < 3600:
        return f"{int(first_age_s / 60)}m"
    return f"{first_age_s / 3600:.1f}h"


def render_scanner(rows: list[ClusterRow]) -> str:
    header = (
        f"{_TITLE}\n\n"
        f"_Tracked wallets clustering in the last "
        f"{settings.cluster_window_hours}h_"
        f"  \\(min `{settings.cluster_min_wallets}` wallets for auto\\-signal\\)\n\n"
    )
    if not rows:
        return (
            header
            + "No active clusters yet\\.\n"
            + "Wallets take time to pile up — check back later\\.\n"
        )

    lines: list[str] = []
    for i, row in enumerate(rows, 1):
        question = (row.market_slug or row.market_id)[:50]
        emoji = _emoji_for_side(row.side)
        passes = row.wallet_count >= settings.cluster_min_wallets
        bullet = "▸" if passes else "◦"
        line = (
            f"{bullet} *{i}\\.* {emoji} `{escape_md(row.side.upper())}`  "
            f"*{row.wallet_count}*× wallets  "
            f"`${int(row.total_conviction_usd):,}`  "
            f"_{escape_md(_age_str(row))}_"
        )
        q_escaped = escape_md(question)
        lines.append(f"{line}\n    {q_escaped}")

    footer = (
        "\n\n_Auto\\-signals only fire when the cluster also clears the score gate "
        "\\(microstructure \\+ mispricing \\+ price\\)_"
    )
    return header + "\n".join(lines) + footer


@requires_auth
async def scanner_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: User
) -> None:
    """Fetch and render the current cluster table.

    We don't gate this behind admin — every allowed user can inspect
    whale activity.  The command is cheap (single aggregate query).
    """
    assert update.effective_message is not None

    async with session_scope() as session:
        rows = await TradersRepository(session).fetch_recent_clusters(
            window_minutes=settings.cluster_window_hours * 60,
            min_wallets=max(2, settings.cluster_min_wallets - 1),
            min_conviction_usd=0.0,
            limit=10,
        )

    body = render_scanner(rows)
    await update.effective_message.reply_text(
        body, parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True
    )
