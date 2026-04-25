"""Microstructure service — translates a raw CLOB order book into the
quantitative features the scoring engine consumes:

* ``spread``          — best_ask − best_bid (absolute probability units).
* ``top5_depth``      — aggregate size (shares) in the top 5 levels.
* ``ofi``             — Order Flow Imbalance, ``(bidDepth − askDepth) /
                         (bidDepth + askDepth)``.  Positive = buy
                         pressure, negative = sell pressure, null when
                         one side is empty.
* ``estimate_slippage`` — simulated VWAP walk through the book for a
                         given USD notional; returns
                         ``(vwap_price, slippage_bps, filled_usd)``.

The :class:`MicrostructureFeatures` dataclass is consumed by the
Liquidity pillar of the scorer and by :mod:`app.services.execution_cost`.

All math is pure so unit tests can inject fake order books without a
network round-trip.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.config.settings import settings
from app.integrations.polymarket_client import (
    MarketSnapshot,
    OrderBook,
    PolymarketClient,
)
from app.utils.logger import get_logger


logger = get_logger(__name__)


@dataclass
class MicrostructureFeatures:
    token_id: str
    best_bid: Optional[float]
    best_ask: Optional[float]
    mid: Optional[float]
    spread: Optional[float]
    spread_pct: Optional[float]
    top5_bid_depth: float
    top5_ask_depth: float
    ofi: Optional[float]
    has_book: bool = False
    # Populated only when ``estimate_slippage`` is called.
    quote_size_usd: Optional[float] = None
    quote_vwap: Optional[float] = None
    quote_slippage_bps: Optional[float] = None
    quote_filled_usd: Optional[float] = None
    extras: dict = field(default_factory=dict)

    @property
    def is_tradeable(self) -> bool:
        """A book with either side empty or a too-wide spread is not tradeable."""
        if not self.has_book or self.spread is None:
            return False
        if self.best_bid is None or self.best_ask is None:
            return False
        return self.spread <= settings.microstructure_max_spread


class MicrostructureService:
    def __init__(self, polymarket: PolymarketClient) -> None:
        self._poly = polymarket

    async def snapshot(
        self,
        market: MarketSnapshot,
        side: str = "yes",
    ) -> MicrostructureFeatures:
        """Fetch and evaluate the order book for one side of ``market``."""
        token_id = market.token_id_for_side(side)
        if not token_id:
            return _empty_features("")
        book = await self._poly.get_order_book(token_id)
        if book is None:
            return _empty_features(token_id)
        return self.from_book(book)

    def from_book(self, book: OrderBook) -> MicrostructureFeatures:
        best_bid = book.best_bid
        best_ask = book.best_ask
        mid = book.mid
        spread = book.spread

        top5_bid = sum(lv.size for lv in book.bids[:5])
        top5_ask = sum(lv.size for lv in book.asks[:5])

        ofi: Optional[float] = None
        if (top5_bid + top5_ask) > 0:
            ofi = (top5_bid - top5_ask) / (top5_bid + top5_ask)

        spread_pct: Optional[float] = None
        if mid and mid > 0 and spread is not None:
            spread_pct = spread / mid

        return MicrostructureFeatures(
            token_id=book.token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            mid=mid,
            spread=spread,
            spread_pct=spread_pct,
            top5_bid_depth=top5_bid,
            top5_ask_depth=top5_ask,
            ofi=ofi,
            has_book=bool(book.bids or book.asks),
        )

    def estimate_slippage(
        self,
        book: OrderBook,
        *,
        side: str,
        size_usd: float,
    ) -> tuple[Optional[float], Optional[float], float]:
        """Walk the book for a buy of ``size_usd`` and compute a VWAP.

        When buying ``yes`` (``side='yes'``) we consume the ``asks``.
        When selling back, we consume the ``bids``.

        Returns ``(vwap, slippage_bps, filled_usd)``.  ``slippage_bps`` is
        measured against the mid-price (positive = worse than mid for
        buyers).  If the book is too thin, ``filled_usd`` < ``size_usd``.
        """
        if size_usd <= 0:
            return None, None, 0.0

        is_buy = side.lower() in ("yes", "buy", "long")
        levels = book.asks if is_buy else book.bids
        if not levels:
            return None, None, 0.0

        mid = book.mid or (levels[0].price if levels else None)
        if mid is None or mid <= 0:
            return None, None, 0.0

        remaining = size_usd
        total_shares = 0.0
        cost = 0.0
        for lv in levels:
            level_notional = lv.price * lv.size
            if remaining <= level_notional:
                shares = remaining / lv.price
                cost += shares * lv.price
                total_shares += shares
                remaining = 0.0
                break
            total_shares += lv.size
            cost += level_notional
            remaining -= level_notional

        filled = size_usd - remaining
        if total_shares <= 0:
            return None, None, 0.0
        vwap = cost / total_shares
        slippage_bps = (vwap - mid) / mid * 10_000
        if not is_buy:
            slippage_bps = -slippage_bps  # selling: positive bps = worse
        return vwap, slippage_bps, filled


def _empty_features(token_id: str) -> MicrostructureFeatures:
    return MicrostructureFeatures(
        token_id=token_id,
        best_bid=None,
        best_ask=None,
        mid=None,
        spread=None,
        spread_pct=None,
        top5_bid_depth=0.0,
        top5_ask_depth=0.0,
        ofi=None,
        has_book=False,
    )
