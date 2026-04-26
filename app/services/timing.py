"""Timing phase detector.

Splits the evolution of a news-driven market move into five phases:

=====  =============================================  ===========
Phase  Label                                          Action
=====  =============================================  ===========
1      Early leak / rumour (pre-news)                 ENTER (edge)
2      Breaking reaction (0..120 s)                   ENTER
3      Retail influx (2..MAX_NEWS_AGE_FOR_TRADE)      CAUTION
4      Overreaction (fast price, low new volume)      AVOID
5      Decay / mean reversion                         EXIT ZONE
=====  =============================================  ===========

We trade phases 1, 2 and 3 (CORE profile).  Phase 3's upper bound is
configurable via ``settings.max_news_age_for_trade`` so that raising
the freshness ceiling actually expands the tradeable window — before
this fix the fallback was hard-coded to 300 s, which silently capped
``MAX_NEWS_AGE_FOR_TRADE > 300``.

* ``news_age_s``    — seconds since headline publish time (``None`` if
                      we spotted the move but no headline matched yet →
                      rumour / leak).
* ``dvol_1m``       — 1-minute volume delta (shares).
* ``dvol_5m``       — 5-minute volume delta.
* ``avg_vol``       — short-window baseline (e.g. 24h mean rate).
* ``dprice_1m``     — 1-minute signed price delta in probability units.

The detector returns both an ``int`` phase id and a ``score`` in the
0..20 range used directly by the Timing pillar.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.config.settings import settings


@dataclass
class TimingFeatures:
    news_age_s: Optional[float]
    dvol_1m: float = 0.0
    dvol_5m: float = 0.0
    avg_vol_1m: float = 0.0
    dprice_1m: float = 0.0


@dataclass
class TimingDecision:
    phase: int  # 1..5
    score: float  # 0..20
    label: str
    reason: str


PHASE_SCORE = {
    1: 20.0,
    2: 16.0,
    3: 6.0,
    4: 0.0,
    5: 0.0,
}
PHASE_LABEL = {
    1: "leak/rumour",
    2: "breaking_reaction",
    3: "retail_influx",
    4: "overreaction",
    5: "decay",
}


def detect_phase(features: TimingFeatures) -> TimingDecision:
    """Pure function — deterministic mapping from features to a phase.

    The thresholds are intentionally simple and testable.  Tune via the
    weighted scorer, not by hacking this function.
    """
    age = features.news_age_s
    dvol_1m = features.dvol_1m
    dvol_5m = features.dvol_5m
    avg_vol = max(1.0, features.avg_vol_1m)  # avoid /0
    dprice_1m = features.dprice_1m

    # Phase 1: leak — we see a move BEFORE a matching headline.
    if age is None:
        if dvol_1m > 2.0 * avg_vol and abs(dprice_1m) > 0.01:
            return TimingDecision(1, PHASE_SCORE[1], PHASE_LABEL[1], "pre_headline_move")
        # No headline AND no move ⇒ decay / noise.
        return TimingDecision(5, PHASE_SCORE[5], PHASE_LABEL[5], "no_signal")

    # Phase 2: breaking reaction — headline is <= 120 s old.
    if age <= 120:
        return TimingDecision(2, PHASE_SCORE[2], PHASE_LABEL[2], "within_2_minutes")

    # Phase-3 ceiling tracks the freshness gate so raising it via env
    # actually widens the tradeable window.  Default 300 s preserves
    # legacy behaviour for callers that haven't tuned the env.
    phase3_ceiling = float(getattr(settings, "max_news_age_for_trade", 300) or 300)

    # Phase 3a: retail influx with confirmed volume surge.
    if age <= phase3_ceiling and dvol_5m > 1.5 * avg_vol:
        return TimingDecision(3, PHASE_SCORE[3], PHASE_LABEL[3], "retail_volume_surge")

    # Phase 3b (age fallback): within the freshness window AND no
    # real-time volume / price-delta data (orchestrator default — the
    # news pipeline does not compute per-market dvol/dprice).  Without
    # this fallback every headline >2 min old would collapse to phase
    # 5 and never trade.  When we DO have real-time data
    # (dvol/dprice non-zero), the phase 4/5 logic below still applies.
    if age <= phase3_ceiling and dvol_5m == 0.0 and dprice_1m == 0.0:
        return TimingDecision(3, PHASE_SCORE[3], PHASE_LABEL[3], "within_window_no_realtime")

    # Phase 4: overreaction — big price movement without matching new volume.
    if dvol_5m < 0.5 * avg_vol and abs(dprice_1m) > 0.03:
        return TimingDecision(4, PHASE_SCORE[4], PHASE_LABEL[4], "thin_volume_fast_price")

    # Default: phase 5 decay.
    return TimingDecision(5, PHASE_SCORE[5], PHASE_LABEL[5], "past_window")


def is_tradeable_phase(phase: int) -> bool:
    return phase in (1, 2)
