"""Seed ``top_traders`` with a curated list of wallet addresses — or, by
default, with the top-N traders of the Polymarket leaderboard.

Running the script is idempotent: existing rows are updated, new ones
inserted.  You can run it any time to bootstrap the table; the live
``TraderAnalysisService.run_refresh_loop`` keeps it fresh afterwards.

Resolution order:

1. ``TRACKED_WALLETS`` env var (highest priority — wins over everything).
2. ``SEED_WALLETS`` list below (hand-curated with full metadata).
3. Auto-fetch the top-N leaderboard from Polymarket.

Usage::

    # Auto-seed from the live Polymarket leaderboard (default)
    python -m scripts.seed_top_traders

    # Or hand-curate: set TRACKED_WALLETS in .env / edit SEED_WALLETS.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Optional

from app.config.settings import settings
from app.database.engine import dispose_engine
from app.database.repositories.traders_repo import TradersRepository
from app.database.session import session_scope
from app.integrations.polymarket_client import PolymarketClient
from app.utils.logger import configure_logging, get_logger
from app.utils.time import utcnow


# Leave empty to auto-fetch from Polymarket's public leaderboard.
# Override here if you already know which wallets to follow.  Note:
# ``TRACKED_WALLETS`` in ``.env`` takes precedence over this list.
SEED_WALLETS: list[dict] = [
    # {
    #     "wallet_address": "0x0000000000000000000000000000000000000000",
    #     "label": "Example Whale",
    #     "roi_30d": "0.35",
    #     "winrate": "0.62",
    #     "volume_30d_usd": "250000",
    # },
]

# How many leaderboard entries to pull when auto-seeding.
AUTO_SEED_LIMIT = 25


def _dec(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


async def _seed_manual(repo: TradersRepository, logger) -> int:
    for row in SEED_WALLETS:
        await repo.upsert_trader(
            wallet_address=row["wallet_address"].lower(),
            label=row.get("label"),
            roi_30d=_dec(row.get("roi_30d")),
            winrate=_dec(row.get("winrate")),
            volume_30d_usd=_dec(row.get("volume_30d_usd")),
            last_checked_at=utcnow(),
        )
    logger.info("seed_top_traders_manual_done", count=len(SEED_WALLETS))
    return len(SEED_WALLETS)


async def _seed_whitelist(repo: TradersRepository, logger, wallets: list[str]) -> int:
    for addr in wallets:
        await repo.upsert_trader(
            wallet_address=addr,
            label=None,
            roi_30d=None,
            winrate=None,
            volume_30d_usd=None,
            last_checked_at=utcnow(),
        )
    logger.info("seed_top_traders_whitelist_done", count=len(wallets))
    return len(wallets)


async def _seed_auto(repo: TradersRepository, logger) -> int:
    async with PolymarketClient(timeout=15.0) as poly:
        leaderboard = await poly.get_leaderboard(limit=AUTO_SEED_LIMIT)

    if not leaderboard:
        logger.warning("seed_top_traders_auto_empty")
        return 0

    for entry in leaderboard:
        await repo.upsert_trader(
            wallet_address=entry.wallet_address.lower(),
            label=entry.label,
            roi_30d=_dec(entry.roi),
            volume_30d_usd=_dec(entry.volume_usd),
            last_checked_at=utcnow(),
        )
    logger.info("seed_top_traders_auto_done", count=len(leaderboard))
    return len(leaderboard)


async def main() -> None:
    configure_logging()
    logger = get_logger(__name__)

    async with session_scope() as session:
        repo = TradersRepository(session)
        whitelist = settings.tracked_wallets
        if whitelist:
            count = await _seed_whitelist(repo, logger, whitelist)
        elif SEED_WALLETS:
            count = await _seed_manual(repo, logger)
        else:
            count = await _seed_auto(repo, logger)

    logger.info("seed_top_traders_complete", total_upserted=count)
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
