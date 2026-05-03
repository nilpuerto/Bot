"""BTC news sentiment overlay for Crypto Mode.

This is a *soft* layer: it never gates a 5-minute trade and never
substitutes for the lag-arb edge.  It exists because the user said:

    "no hara nada de restriccion, solo sera fuente de informacion
     porque alomejor te sale un analisis muy bien pero sale una
     noticia de algo de bitcoin que afecta lo contrario, y tendras
     que canviar, pero solo noticias para informacion y serviran
     mas para los mercados de 1h o 1 dia, pero simpre haras mas
     de 5minutos"

Implementation:

* Subscribes to the existing :class:`NewsIngestionService` *output*
  (we don't poll any new feed) — the orchestrator pushes ingested
  items into us via :meth:`record`.
* Maintains a rolling :class:`Sentiment` over a configurable window
  (default 30 min) computed from the AI's ``impact`` field on
  BTC-tagged signals.
* Exposes :meth:`modifier` returning a :class:`OverlayDecision`:
    - ``"hold"``  + ``scale=1.0``   — neutral / no opinion
    - ``"boost"`` + ``scale=1.25``  — sentiment aligns with side
    - ``"shrink"``+ ``scale=0.75``  — sentiment opposes
    - ``"veto"``  + ``scale=0.0``   — only on 1d markets when
      sentiment magnitude is extreme AND opposes the lag-arb side
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Literal, Optional

from app.config.settings import settings
from app.utils.logger import get_logger


logger = get_logger(__name__)


Action = Literal["hold", "boost", "shrink", "veto"]
Side = Literal["yes", "no"]
Horizon = Literal["5m", "1h", "1d"]


_BTC_TOKENS = {
    "btc",
    "bitcoin",
    "btcusd",
    "btcusdt",
    "ibit",
    "bitcoin etf",
}


def _is_btc(text: str, entities: list[str]) -> bool:
    """Loose BTC tagging: title or any extracted entity contains a token."""
    haystack = " ".join([text or "", " ".join(entities or [])]).lower()
    return any(tok in haystack for tok in _BTC_TOKENS)


@dataclass
class _NewsPoint:
    ts: float
    impact: float       # +1 bullish, -1 bearish, 0 neutral
    weight: float       # urgency / 10 (0..1)


@dataclass(frozen=True)
class OverlayDecision:
    action: Action
    scale: float
    sentiment: float
    reason: str


class CryptoNewsOverlay:
    def __init__(self) -> None:
        self._points: Deque[_NewsPoint] = deque(maxlen=200)
        self._window_seconds = max(60, settings.crypto_news_window_minutes * 60)

    # ---- public ingestion API -----------------------------------------

    def record(self, *, title: str, impact: str, urgency: int, entities: list[str]) -> None:
        """Push a news event into the overlay.  Non-BTC items are dropped."""
        if not settings.crypto_news_overlay_enabled:
            return
        if not _is_btc(title, entities or []):
            return
        impact_val = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}.get(
            impact.lower(), 0.0
        )
        weight = max(0.0, min(1.0, (urgency or 0) / 10.0))
        self._points.append(_NewsPoint(ts=time.monotonic(), impact=impact_val, weight=weight))
        logger.debug(
            "crypto_news_overlay_recorded",
            impact=impact,
            urgency=urgency,
            sentiment=self._current_sentiment(),
        )

    # ---- internals ----------------------------------------------------

    def _evict(self) -> None:
        cutoff = time.monotonic() - self._window_seconds
        while self._points and self._points[0].ts < cutoff:
            self._points.popleft()

    def _current_sentiment(self) -> float:
        """Weighted average sentiment in [-1, +1]."""
        self._evict()
        if not self._points:
            return 0.0
        total_w = sum(p.weight for p in self._points)
        if total_w <= 0:
            return 0.0
        s = sum(p.impact * p.weight for p in self._points) / total_w
        return max(-1.0, min(1.0, s))

    # ---- decision API -------------------------------------------------

    def modifier(self, side: Side, horizon: Horizon) -> OverlayDecision:
        """Return the size modifier the orchestrator should apply.

        * 5m markets: always ``hold`` (overlay ignored — your spec).
        * 1h markets: ``boost`` / ``shrink`` by 25 % when sentiment aligns
          / opposes; ``hold`` otherwise.
        * 1d markets: same as 1h, but a strong contradiction (|sent| >= 0.6
          and opposite sign) becomes a ``veto``.
        """
        if not settings.crypto_news_overlay_enabled or horizon == "5m":
            return OverlayDecision(
                action="hold", scale=1.0, sentiment=0.0, reason="ignored"
            )
        sent = self._current_sentiment()
        if sent == 0.0 or not self._points:
            return OverlayDecision(
                action="hold", scale=1.0, sentiment=0.0, reason="no_signal"
            )

        side_sign = 1.0 if side == "yes" else -1.0
        aligned = sent * side_sign  # > 0 if sentiment supports the side

        if horizon == "1d" and aligned <= -0.6:
            return OverlayDecision(
                action="veto", scale=0.0, sentiment=sent, reason="strong_contradiction_1d"
            )
        if aligned >= 0.25:
            return OverlayDecision(
                action="boost", scale=1.25, sentiment=sent, reason="aligned"
            )
        if aligned <= -0.25:
            return OverlayDecision(
                action="shrink", scale=0.75, sentiment=sent, reason="opposes"
            )
        return OverlayDecision(
            action="hold", scale=1.0, sentiment=sent, reason="weak"
        )

    @property
    def sentiment(self) -> float:
        return self._current_sentiment()


__all__ = ["CryptoNewsOverlay", "OverlayDecision"]
