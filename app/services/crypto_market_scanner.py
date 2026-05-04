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
import time
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


@dataclass(frozen=True)
class ClassifyResult:
    """Outcome of :func:`classify_with_reason`. ``cm`` is ``None`` on rejection."""

    cm: Optional[CryptoMarket]
    reason: str  # "ok" or short rejection code


def classify_with_reason(market: MarketSnapshot) -> ClassifyResult:
    """Same as :func:`classify` but also returns *why* a market was rejected."""
    slug = (market.slug or "").lower()
    question = (market.question or "").lower()
    blob = f"{slug} {question}"

    has_btc = ("bitcoin" in blob) or ("btc" in blob)
    if not has_btc:
        return ClassifyResult(None, "not_btc")

    end_at = _parse_end_date(market.end_date)
    if end_at is None:
        return ClassifyResult(None, "no_end_date")
    seconds_left = (end_at - utcnow()).total_seconds()
    if seconds_left <= 0:
        return ClassifyResult(None, "expired")

    # --- Horizon classification (very permissive: any binary BTC market wins) ---
    horizon: Optional[Horizon] = None
    is_ud_style = (
        "up-or-down" in slug
        or "up_or_down" in slug
        or "updown" in slug
        or "-up-down-" in slug
        or "up or down" in blob
        or "up/down" in blob
    )
    if is_ud_style:
        if seconds_left <= 7 * 60:
            horizon = "5m"
        elif seconds_left <= 75 * 60:
            horizon = "1h"
        else:
            horizon = "1d"
    if horizon is None:
        if "1h" in slug or "hourly" in slug:
            horizon = "1h"
        elif "eod" in slug or "daily" in slug or "1d" in slug or "today" in slug:
            horizon = "1d"
        elif seconds_left <= 7 * 60:
            horizon = "5m"
        elif seconds_left <= 75 * 60:
            horizon = "1h"
        else:
            # Any other BTC binary still falls under the 1d bucket so the engine
            # at least *evaluates* it; the edge gate will reject if there is none.
            horizon = "1d"

    if not market.yes_token_id or not market.no_token_id:
        return ClassifyResult(None, "no_clob_tokens")

    strike_value = _parse_usd_strike(market.question or "")
    if strike_value is not None:
        strike_kind: Literal["above_open", "absolute"] = "absolute"
    else:
        strike_kind = "above_open"

    return ClassifyResult(
        CryptoMarket(
            market=market,
            horizon=horizon,
            end_at=end_at,
            strike_kind=strike_kind,
            strike=strike_value,
        ),
        "ok",
    )


def classify(market: MarketSnapshot) -> Optional[CryptoMarket]:
    """Return a :class:`CryptoMarket` if ``market`` is a BTC binary, else None."""
    return classify_with_reason(market).cm


# ---- scanner --------------------------------------------------------------

OnMarket = Callable[[CryptoMarket], Awaitable[Optional[str]]]
"""Callback contract for new markets.

The handler may return:

* ``None`` / ``"ok"`` — the market was processed (success or final skip).
* ``"retry"`` — transient skip (e.g. feed warming up); the scanner will
  re-evaluate the market on subsequent ticks instead of silently
  dropping it from the seen-set.
"""

_TRANSIENT_RETRY_REASONS = {"feed_warming", "feed_stale"}
_MAX_RETRY_AGE_S = 90.0


class CryptoMarketScanner:
    """Polls Polymarket Gamma for fresh BTC binary markets."""

    # Search queries we hit on each tick; deduplicated by market id.
    # The "up or down" / "next 5 minutes" variants are crucial for the
    # short-horizon BTC binaries — Gamma's full-text search ranks them
    # below the bulky monthly markets without an explicit hint.
    _QUERIES = (
        "bitcoin",
        "btc",
        "bitcoin up or down",
        "btc up or down",
        "bitcoin 5m",
        "bitcoin hourly",
    )

    def __init__(self, polymarket: PolymarketClient) -> None:
        self._poly = polymarket
        self._stop = asyncio.Event()
        self._seen_ids: set[str] = set()
        self._pending: dict[str, tuple[CryptoMarket, float]] = {}
        self._interval = max(2, settings.crypto_scanner_interval_seconds)
        self._tick_i = 0
        self._last_tick_log_m: float = 0.0

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
        merged: dict[str, MarketSnapshot] = {}
        for query in self._QUERIES:
            for m in await self._poly.search_markets(query, limit=50):
                if m.id:
                    merged[m.id] = m

        # Every ~15s also merge high-volume active markets — search alone
        # often misses short-lived BTC 5m listings already in the catalogue.
        self._tick_i += 1
        if self._tick_i % 3 == 0:
            for m in await self._poly.list_active_markets(limit=200):
                if m.id:
                    merged[m.id] = m

        new_markets: list[CryptoMarket] = []
        reject_counts: dict[str, int] = {}
        sample_btc_rejects: list[tuple[str, str]] = []
        for market in merged.values():
            if not market.id or market.id in self._seen_ids:
                continue
            if market.id in self._pending:
                continue
            res = classify_with_reason(market)
            if res.cm is None:
                reject_counts[res.reason] = reject_counts.get(res.reason, 0) + 1
                if res.reason != "not_btc" and len(sample_btc_rejects) < 5:
                    sample_btc_rejects.append((market.slug or market.id, res.reason))
                continue
            self._seen_ids.add(market.id)
            new_markets.append(res.cm)

        if len(self._seen_ids) > 5_000:
            self._seen_ids = set(list(self._seen_ids)[-2_500:])

        # Re-process markets previously kept "pending" (transient skip).
        retry_markets: list[CryptoMarket] = []
        now_ts = time.monotonic()
        for mid, (cm, first_seen) in list(self._pending.items()):
            if cm.seconds_left <= 5 or now_ts - first_seen > _MAX_RETRY_AGE_S:
                self._pending.pop(mid, None)
                continue
            retry_markets.append(cm)

        now_m = time.monotonic()
        if now_m - self._last_tick_log_m >= 60.0:
            self._last_tick_log_m = now_m
            btc_count = max(0, len(merged) - reject_counts.get("not_btc", 0))
            horizons_breakdown: dict[str, int] = {"5m": 0, "1h": 0, "1d": 0}
            for cm in new_markets:
                horizons_breakdown[cm.horizon] = horizons_breakdown.get(cm.horizon, 0) + 1
            logger.info(
                "crypto_scanner_tick",
                catalogue=len(merged),
                btc_candidates=btc_count,
                batch_discovered=len(new_markets),
                horizons=horizons_breakdown,
                seen_ids=len(self._seen_ids),
                rejects=reject_counts,
                sample=sample_btc_rejects,
            )

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
            await self._dispatch(on_market, cm, first_attempt=True)

        for cm in retry_markets:
            await self._dispatch(on_market, cm, first_attempt=False)

    async def _dispatch(
        self,
        on_market: OnMarket,
        cm: CryptoMarket,
        *,
        first_attempt: bool,
    ) -> None:
        try:
            result = await on_market(cm)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "crypto_market_handler_error",
                market_id=cm.market.id,
                error=str(exc),
            )
            self._pending.pop(cm.market.id, None)
            return

        if result == "retry":
            if first_attempt:
                self._pending[cm.market.id] = (cm, time.monotonic())
            # Otherwise it's already in self._pending.
            return
        self._pending.pop(cm.market.id, None)


__all__ = [
    "CryptoMarket",
    "CryptoMarketScanner",
    "Horizon",
    "classify",
]
