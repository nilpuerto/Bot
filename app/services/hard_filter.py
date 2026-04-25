"""Hard filter — the cheap, deterministic gate BEFORE calling the AI.

Three conditions, all of which must be satisfied for the item to pass:

1. Title / summary contains at least one configured strong keyword.
2. Article is recent enough (``news_max_age_seconds``).
3. Source is on the allowlist (if one is configured; empty list = allow all).

This single module is responsible for ~95 % of cost reduction: only items
that survive reach Mistral.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from app.config.settings import settings
from app.integrations.rss_client import NewsItem
from app.utils.text import contains_any
from app.utils.time import is_fresh


@dataclass
class FilterResult:
    passed: bool
    reason: str


class HardFilter:
    def __init__(
        self,
        keywords: Optional[list[str]] = None,
        max_age_seconds: Optional[int] = None,
        allowed_sources: Optional[Iterable[str]] = None,
    ) -> None:
        self.keywords = keywords if keywords is not None else settings.hard_filter_keywords
        self.max_age_seconds = (
            max_age_seconds if max_age_seconds is not None else settings.news_max_age_seconds
        )
        self.allowed_sources = [s.lower() for s in allowed_sources] if allowed_sources else []

    def evaluate(self, item: NewsItem) -> FilterResult:
        text = f"{item.title}\n{item.summary or ''}"
        if not contains_any(text, self.keywords):
            return FilterResult(False, "no_strong_keyword")

        if item.published_at is not None and not is_fresh(item.published_at, self.max_age_seconds):
            return FilterResult(False, "too_old")

        if self.allowed_sources:
            src = (item.source or "").lower()
            if not any(allowed in src for allowed in self.allowed_sources):
                return FilterResult(False, "source_not_allowlisted")

        return FilterResult(True, "ok")

    def passes(self, item: NewsItem) -> bool:
        return self.evaluate(item).passed
