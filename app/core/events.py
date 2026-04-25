"""Internal asyncio event bus.

Lightweight dataclasses used to hand typed events between the news loop,
the signal pipeline and the Telegram broadcaster.  Avoids leaking ORM
objects across task boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class SignalEvent:
    signal_id: int
    urgency: int
    score: float
    trader_aligned: int
    trader_conviction_usd: float
    passes_auto: bool
    market_id: str
    market_question: str
    side: str
    entry_price: float
    news_title: str
    news_url: Optional[str] = None
