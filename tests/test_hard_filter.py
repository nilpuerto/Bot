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


# --- Blocklist tests ----------------------------------------------------------


def _hf_with_blocklist(*patterns: str) -> HardFilter:
    """Helper: filter with keyword=war + specific blocklist patterns."""
    return HardFilter(keywords=["war", "shooting", "election"], max_age_seconds=300, blocklist=list(patterns))


def test_blocklist_rejects_live_updates() -> None:
    hf = _hf_with_blocklist("live updates", "live blog")
    # Has "shooting" keyword but title starts with "Live Updates:"
    result = hf.evaluate(_item("Live Updates: Trump Shooting Investigation Continues"))
    assert result.passed is False
    assert "blocklist" in result.reason


def test_blocklist_rejects_opinion() -> None:
    hf = _hf_with_blocklist("opinion:", "analysis:")
    result = hf.evaluate(_item("Opinion: Why the War in Ukraine Matters"))
    assert result.passed is False


def test_blocklist_rejects_analysis_prefix() -> None:
    hf = _hf_with_blocklist("analysis:")
    result = hf.evaluate(_item("Analysis: What the election results mean"))
    assert result.passed is False


def test_blocklist_allows_genuine_event() -> None:
    hf = HardFilter(keywords=["ceasefire", "war"], max_age_seconds=300, blocklist=["live updates", "opinion:"])
    # Real event — no blocklist pattern in title, has keyword
    assert hf.passes(_item("US confirms ceasefire agreement with Iran")) is True


def test_blocklist_case_insensitive() -> None:
    hf = _hf_with_blocklist("live updates")
    assert hf.passes(_item("LIVE UPDATES: Shooting at Washington DC")) is False


def test_empty_blocklist_does_not_block() -> None:
    hf = HardFilter(keywords=["war"], max_age_seconds=300, blocklist=[])
    assert hf.passes(_item("Live Updates: war declared")) is True
