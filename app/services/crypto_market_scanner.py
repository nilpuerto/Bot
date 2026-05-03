"""Crypto market scanner — discovers Polymarket BTC binary markets.

Polls the Gamma REST endpoint at a high cadence and emits one
:class:`CryptoMarket` per *new* BTC Up/Down market the bot has not yet
seen.  We bucket by horizon (5 m / 1 h / 1 d) so the trader can apply
different gates and notification copy.

Slug heuristics used to classify horizon (Polymarket's slug naming is
fairly stable but we stay defensive):

* 5 m:  ``bitcoin-up-or-down-...`` and end_date within 6 minutes of now
* 1 h:  contains ``bitcoin-1h``, ``btc-1h``, or 5m..70m to expiry
* 1 d:  contains ``-eod`` / ``-daily`` / ``-1d`` or end_date within 30 h

The "strike" semantics differ per family.  For Up/Down markets the
"YES = price at maturity > price at open" interpretation maps cleanly
onto :func:`fair_prob_above`: we treat the market open as ``S_open``
and the strike as ``S_open`` itself.  For markets that explicitly
encode a strike (e.g. "BTC above $72k by EOD") we parse the number
out of the question.

The scanner stays defensive: anything ambiguous is logged with
``crypto_scanner_unparsed`` and skipped — better than a wrong trade.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Literal, Optional

from app.config.settings import settings
from app.integrations.polymarket_client import MarketSnapshot, PolymarketClient
from app.utils.logger import get_logger
from app.utils.time import utcnow


logger = get_logger(__name__)


Horizon = Literal["5m", "1h", "1d"]


@dataclass(frozen=True)
class CryptoMarket:
    """Normalised view of a Polymarket BTC binary market."""

    market: MarketSnapshot
    horizon: Horizon
    end_at: datetime
    # ``strike_kind == "above_open"`` means the market resolves YES iff
    # the spot at ``end_at`` is strictly above the candle/open price.
    # ``strike_kind == "absolute"`` means ``strike`` is a USD number
    # parsed from the question.
    strike_kind: Literal["above_open", "absolute"]
    strike: Optional[float]   # USD when absolute, ``None`` otherwise

    @property
    def seconds_left(self) -> float:
        return max(0.0, (self.end_at - utcnow()).total_seconds())

    @property
    def yes_token_id(self) -> Optional[str]:
        return self.market.yes_token_id

    @property
    def no_token_id(self) -> Optional[str]:
        return self.market.no_token_id


# ---- regex helpers --------------------------------------------------------

_RE_ABOVE_USD = re.compile(
    r"(?:above|over|greater\s+than|>=|>)\s*\$?\s*([0-9]{2,3}(?:[.,]?[0-9]{3})*(?:\.[0-9]+)?)\s*k?",
    re.IGNORECASE,
)
_RE_AT_USD = re.compile(
    r"\$\s*([0-9]{2,3}(?:[.,]?[0-9]{3})*(?:\.[0-9]+)?)\s*k?",
    re.IGNORECASE,
)


def _parse_usd_strike(question: str) -> Optional[float]:
    """Best-effort extraction of a USD strike from a question string.

    Handles ``"$72,500"``, ``"above 72k"``, ``"75000"``, etc.  Returns
    ``None`` when nothing convincing is found.
    """
    if not question:
        return None
    text = question.lower()
    for pattern in (_RE_ABOVE_USD, _RE_AT_USD):
        m = pattern.search(text)
        if not m:
            continue
        raw = m.group(1).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        if "k" in text[m.start():m.end()]:
            value *= 1_000.0
        if 1_000.0 <= value <= 10_000_000.0:
            return value
    return None


def _parse_end_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        # Polymarket uses ISO-8601 ``Z`` suffix.
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except (ValueError, TypeError):
        return None


def classify(market: MarketSnapshot) -> Optional[CryptoMarket]:
    """Return a :class:`CryptoMarket` if ``market`` is a BTC binary, else None."""
    slug = (market.slug or "").lower()
    question = (market.question or "").lower()
    if "bitcoin" not in slug and "btc" not in slug and "bitcoin" not in question:
        return None
    end_at = _parse_end_date(market.end_date)
    if end_at is None:
        return None
    seconds_left = (end_at - utcnow()).total_seconds()
    if seconds_left <= 0:
        return None

    # --- Horizon classification ---
    horizon: Optional[Horizon] = None
    if "up-or-down" in slug or "up_or_down" in slug or "updown" in slug:
        if seconds_left <= 7 * 60:
            horizon = "5m"
        elif seconds_left <= 75 * 60:
            horizon = "1h"
        elif seconds_left <= 32 * 3600:
            horizon = "1d"
    if horizon is None:
        if "1h" in slug or "hourly" in slug:
            horizon = "1h"
        elif "eod" in slug or "daily" in slug or "1d" in slug or "today" in slug:
            horizon = "1d"
        elif seconds_left <= 7 * 60 and ("bitcoin" in slug):
            horizon = "5m"
    if horizon is None:
        return None

    strike_value = _parse_usd_strike(market.question or "")
    if strike_value is not None:
        strike_kind: Literal["above_open", "absolute"] = "absolute"
    else:
        strike_kind = "above_open"

    return CryptoMarket(
        market=market,
        horizon=horizon,
        end_at=end_at,
        strike_kind=strike_kind,
        strike=strike_value,
    )


# ---- scanner --------------------------------------------------------------

OnMarket = Callable[[CryptoMarket], Awaitable[None]]


class CryptoMarketScanner:
    """Polls Polymarket Gamma for fresh BTC binary markets."""

    # Search queries we hit on each tick; deduplicated by market id.
    _QUERIES = ("bitcoin", "btc", "bitcoin up or down")

    def __init__(self, polymarket: PolymarketClient) -> None:
        self._poly = polymarket
        self._stop = asyncio.Event()
        self._seen_ids: set[str] = set()
        self._interval = max(2, settings.crypto_scanner_interval_seconds)

    def stop(self) -> None:
        self._stop.set()

    async def run(self, on_market: OnMarket) -> None:
        logger.info("crypto_market_scanner_started", interval_s=self._interval)
        while not self._stop.is_set():
            try:
                await self._tick(on_market)
            except Exception as exc:  # noqa: BLE001 — keep loop alive
                logger.exception("crypto_scanner_tick_error", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
        logger.info("crypto_market_scanner_stopped")

    async def _tick(self, on_market: OnMarket) -> None:
        new_markets: list[CryptoMarket] = []
        for query in self._QUERIES:
            results = await self._poly.search_markets(query, limit=20)
            for market in results:
                if not market.id or market.id in self._seen_ids:
                    continue
                classified = classify(market)
                if classified is None:
                    continue
                self._seen_ids.add(market.id)
                new_markets.append(classified)
        # Trim seen set to avoid unbounded growth: a few thousand ids is fine.
        if len(self._seen_ids) > 5_000:
            # Keep the latest 2_500 inserts (rough FIFO via list re-creation).
            self._seen_ids = set(list(self._seen_ids)[-2_500:])

        for cm in new_markets:
            logger.info(
                "crypto_market_discovered",
                market_id=cm.market.id,
                slug=cm.market.slug,
                horizon=cm.horizon,
                seconds_left=int(cm.seconds_left),
                strike_kind=cm.strike_kind,
                strike=cm.strike,
            )
            try:
                await on_market(cm)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "crypto_market_handler_error",
                    market_id=cm.market.id,
                    error=str(exc),
                )


__all__ = [
    "CryptoMarket",
    "CryptoMarketScanner",
    "Horizon",
    "classify",
]
