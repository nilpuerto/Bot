"""Hard filter is the cost-saving choke point; lock its behavior down."""
from __future__ import annotations

from datetime import timedelta

from app.integrations.rss_client import NewsItem
from app.services.hard_filter import HardFilter
from app.utils.time import utcnow


def _item(title: str, minutes_old: int = 1, source: str = "Reuters", summary: str = "") -> NewsItem:
    return NewsItem(
        title=title,
        url="https://example.com",
        source=source,
        summary=summary,
        published_at=utcnow() - timedelta(minutes=minutes_old),
    )


def test_passes_with_strong_keyword_and_fresh() -> None:
    hf = HardFilter(keywords=["election", "war"], max_age_seconds=300)
    assert hf.passes(_item("Breaking: war declared in region", minutes_old=1)) is True


def test_fails_when_no_keyword() -> None:
    hf = HardFilter(keywords=["election"], max_age_seconds=300)
    assert hf.passes(_item("Stock market closes up 1%")) is False


def test_fails_when_too_old() -> None:
    hf = HardFilter(keywords=["election"], max_age_seconds=60)
    item = _item("Election result confirmed", minutes_old=10)
    assert hf.passes(item) is False


def test_source_allowlist_respected() -> None:
    hf = HardFilter(keywords=["war"], max_age_seconds=300, allowed_sources=["reuters"])
    assert hf.passes(_item("war breaking out", source="Reuters World")) is True
    assert hf.passes(_item("war breaking out", source="Unknown Blog")) is False


def test_case_insensitive_keyword_match() -> None:
    hf = HardFilter(keywords=["Election"], max_age_seconds=300)
    assert hf.passes(_item("ELECTION: candidate wins by landslide")) is True
