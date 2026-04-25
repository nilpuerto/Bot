"""One-shot: pull news once, run them through the pipeline, print a report.

Useful for smoke-testing credentials without starting the full bot loop.
The live bot only reacts to news younger than ``NEWS_MAX_AGE_SECONDS``
(default 5 minutes); for a smoke test that constraint is pointless, so
we pass a very large ``max_age_seconds`` here to let the pipeline chew
on whatever the RSS feeds currently expose.

    python -m scripts.backfill_signals
"""
from __future__ import annotations

import asyncio

from app.config.settings import settings
from app.database.engine import dispose_engine
from app.integrations.mistral_client import MistralClient
from app.integrations.polymarket_client import PolymarketClient
from app.integrations.rss_client import RSSClient
from app.services.hard_filter import HardFilter
from app.services.market_matching import MarketMatchingService
from app.utils.logger import configure_logging, get_logger


async def main() -> None:
    configure_logging()
    logger = get_logger(__name__)
    if not settings.rss_feeds:
        logger.error("no_rss_feeds_configured")
        return

    async with RSSClient() as rss, MistralClient() as mistral, PolymarketClient() as poly:
        items = await rss.fetch_many(settings.rss_feeds)
        logger.info("items_fetched", count=len(items))

        # Smoke test: keep the keyword check but ignore the "fresh news
        # only" rule so the RSS feeds give us something to chew on.
        hf = HardFilter(max_age_seconds=7 * 24 * 3600)
        passed = [i for i in items if hf.passes(i)]
        logger.info("items_passed_hard_filter", count=len(passed))
        if not passed:
            logger.warning(
                "no_items_passed_hard_filter",
                hint="relax HARD_FILTER_KEYWORDS or wait for breaking news",
            )
            await dispose_engine()
            return

        matcher = MarketMatchingService(poly)
        shown = 0
        for item in passed:
            if shown >= 5:
                break
            ai = await mistral.analyze(
                title=item.title, source=item.source or "", summary=item.summary or ""
            )
            if ai is None:
                continue
            match = await matcher.find(
                ai_market_hint=ai.market,
                news_title=item.title,
                entities=ai.entities,
                category=ai.category,
            )
            print(
                f"[{ai.urgency}/10 {ai.impact:>7}]  "
                f"{(item.title or '')[:70]:70s}  "
                f"->  {(match.market.question if match else '-- no match')}"
            )
            shown += 1
        logger.info("backfill_done", analysed=shown, total_passed=len(passed))
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
