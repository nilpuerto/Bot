"""Data-quality scorer — source / certainty / recency / corroboration."""
from __future__ import annotations

from datetime import timedelta

from app.integrations.rss_client import NewsItem
from app.services.data_quality import DataQualityScorer, count_corroborators
from app.utils.time import utcnow


def _item(
    title: str = "X confirmed Y",
    source: str = "Reuters",
    age_seconds: int = 30,
    summary: str | None = None,
) -> NewsItem:
    return NewsItem(
        title=title,
        url="https://example.com",
        source=source,
        published_at=utcnow() - timedelta(seconds=age_seconds),
        summary=summary,
    )


def test_high_tier_source_fresh_confirmed_passes() -> None:
    scorer = DataQualityScorer(min_score=70)
    score = scorer.score(_item(), corroborators=1)
    assert score.passed is True
    # 50 (reuters) + 30 (confirmed) + 10 (recency) + 3 (1 corroborator) = 93
    assert score.total >= 85


def test_blog_rumour_stale_fails() -> None:
    scorer = DataQualityScorer(min_score=70)
    score = scorer.score(
        _item(title="Rumour: X might happen", source="Medium", age_seconds=1200)
    )
    assert score.passed is False
    assert score.total < 70


def test_corroboration_bounded_at_ten() -> None:
    scorer = DataQualityScorer(min_score=70)
    score = scorer.score(_item(), corroborators=10)
    assert score.corroboration == 10.0


def test_count_corroborators_dedupes_candidate() -> None:
    sources = ["Reuters", "reuters", "Bloomberg", "Reuters"]
    n = count_corroborators(sources, candidate_source="Reuters")
    assert n == 1  # only Bloomberg remains after deduping


def test_unknown_source_gets_default_tier() -> None:
    scorer = DataQualityScorer(min_score=70)
    score = scorer.score(_item(source="TotallyMadeUpFeed"))
    # 30 (unknown) + 30 (confirmed) + 10 (fresh) = 70 borderline
    assert score.source == 30.0
