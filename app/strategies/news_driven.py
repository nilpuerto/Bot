"""Legacy alias for :class:`~app.strategies.prym_strategy.PrymStrategy`.

The original news-driven strategy has been folded into the four-pillar
``PrymStrategy``.  This shim exists only so external imports
(``from app.strategies.news_driven import NewsDrivenStrategy``) keep
working.  New code should import :class:`PrymStrategy` directly.
"""
from __future__ import annotations

from app.strategies.prym_strategy import PrymStrategy


class NewsDrivenStrategy(PrymStrategy):
    name = "news_driven"
