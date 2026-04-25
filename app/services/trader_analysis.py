"""Top-trader analysis.

Two responsibilities:

1. **Refresh**: periodically pull the Polymarket leaderboard and seed any
   manually curated wallets, upserting into ``top_traders``.  For each
   active trader, we persist their recent trades into ``trader_positions``.
2. **Confirm**: on demand, return a :class:`TraderConfirmation` for a given
   market — how many tracked top traders have entered, with what conviction,
   and the dominant side.  Used by the signal scoring system.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.config.settings import settings
from app.database.models import TopTrader, TraderPosition
from app.database.repositories.traders_repo import TradersRepository
from app.database.session import session_scope
from app.integrations.polymarket_client import PolymarketClient, UserTrade
from app.utils.logger import get_logger
from app.utils.time import utcnow


logger = get_logger(__name__)


@dataclass
class TraderConfirmation:
    aligned_count: int
    conviction_usd: float
    dominant_side: Optional[str]  # 'yes' / 'no' / None
    high_conviction: bool

    @property
    def confirmed(self) -> bool:
        return self.aligned_count >= 1 and self.conviction_usd > 0


class TraderAnalysisService:
    def __init__(
        self,
        polymarket: PolymarketClient,
        conviction_threshold_usd: Optional[float] = None,
        lookback_minutes: Optional[int] = None,
    ) -> None:
        self._poly = polymarket
        self._conviction_threshold = (
            conviction_threshold_usd
            if conviction_threshold_usd is not None
            else settings.trader_conviction_usd
        )
        self._lookback = (
            lookback_minutes if lookback_minutes is not None else settings.trader_lookback_minutes
        )
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    # ---- periodic refresh ------------------------------------------------

    async def run_refresh_loop(self) -> None:
        interval = settings.trader_refresh_interval_seconds
        while not self._stop.is_set():
            try:
                await self.refresh_once()
            except Exception as exc:  # defensive
                logger.exception("trader_refresh_error", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def refresh_once(self) -> None:
        whitelist = settings.tracked_wallets

        if whitelist:
            # Whitelist mode — ignore the public leaderboard and only
            # track the user-curated wallets.  The upsert keeps any
            # existing metadata intact while guaranteeing the row exists
            # and is marked active.
            async with session_scope() as session:
                repo = TradersRepository(session)
                for addr in whitelist:
                    await repo.upsert_trader(
                        wallet_address=addr,
                        label=None,
                        roi_30d=None,
                        volume_30d_usd=None,
                        last_checked_at=utcnow(),
                    )
            logger.info("trader_refresh_whitelist_mode", wallets=len(whitelist))
        else:
            # Step 1: refresh leaderboard (auto-seed mode)
            leaderboard = await self._poly.get_leaderboard(limit=50)
            async with session_scope() as session:
                repo = TradersRepository(session)
                for entry in leaderboard:
                    await repo.upsert_trader(
                        wallet_address=entry.wallet_address,
                        label=entry.label,
                        roi_30d=_dec(entry.roi),
                        volume_30d_usd=_dec(entry.volume_usd),
                        last_checked_at=utcnow(),
                    )

        # Step 2: poll recent trades for each active trader
        async with session_scope() as session:
            repo = TradersRepository(session)
            active = await repo.list_active()

        if whitelist:
            # Keep polling strictly to the whitelist even if older rows
            # from a previous auto-seed run are still marked active.
            active = [t for t in active if t.wallet_address.lower() in whitelist]

        if not active:
            logger.debug("trader_refresh_no_active_traders")
            return

        for trader in active:
            trades = await self._poly.get_trades_for_wallet(trader.wallet_address, limit=25)
            if not trades:
                continue
            positions = [
                _to_position(trader.id, t) for t in trades if t.market_id
            ]
            if not positions:
                continue
            async with session_scope() as session:
                await TradersRepository(session).record_positions(positions)

        logger.info("trader_refresh_done", traders=len(active))

    # ---- on-demand confirmation -----------------------------------------

    async def confirm(self, market_id: str) -> TraderConfirmation:
        if not market_id:
            return TraderConfirmation(0, 0.0, None, False)

        async with session_scope() as session:
            repo = TradersRepository(session)
            positions = await repo.recent_on_market(market_id, lookback_minutes=self._lookback)

        if not positions:
            return TraderConfirmation(0, 0.0, None, False)

        side_counts: Counter[str] = Counter(p.side.value for p in positions)
        dominant_side, _ = side_counts.most_common(1)[0]
        aligned = [p for p in positions if p.side.value == dominant_side]
        conviction = float(sum((p.size_usd or Decimal("0")) for p in aligned))

        return TraderConfirmation(
            aligned_count=len({p.trader_id for p in aligned}),
            conviction_usd=conviction,
            dominant_side=dominant_side,
            high_conviction=conviction >= self._conviction_threshold,
        )


# ---- helpers ---------------------------------------------------------------

def _dec(value: Optional[float]) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _to_position(trader_id: int, trade: UserTrade) -> TraderPosition:
    from app.database.models import TradeSide

    side_enum = TradeSide.YES if trade.side == "yes" else TradeSide.NO
    return TraderPosition(
        trader_id=trader_id,
        market_id=trade.market_id,
        market_slug=trade.market_slug,
        side=side_enum,
        price=_dec(trade.price),
        size_usd=Decimal(str(trade.size_usd or 0)),
        tx_hash=trade.tx_hash,
        observed_at=utcnow(),
    )
