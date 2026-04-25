"""Quick snapshot of every table the bot writes to."""
from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.database.session import session_scope


TABLES = [
    "users",
    "news_seen",
    "signals",
    "trades",
    "top_traders",
    "trader_positions",
    "component_weights",
    "market_price_history",
    "daily_counters",
    "app_settings",
]


async def main() -> None:
    print(f"{'table':26s}  {'rows':>10s}  sample")
    print("-" * 78)
    for t in TABLES:
        try:
            async with session_scope() as session:
                res = await session.execute(text(f"SELECT count(*) FROM {t}"))
                n = int(res.scalar_one() or 0)
                print(f"{t:26s}  {n:>10d}")
        except Exception as exc:
            msg = str(exc).splitlines()[0][:80]
            print(f"{t:26s}  {'ERR':>10s}  {msg}")

    # show component_weights snapshot
    async with session_scope() as session:
        try:
            r = await session.execute(
                text(
                    "SELECT name, weight FROM component_weights ORDER BY name"
                )
            )
            print("\ncomponent_weights:")
            for row in r:
                print(f"  {row[0]:14s} {row[1]}")
        except Exception as exc:
            print(f"component_weights dump ERR: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
