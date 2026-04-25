"""Data-Quality Scorer — the first gate on every incoming headline.

Each :class:`NewsItem` is scored on four deterministic axes (total 0..100):

=====================  =======  ==========================================
Axis                   Weight   Rationale
=====================  =======  ==========================================
Source reliability     0..50    Reuters / Bloomberg / AP / official gov
                                 wires score at the top; anonymous blogs
                                 or aggregators sit at the bottom.
Language certainty     0..30    "X confirmed Y" > "reports say X" >
                                 "rumour: X".  Rumours are still passed
                                 down-pipeline but pre-penalised.
Recency                0..10    Fresh (<60 s) > warm (<5 min) > cold.
Cross-source corrob.   0..10    Count of distinct sources that reported
                                 the same canonical headline in the last
                                 ``DQ_CORROBORATION_WINDOW_MINUTES``.
=====================  =======  ==========================================

Only headlines whose total score ``>= settings.dq_min_score`` are sent
to the (expensive) Mistral analyser.  Everything else is marked seen
and dropped.

The scorer is intentionally **stateless** w.r.t. the database — the
corroboration lookup is injected by the caller so we can unit-test the
pure math without a Postgres round-trip.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.config.settings import settings
from app.integrations.rss_client import NewsItem
from app.utils.text import normalize
from app.utils.time import seconds_since


# --- Source reliability registry --------------------------------------------
# Values are the UPPER bound of the "source" component.  Matching is
# substring-based against the normalised feed title.  Unknown sources get
# a middling 30 — the bot can still trade them but with a penalty.

SOURCE_RELIABILITY: dict[str, int] = {
    "reuters": 50,
    "bloomberg": 50,
    "associated press": 48,
    "ap news": 48,
    "ap top": 48,
    "financial times": 46,
    "wall street journal": 46,
    "bbc": 44,
    "bbc news": 44,
    "the economist": 42,
    "axios": 38,
    "cnbc": 38,
    "politico": 36,
    "the guardian": 36,
    "nyt": 40,
    "new york times": 40,
    "washington post": 38,
    "cnn": 30,
    "fox": 28,
    "forbes": 26,
    "twitter": 20,
    "x.com": 20,
    "medium": 18,
    "blog": 12,
    "substack": 15,
}
DEFAULT_SOURCE_SCORE = 30

# --- Language certainty regexes --------------------------------------------
# Ordered from strong to weak.  The first match wins.

_CONFIRMED_RE = re.compile(
    r"\b(confirmed|announces|announced|declares|declared|signs|signed|"
    r"approved|votes|voted|passes|passed|ruled|rules|wins|won|appoints|"
    r"appointed|killed|dies|died|resigns|resigned|arrested|charged|"
    r"indicted|fired|launched|launches)\b",
    re.IGNORECASE,
)
_REPORTED_RE = re.compile(
    r"\b(reports?|reported|sources?|says?|said|claims?|claimed|allegedly|"
    r"expected|set to|likely to|may|might|plans to|planning|intends)\b",
    re.IGNORECASE,
)
_RUMOUR_RE = re.compile(
    r"\b(rumou?r|unconfirmed|speculation|speculating|hinted|whisper|leak)\b",
    re.IGNORECASE,
)


@dataclass
class DQScore:
    total: float
    source: float
    certainty: float
    recency: float
    corroboration: float
    passed: bool
    corroborators: int = 0
    reason: str = ""
    details: dict = field(default_factory=dict)


class DataQualityScorer:
    """Pure, synchronous scorer.  Inject ``corroborating_sources_fn`` when
    you want the real Postgres-backed lookup; unit tests leave it as
    ``None`` which implies zero corroboration (a worst-case bound)."""

    def __init__(
        self,
        *,
        min_score: Optional[float] = None,
        source_reliability: Optional[dict[str, int]] = None,
    ) -> None:
        self.min_score = (
            min_score if min_score is not None else settings.dq_min_score
        )
        self.source_reliability = source_reliability or SOURCE_RELIABILITY

    # ---- Component calculations --------------------------------------

    def _source_score(self, source: Optional[str]) -> tuple[float, str]:
        if not source:
            return float(DEFAULT_SOURCE_SCORE), "unknown"
        key = normalize(source)
        # Longest match wins — "bbc news" before "bbc".
        best = DEFAULT_SOURCE_SCORE
        best_key = "unknown"
        for tier_key, tier_score in self.source_reliability.items():
            if tier_key in key and tier_score >= best:
                if tier_score > best or len(tier_key) > len(best_key):
                    best = tier_score
                    best_key = tier_key
        return float(best), best_key

    @staticmethod
    def _certainty_score(text: str) -> tuple[float, str]:
        if not text:
            return 0.0, "empty"
        if _CONFIRMED_RE.search(text):
            return 30.0, "confirmed"
        if _REPORTED_RE.search(text):
            return 15.0, "reported"
        if _RUMOUR_RE.search(text):
            return 5.0, "rumour"
        return 8.0, "neutral"  # neutral factual headline

    @staticmethod
    def _recency_score(item: NewsItem) -> tuple[float, float]:
        if item.published_at is None:
            return 3.0, -1.0  # unknown age — neutral-minus
        age = seconds_since(item.published_at)
        if age < 60:
            return 10.0, age
        if age < 300:
            return 6.0, age
        if age < 900:
            return 2.0, age
        return 0.0, age

    @staticmethod
    def _corroboration_score(n: int) -> float:
        # Each distinct additional source adds 3 points, capped at 10.
        return min(10.0, 3.0 * max(0, n))

    # ---- Public API ---------------------------------------------------

    def score(
        self,
        item: NewsItem,
        *,
        corroborators: int = 0,
    ) -> DQScore:
        source_score, source_tier = self._source_score(item.source)
        text = f"{item.title}\n{item.summary or ''}"
        certainty, certainty_label = self._certainty_score(text)
        recency, age = self._recency_score(item)
        corroboration = self._corroboration_score(corroborators)

        total = source_score + certainty + recency + corroboration
        total = max(0.0, min(100.0, total))

        passed = total >= self.min_score
        reason = "ok" if passed else f"dq_below_threshold_{total:.1f}"

        return DQScore(
            total=round(total, 2),
            source=round(source_score, 2),
            certainty=round(certainty, 2),
            recency=round(recency, 2),
            corroboration=round(corroboration, 2),
            corroborators=corroborators,
            passed=passed,
            reason=reason,
            details={
                "source_tier": source_tier,
                "certainty_label": certainty_label,
                "age_seconds": round(age, 1) if age is not None else None,
            },
        )


def count_corroborators(
    known_sources: Iterable[str], candidate_source: Optional[str]
) -> int:
    """Count distinct outlets excluding ``candidate_source``.

    Used by :class:`app.services.news_ingestion.NewsIngestionService` to
    turn a list of ``(hash, source)`` pairs from the ``news_seen`` table
    into a single scalar.
    """
    candidate = normalize(candidate_source or "")
    seen: set[str] = set()
    for s in known_sources:
        n = normalize(s or "")
        if not n or n == candidate:
            continue
        seen.add(n)
    return len(seen)
