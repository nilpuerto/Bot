"""30-day realistic Monte-Carlo simulation of Prym Signals.

Design goals (per user spec):

1. **Full pipeline, no shortcuts.**  Every synthetic news item walks
   through the same stages the live bot does:

       hard filter -> AI analyse -> market match -> mispricing -> timing
       -> execution cost -> scorer -> strategy gate -> trade limiter

   and the rejection reason at every stage is counted so we can see
   exactly where the funnel collapses.

2. **Multi-category news stream.**  Politics / economy / breaking /
   sports / pure-noise each have their own hit rate on the hard
   filter, the market matcher, and their own urgency / impact
   distribution.  This is closer to the raw RSS mix than the old
   uniform synthetic stream.

3. **Edge-conditional price path (NOT pure random walk).**  The mid-
   price walk injects a directional drift whose sign + magnitude are
   conditional on the *quality of the gate inputs*:

       - phase 1 + high |z| + high net-edge -> tilted positive, so that
         if the system's gates really are picking mispricings, the
         exit ladder has a chance to fire.
       - weak gates -> approximately zero drift.
       - LOW-PROB entries use a bimodal lottery distribution: most
         paths decay (lose), a small minority spike to 10x-20x.

   This lets us evaluate whether the exit engine + the gates together
   produce positive EV *under realistic assumptions*, rather than the
   honest-zero-edge baseline of the older 15-day simulator.

4. **Realistic execution.**  Fill ratio, spread-based slippage and
   news->entry latency are all modelled, so the entry price differs
   from the quoted mid.

5. **Dual account.**  The same news stream is replayed against two
   balances (€400 and €20) to show how sizing / floors / caps shape
   behaviour on a small vs. medium account.

6. **Detailed output.**  Per-day summaries, a sample of the decision
   log (pass + reject reasons), every trade verbatim with realised
   ladder + max PnL + runner flag, and a final edge verdict.

Run it::

    python -m scripts.simulate_30d_realistic
    python -m scripts.simulate_30d_realistic --seed 7
    python -m scripts.simulate_30d_realistic --days 30 --sample 4
"""
from __future__ import annotations

import argparse
import math
import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from app.config.settings import settings
from app.database.models import CloseReason
from app.integrations.mistral_client import AIAnalysis
from app.integrations.polymarket_client import MarketSnapshot
from app.services.exit_strategy import (
    ExitActionKind,
    TradeExitView,
    empty_exit_state,
    evaluate_exit,
    record_partial,
)
from app.services.mispricing import MispricingResult
from app.services.signal_scoring import SignalScoringSystem
from app.services.timing import PHASE_LABEL, TimingDecision
from app.strategies.prym_strategy import PrymStrategy


# ===========================================================================
#   News universe
# ===========================================================================

NEWS_PER_DAY_MEAN = 350
NEWS_PER_DAY_STD = 40

# Shares sum to 1.0.  ``hard_filter_rate`` = probability that an item
# from this category contains one of the HARD_FILTER_KEYWORDS.
# ``match_rate`` = probability it matches a tradeable Polymarket market.
# These are calibrated to give roughly the pass-rates of the live
# pipeline: ~10% clear the hard filter, ~40% of those reach a match.
CATEGORIES: Dict[str, Dict[str, float]] = {
    "politics": {"share": 0.20, "hard_filter_rate": 0.22, "match_rate": 0.55},
    "economy":  {"share": 0.20, "hard_filter_rate": 0.18, "match_rate": 0.42},
    "breaking": {"share": 0.10, "hard_filter_rate": 0.55, "match_rate": 0.50},
    "sports":   {"share": 0.20, "hard_filter_rate": 0.06, "match_rate": 0.18},
    "noise":    {"share": 0.30, "hard_filter_rate": 0.02, "match_rate": 0.05},
}

IMPACT_DIST: Dict[str, Dict[str, float]] = {
    "politics": {"bullish": 0.42, "bearish": 0.38, "neutral": 0.20},
    "economy":  {"bullish": 0.40, "bearish": 0.35, "neutral": 0.25},
    "breaking": {"bullish": 0.45, "bearish": 0.40, "neutral": 0.15},
    "sports":   {"bullish": 0.35, "bearish": 0.30, "neutral": 0.35},
    "noise":    {"bullish": 0.20, "bearish": 0.15, "neutral": 0.65},
}

URGENCY_WEIGHTS: Dict[str, List[Tuple[int, float]]] = {
    "politics": [(5, 0.10), (6, 0.20), (7, 0.25), (8, 0.25), (9, 0.15), (10, 0.05)],
    "economy":  [(4, 0.10), (5, 0.20), (6, 0.25), (7, 0.25), (8, 0.15), (9, 0.05)],
    "breaking": [(7, 0.15), (8, 0.25), (9, 0.30), (10, 0.30)],
    "sports":   [(3, 0.25), (4, 0.25), (5, 0.25), (6, 0.15), (7, 0.10)],
    "noise":    [(1, 0.25), (2, 0.30), (3, 0.25), (4, 0.15), (5, 0.05)],
}

# Price distribution by category, in *YES token* price (so the
# underlying probability).  Politics and breaking tend to sit in the
# mid range; economy / sports are more often near-50/50; noise can be
# anywhere.
PRICE_DIST: Dict[str, Tuple[float, float]] = {
    "politics": (0.05, 0.55),
    "economy":  (0.15, 0.65),
    "breaking": (0.03, 0.40),
    "sports":   (0.15, 0.80),
    "noise":    (0.05, 0.85),
}

# |z| ~ half-normal - most signals are not mispriced; a tail of good
# setups.  Per-category means, so politics/breaking tend to surface
# sharper dislocations than noise.
Z_MEAN_BY_CATEGORY: Dict[str, float] = {
    "politics": 1.4,
    "economy":  1.2,
    "breaking": 1.8,
    "sports":   0.9,
    "noise":    0.5,
}
Z_STD = 1.0

PHASE_WEIGHTS: Dict[int, float] = {1: 0.18, 2: 0.32, 3: 0.25, 4: 0.15, 5: 0.10}

NET_EDGE_MEAN = 4.0
NET_EDGE_STD = 6.5
FILL_RATIO_MEAN = 0.82
FILL_RATIO_STD = 0.11

VOLUME_MIN, VOLUME_MAX = 800, 250_000

# Execution realism.
SPREAD_COST_PCT_RANGE = (0.2, 1.5)   # extra cost baked into entry price
LATENCY_SECONDS_RANGE = (5, 90)       # news publish -> entry open


# ===========================================================================
#   Edge-conditional price-path model
# ===========================================================================

PRICE_PATH_HOURLY_SIGMA = 0.045       # base log-return vol per hour
PRICE_PATH_STEP_SECONDS = 60
PRICE_PATH_MAX_HOURS_MULT = 2.0       # cap sim runtime at 2x time_exit


def _estimate_p_correct(
    *, phase: int, abs_z: float, net_edge: float, is_low_prob: bool
) -> float:
    """Model-side assumption of probability our side is "right".

    Calibrated so the simulation *conditionally* injects edge:
        * phase-1 + high-|z| + high-edge -> ~65-72 % (system is picking
          genuine repricings).
        * borderline gates (z~1.5, phase=2, edge~3%) -> ~52-56 %.
        * LOW-PROB setups -> intrinsically lottery-style (~18-25 %
          but asymmetric upside when they hit).

    These are model assumptions, NOT proof of edge.  The point of
    the simulation is to see whether, *given that assumption*, the
    exit + sizing machinery produces net EV > 0.
    """
    if is_low_prob:
        # Cheap-price lottery tickets.  Low hit rate but when they hit,
        # the path is engineered to drift up strongly -> 5x-20x runner
        # scenarios.
        p = 0.18 + 0.02 * max(0.0, abs_z - 2.5)
        p += 0.01 * min(net_edge / 4.0, 3.0)
        return max(0.12, min(0.32, p))

    p = 0.50
    p += 0.03 * min(abs_z, 3.0)
    p += 0.05 if phase == 1 else (0.02 if phase == 2 else 0.0)
    p += 0.01 * min(net_edge / 5.0, 3.0)
    return max(0.45, min(0.72, p))


def _drift_params(
    *, phase: int, net_edge: float, is_low_prob: bool, is_correct: bool
) -> Tuple[float, float]:
    """Return (hourly_drift, hourly_sigma) for this trade's path."""
    sigma_h = PRICE_PATH_HOURLY_SIGMA
    if is_low_prob:
        # Lottery: when correct, slow-burn upward; when wrong, fast decay.
        if is_correct:
            mu_h = 0.018 + 0.002 * min(net_edge, 20)
        else:
            mu_h = -0.012
        sigma_h = 0.06  # lotteries more volatile
        return mu_h, sigma_h

    base = 0.004 + 0.002 * min(net_edge, 15.0)
    phase_mult = 1.4 if phase == 1 else 1.0
    if is_correct:
        mu_h = base * phase_mult
    else:
        mu_h = -base * 0.8   # losing trades drift down slightly weaker
    return mu_h, sigma_h


# ===========================================================================
#   Synthetic news generator
# ===========================================================================


@dataclass
class NewsItem:
    day: int
    timestamp: datetime
    category: str
    urgency: int
    impact: str
    hard_filter_pass: bool


def _pick_category(rng: random.Random) -> str:
    r = rng.random()
    cum = 0.0
    for cat, cfg in CATEGORIES.items():
        cum += cfg["share"]
        if r <= cum:
            return cat
    return "noise"


def _pick_from_weights(rng: random.Random, weights: List[Tuple[int, float]]) -> int:
    r = rng.random()
    cum = 0.0
    for val, w in weights:
        cum += w
        if r <= cum:
            return val
    return weights[-1][0]


def _pick_impact(rng: random.Random, category: str) -> str:
    dist = IMPACT_DIST[category]
    r = rng.random()
    cum = 0.0
    for impact, w in dist.items():
        cum += w
        if r <= cum:
            return impact
    return "neutral"


def _sample_price(rng: random.Random, category: str) -> float:
    lo, hi = PRICE_DIST[category]
    return round(rng.uniform(lo, hi), 3)


def _sample_phase(rng: random.Random) -> int:
    r = rng.random()
    cum = 0.0
    for phase, w in PHASE_WEIGHTS.items():
        cum += w
        if r <= cum:
            return phase
    return 3


def _build_news(rng: random.Random, day: int, now: datetime) -> NewsItem:
    category = _pick_category(rng)
    cfg = CATEGORIES[category]
    urgency = _pick_from_weights(rng, URGENCY_WEIGHTS[category])
    impact = _pick_impact(rng, category)
    hard_filter_pass = rng.random() < cfg["hard_filter_rate"]
    return NewsItem(
        day=day,
        timestamp=now,
        category=category,
        urgency=urgency,
        impact=impact,
        hard_filter_pass=hard_filter_pass,
    )


# ===========================================================================
#   Trade bookkeeping
# ===========================================================================


@dataclass
class TradeRecord:
    day: int
    opened_at: datetime
    closed_at: datetime
    category: str
    profile: str                 # "core" | "low_prob"
    band: str
    side: str
    market_id: str
    entry_price: float
    quoted_price: float          # mid before slippage
    implied_prob: float
    amount_usd: float
    filled_ratio: float
    abs_z: float
    phase: int
    net_edge_pct: float
    p_correct_model: float
    is_correct_path: bool
    tiers_hit: List[float]
    partials: List[Dict]
    close_reason: str
    max_pnl_pct_seen: float
    min_pnl_pct_seen: float
    duration_minutes: float
    final_pnl_usd: float
    final_pnl_pct: float
    is_runner: bool              # max PnL >= 200%


@dataclass
class RejectionCounters:
    news_total: int = 0
    hard_filter_reject: int = 0
    neutral_impact_reject: int = 0
    market_no_match: int = 0
    phase_reject: int = 0
    z_reject: int = 0
    edge_reject: int = 0
    fill_reject: int = 0
    score_gate_other: int = 0
    strategy_reject: int = 0
    limiter_reject_cooldown: int = 0
    limiter_reject_daily: int = 0
    limiter_reject_reentry: int = 0
    limiter_reject_other: int = 0
    trades_opened: int = 0


@dataclass
class AccountState:
    label: str
    starting_balance: float
    balance: float
    peak_balance: float = 0.0
    trades: List[TradeRecord] = field(default_factory=list)
    daily_pnl: List[float] = field(default_factory=list)
    last_trade_at: Optional[datetime] = None
    last_close_by_market: Dict[str, datetime] = field(default_factory=dict)
    day_trades: int = 0
    rejects: RejectionCounters = field(default_factory=RejectionCounters)

    def __post_init__(self) -> None:
        self.peak_balance = self.starting_balance


# ===========================================================================
#   Price-path driven outcome
# ===========================================================================


def _simulate_outcome(
    *,
    entry_price: float,
    amount_usd: float,
    opened_at: datetime,
    phase: int,
    abs_z: float,
    net_edge: float,
    is_low_prob: bool,
    rng: random.Random,
) -> Dict:
    if entry_price <= 0 or amount_usd <= 0:
        return _empty_outcome(CloseReason.ERROR)

    shares = amount_usd / entry_price
    p_correct = _estimate_p_correct(
        phase=phase, abs_z=abs_z, net_edge=net_edge, is_low_prob=is_low_prob
    )
    is_correct = rng.random() < p_correct
    mu_h, sigma_h = _drift_params(
        phase=phase,
        net_edge=net_edge,
        is_low_prob=is_low_prob,
        is_correct=is_correct,
    )

    step_s = PRICE_PATH_STEP_SECONDS
    mu_step = mu_h * (step_s / 3600.0)
    sigma_step = sigma_h * math.sqrt(step_s / 3600.0)
    max_hours = settings.time_exit_hours * PRICE_PATH_MAX_HOURS_MULT
    total_steps = int((max_hours * 3600) / step_s)

    state = empty_exit_state()
    peak_price: Optional[float] = None
    trailing_active = False
    price = entry_price
    now = opened_at
    min_pnl_pct = 0.0
    partials_log: List[Dict] = []

    for step in range(1, total_steps + 1):
        now = opened_at + timedelta(seconds=step * step_s)
        log_return = rng.gauss(mu_step, sigma_step)
        price = max(0.001, min(0.999, price * math.exp(log_return)))
        pnl_pct_value = (price - entry_price) / entry_price * 100.0
        min_pnl_pct = min(min_pnl_pct, pnl_pct_value)

        view = TradeExitView(
            entry_price=entry_price,
            current_shares=shares,
            opened_at=opened_at,
            peak_price=peak_price,
            trailing_active=trailing_active,
            exit_state=state,
        )
        evaluation = evaluate_exit(
            view, price=price, pnl_pct_value=pnl_pct_value, now=now
        )
        state = evaluation.new_exit_state
        peak_price = evaluation.new_peak_price
        trailing_active = evaluation.new_trailing_active
        action = evaluation.action

        if action.kind is ExitActionKind.PARTIAL:
            assert (
                action.close_shares is not None
                and action.tier is not None
                and action.new_trailing_pct is not None
            )
            state = record_partial(
                state=state,
                tier=action.tier,
                close_shares=action.close_shares,
                close_price=price,
                entry_price=entry_price,
                at=now,
            )
            state["trailing_pct"] = float(action.new_trailing_pct)
            partials_log.append(
                {
                    "tier": action.tier,
                    "price": round(price, 4),
                    "shares": round(action.close_shares, 4),
                    "pnl_pct": round(pnl_pct_value, 2),
                    "at_minutes": round((now - opened_at).total_seconds() / 60.0, 1),
                }
            )
            shares = max(0.0, shares - action.close_shares)
            if shares <= 0:
                return _finalize(
                    state=state,
                    close_price=price,
                    entry_price=entry_price,
                    remaining_shares=0.0,
                    original_amount_usd=amount_usd,
                    reason=CloseReason.TAKE_PROFIT,
                    opened_at=opened_at,
                    closed_at=now,
                    min_pnl_pct=min_pnl_pct,
                    partials=partials_log,
                    p_correct=p_correct,
                    is_correct=is_correct,
                )
            continue

        if action.kind is ExitActionKind.CLOSE:
            assert action.close_reason is not None
            return _finalize(
                state=state,
                close_price=price,
                entry_price=entry_price,
                remaining_shares=shares,
                original_amount_usd=amount_usd,
                reason=action.close_reason,
                opened_at=opened_at,
                closed_at=now,
                min_pnl_pct=min_pnl_pct,
                partials=partials_log,
                p_correct=p_correct,
                is_correct=is_correct,
            )

    reason = CloseReason.TRAILING_STOP if trailing_active else CloseReason.TIME_EXIT
    return _finalize(
        state=state,
        close_price=price,
        entry_price=entry_price,
        remaining_shares=shares,
        original_amount_usd=amount_usd,
        reason=reason,
        opened_at=opened_at,
        closed_at=now,
        min_pnl_pct=min_pnl_pct,
        partials=partials_log,
        p_correct=p_correct,
        is_correct=is_correct,
    )


def _empty_outcome(reason: CloseReason) -> Dict:
    return {
        "pnl_usd": 0.0,
        "pnl_pct": 0.0,
        "tiers_hit": [],
        "partials": [],
        "close_reason": reason.value,
        "max_pnl_pct_seen": 0.0,
        "min_pnl_pct_seen": 0.0,
        "duration_minutes": 0.0,
        "opened_at": None,
        "closed_at": None,
        "p_correct": 0.0,
        "is_correct": False,
    }


def _finalize(
    *,
    state: dict,
    close_price: float,
    entry_price: float,
    remaining_shares: float,
    original_amount_usd: float,
    reason: CloseReason,
    opened_at: datetime,
    closed_at: datetime,
    min_pnl_pct: float,
    partials: List[Dict],
    p_correct: float,
    is_correct: bool,
) -> Dict:
    realized = float(state.get("realized_pnl_usd", 0.0))
    unrealized = (close_price - entry_price) * remaining_shares
    total_pnl = realized + unrealized
    pnl_pct_value = (
        (total_pnl / original_amount_usd) * 100.0 if original_amount_usd > 0 else 0.0
    )
    duration_min = (closed_at - opened_at).total_seconds() / 60.0
    return {
        "pnl_usd": round(total_pnl, 6),
        "pnl_pct": round(pnl_pct_value, 4),
        "tiers_hit": [float(t) for t in state.get("tiers_hit", [])],
        "partials": partials,
        "close_reason": reason.value,
        "max_pnl_pct_seen": float(state.get("max_pnl_pct_seen", 0.0)),
        "min_pnl_pct_seen": float(min_pnl_pct),
        "duration_minutes": round(duration_min, 1),
        "opened_at": opened_at,
        "closed_at": closed_at,
        "p_correct": p_correct,
        "is_correct": is_correct,
    }


# ===========================================================================
#   Per-news pipeline + per-account application
# ===========================================================================


def _build_ai(rng: random.Random, news: NewsItem) -> AIAnalysis:
    confidence = max(0, min(100, int(round(rng.gauss(65, 15)))))
    return AIAnalysis(
        market="synthetic",
        impact=news.impact,
        urgency=news.urgency,
        confidence=confidence,
    )


def _build_market(rng: random.Random, category: str) -> MarketSnapshot:
    price = _sample_price(rng, category)
    volume = rng.uniform(VOLUME_MIN, VOLUME_MAX)
    return MarketSnapshot(
        id=f"m-{category[:3]}-{rng.randint(1, 20_000)}",
        slug=f"{category}-sim",
        question=f"Synthetic {category} market",
        outcomes=["YES", "NO"],
        outcome_prices=[price, 1 - price],
        volume_24h=volume,
        liquidity=rng.uniform(500, 40_000),
        best_yes_price=price,
        best_no_price=1 - price,
    )


def _build_mispricing(
    rng: random.Random, category: str, price: float
) -> MispricingResult:
    abs_z = max(0.0, abs(rng.gauss(Z_MEAN_BY_CATEGORY[category], Z_STD)))
    sign = rng.choice([-1.0, 1.0])
    return MispricingResult(
        market_id="sim",
        z=sign * abs_z,
        mean=price,
        stddev=0.04 + rng.random() * 0.03,
        samples=150 + rng.randint(0, 100),
        adj_vol_score=max(0.0, min(1.0, rng.gauss(0.55, 0.2))),
        current_price=price,
    )


def _build_timing(rng: random.Random) -> TimingDecision:
    phase = _sample_phase(rng)
    phase_score = {1: 20.0, 2: 16.0, 3: 6.0, 4: 0.0, 5: 0.0}[phase]
    return TimingDecision(
        phase=phase, score=phase_score, label=PHASE_LABEL[phase], reason="synthetic"
    )


def _apply_execution_costs(
    rng: random.Random, quoted_price: float
) -> Tuple[float, float]:
    """Return (effective_entry_price, spread_cost_pct)."""
    spread_cost_pct = rng.uniform(*SPREAD_COST_PCT_RANGE)
    entry = quoted_price * (1.0 + spread_cost_pct / 100.0)
    entry = min(0.999, max(0.001, entry))
    return round(entry, 4), spread_cost_pct


def _sample_net_edge(rng: random.Random, abs_z: float, phase: int) -> float:
    mean = NET_EDGE_MEAN + (1.0 if phase == 1 else 0.0) + min(abs_z, 3.0) * 0.4
    return rng.gauss(mean, NET_EDGE_STD)


def _sample_fill_ratio(rng: random.Random) -> float:
    return max(0.0, min(1.0, rng.gauss(FILL_RATIO_MEAN, FILL_RATIO_STD)))


# ---------------------------------------------------------------------------
#   Per-account bookkeeping replicates TradeLimiter rules.
# ---------------------------------------------------------------------------


def _limiter_check(
    acct: AccountState, market_id: str, now: datetime
) -> Tuple[bool, str]:
    if acct.day_trades >= settings.max_trades_per_day:
        return False, "daily_limit"
    if acct.last_trade_at is not None:
        elapsed = (now - acct.last_trade_at).total_seconds()
        if elapsed < settings.trade_cooldown_seconds:
            return False, "cooldown"
    last_close = acct.last_close_by_market.get(market_id)
    if last_close is not None and settings.post_close_reentry_seconds > 0:
        elapsed = (now - last_close).total_seconds()
        if elapsed < settings.post_close_reentry_seconds:
            return False, "reentry"
    return True, "ok"


# ---------------------------------------------------------------------------
#   Log-helpers.  We emit a compact, greppable decision trace so the
#   user can verify EVERY stage of the pipeline on a sample of events.
# ---------------------------------------------------------------------------


def _fmt_time(t: datetime) -> str:
    return t.strftime("%H:%M:%S")


def _log_news_decision(
    *,
    enabled: bool,
    account: str,
    news: NewsItem,
    stage: str,
    detail: str,
) -> None:
    if not enabled:
        return
    print(
        f"      [{_fmt_time(news.timestamp)}] {account:<6s} | {news.category:<8s} "
        f"| urg={news.urgency:<2d} | {news.impact:<7s} | {stage:<18s} | {detail}"
    )


# ===========================================================================
#   The simulation
# ===========================================================================


def run_simulation(
    *,
    days: int = 30,
    seed: Optional[int] = None,
    verbose_sample_per_day: int = 4,
) -> Tuple[AccountState, AccountState, List[Dict]]:
    """Replay one stream of synthetic news against two accounts."""
    rng = random.Random(seed)
    strategy = PrymStrategy()
    scorer = SignalScoringSystem()

    acct_a = AccountState(label="A(400)", starting_balance=400.0, balance=400.0)
    acct_b = AccountState(label="B(20)", starting_balance=20.0, balance=20.0)

    daily_summaries: List[Dict] = []
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    for day_idx in range(1, days + 1):
        day_start = now + timedelta(days=day_idx - 1)
        acct_a.day_trades = 0
        acct_b.day_trades = 0
        acct_a_day_pnl = 0.0
        acct_b_day_pnl = 0.0

        news_today = max(50, int(rng.gauss(NEWS_PER_DAY_MEAN, NEWS_PER_DAY_STD)))
        tick_seconds = max(5, 86400 // news_today)
        sim_clock = day_start

        sample_budget = verbose_sample_per_day  # per-day decision-log samples
        header_printed = False

        for _ in range(news_today):
            sim_clock += timedelta(seconds=tick_seconds)
            news = _build_news(rng, day_idx, sim_clock)

            acct_a.rejects.news_total += 1
            acct_b.rejects.news_total += 1

            if not news.hard_filter_pass:
                acct_a.rejects.hard_filter_reject += 1
                acct_b.rejects.hard_filter_reject += 1
                continue

            if news.impact == "neutral":
                acct_a.rejects.neutral_impact_reject += 1
                acct_b.rejects.neutral_impact_reject += 1
                continue

            # Market match
            if rng.random() >= CATEGORIES[news.category]["match_rate"]:
                acct_a.rejects.market_no_match += 1
                acct_b.rejects.market_no_match += 1
                continue

            ai = _build_ai(rng, news)
            market = _build_market(rng, news.category)
            mispricing = _build_mispricing(rng, news.category, market.best_yes_price or 0.5)
            timing = _build_timing(rng)
            side = "yes" if ai.impact == "bullish" else "no"
            quoted = market.best_yes_price if side == "yes" else market.best_no_price
            quoted = quoted or 0.5
            entry_price, spread_cost_pct = _apply_execution_costs(rng, quoted)
            latency_s = rng.randint(*LATENCY_SECONDS_RANGE)
            # The scorer computes ``news_age_s`` against wall clock at
            # call time.  To keep the sim independent of when we run it,
            # anchor ``news_published_at`` to ``datetime.now(UTC)`` so the
            # age is exactly the injected ``latency_s``.
            news_time = datetime.now(timezone.utc) - timedelta(seconds=latency_s)

            abs_z = abs(mispricing.z or 0.0)
            net_edge_pct = _sample_net_edge(rng, abs_z, timing.phase)
            fill_ratio = _sample_fill_ratio(rng)

            breakdown = scorer.score(
                ai=ai,
                market=market,
                mispricing=mispricing,
                timing=timing,
                news_published_at=news_time,
                side=side,
                net_edge_pct=net_edge_pct,
                fill_ratio=fill_ratio,
                entry_price=entry_price,
            )

            # The scorer already collapsed CORE vs LOW-PROB per our
            # gate-profile logic.  Surface the profile so we can log +
            # count it per account.
            is_low_prob = bool(breakdown.feature_vector.get("is_low_prob"))
            profile = "low_prob" if is_low_prob else "core"
            gate_reason = breakdown.gate_reason

            sample_verbose = sample_budget > 0
            if not header_printed and sample_verbose:
                print(
                    f"\n  DAY {day_idx:2d}  -  sample decision log "
                    f"(first {verbose_sample_per_day} relevant events):"
                )
                header_printed = True

            if not breakdown.passes_trade:
                # Attribute the rejection across all stages so the
                # funnel adds up on each account.
                stage_reason = gate_reason.split("_", 2)[0]
                if "phase" in gate_reason:
                    acct_a.rejects.phase_reject += 1
                    acct_b.rejects.phase_reject += 1
                elif "z_below" in gate_reason:
                    acct_a.rejects.z_reject += 1
                    acct_b.rejects.z_reject += 1
                elif "edge_below" in gate_reason:
                    acct_a.rejects.edge_reject += 1
                    acct_b.rejects.edge_reject += 1
                elif "fill_below" in gate_reason:
                    acct_a.rejects.fill_reject += 1
                    acct_b.rejects.fill_reject += 1
                else:
                    acct_a.rejects.score_gate_other += 1
                    acct_b.rejects.score_gate_other += 1
                if sample_budget > 0:
                    _log_news_decision(
                        enabled=True,
                        account="BOTH",
                        news=news,
                        stage="REJECT",
                        detail=(
                            f"{gate_reason}  "
                            f"(z={abs_z:.2f} ph={timing.phase} edge={net_edge_pct:.2f}% "
                            f"fill={fill_ratio:.2f} price={entry_price:.3f} profile={profile})"
                        ),
                    )
                    sample_budget -= 1
                continue

            decision = strategy.evaluate(ai=ai, market=market, score=breakdown)
            if not decision.should_enter:
                acct_a.rejects.strategy_reject += 1
                acct_b.rejects.strategy_reject += 1
                if sample_budget > 0:
                    _log_news_decision(
                        enabled=True,
                        account="BOTH",
                        news=news,
                        stage="STRATEGY_REJECT",
                        detail=decision.reason,
                    )
                    sample_budget -= 1
                continue

            # A trade candidate - apply per-account sizing + limiter.
            for acct in (acct_a, acct_b):
                _maybe_enter(
                    acct=acct,
                    rng=rng,
                    strategy=strategy,
                    breakdown=breakdown,
                    market=market,
                    news=news,
                    sim_clock=sim_clock,
                    side=side,
                    entry_price=entry_price,
                    quoted=quoted,
                    abs_z=abs_z,
                    fill_ratio=fill_ratio,
                    net_edge_pct=net_edge_pct,
                    timing=timing,
                    is_low_prob=is_low_prob,
                    profile=profile,
                    sample_verbose_left=sample_budget,
                )

            if sample_budget > 0:
                # We emitted the candidate into the detailed log inside
                # _maybe_enter; shrink the remaining budget.
                sample_budget -= 1

        # End of day.
        acct_a_day_pnl = sum(
            t.final_pnl_usd for t in acct_a.trades if t.closed_at.date() == sim_clock.date()
        )
        acct_b_day_pnl = sum(
            t.final_pnl_usd for t in acct_b.trades if t.closed_at.date() == sim_clock.date()
        )
        acct_a.peak_balance = max(acct_a.peak_balance, acct_a.balance)
        acct_b.peak_balance = max(acct_b.peak_balance, acct_b.balance)
        daily_summaries.append(
            {
                "day": day_idx,
                "news": news_today,
                "a_trades": sum(1 for t in acct_a.trades if t.day == day_idx),
                "a_pnl": acct_a_day_pnl,
                "a_balance": acct_a.balance,
                "b_trades": sum(1 for t in acct_b.trades if t.day == day_idx),
                "b_pnl": acct_b_day_pnl,
                "b_balance": acct_b.balance,
            }
        )

    return acct_a, acct_b, daily_summaries


def _maybe_enter(
    *,
    acct: AccountState,
    rng: random.Random,
    strategy: PrymStrategy,
    breakdown,
    market: MarketSnapshot,
    news: NewsItem,
    sim_clock: datetime,
    side: str,
    entry_price: float,
    quoted: float,
    abs_z: float,
    fill_ratio: float,
    net_edge_pct: float,
    timing: TimingDecision,
    is_low_prob: bool,
    profile: str,
    sample_verbose_left: int,
) -> None:
    allowed, reason = _limiter_check(acct, market.id, sim_clock)
    if not allowed:
        if reason == "daily_limit":
            acct.rejects.limiter_reject_daily += 1
        elif reason == "cooldown":
            acct.rejects.limiter_reject_cooldown += 1
        elif reason == "reentry":
            acct.rejects.limiter_reject_reentry += 1
        else:
            acct.rejects.limiter_reject_other += 1
        if sample_verbose_left > 0:
            _log_news_decision(
                enabled=True,
                account=acct.label,
                news=news,
                stage="LIMITER_REJECT",
                detail=reason,
            )
        return

    plan = strategy.sizing(
        balance=acct.balance,
        risk_pct=settings.default_risk_pct,
        entry_price=entry_price,
        high_confidence=breakdown.high_confidence,
        stop_loss_enabled=True,
        net_edge_pct=net_edge_pct,
        abs_z=abs_z,
    )

    # Execution realism: ``plan.amount_usd`` is the intended notional.
    # Fill ratio reduces what we actually get on the book.  A trade is
    # only rejected outright if the *intended* size already fails the
    # MIN_TRADE_USD guard or exceeds the balance; partial fills below
    # MIN are still real executions (Polymarket accepts sub-dollar
    # fills), down to a dust floor of $0.30.
    if plan.amount_usd < settings.min_trade_usd or plan.amount_usd > acct.balance:
        acct.rejects.limiter_reject_other += 1
        if sample_verbose_left > 0:
            _log_news_decision(
                enabled=True,
                account=acct.label,
                news=news,
                stage="SIZING_REJECT",
                detail=(
                    f"intended notional {plan.amount_usd:.2f} below min or above balance"
                ),
            )
        return
    actual_amount = round(plan.amount_usd * fill_ratio, 2)
    if actual_amount < 0.30:
        acct.rejects.limiter_reject_other += 1
        if sample_verbose_left > 0:
            _log_news_decision(
                enabled=True,
                account=acct.label,
                news=news,
                stage="SIZING_REJECT",
                detail=(
                    f"fill {fill_ratio:.2f} drove notional to {actual_amount:.2f} (dust)"
                ),
            )
        return

    outcome = _simulate_outcome(
        entry_price=entry_price,
        amount_usd=actual_amount,
        opened_at=sim_clock,
        phase=timing.phase,
        abs_z=abs_z,
        net_edge=net_edge_pct,
        is_low_prob=is_low_prob,
        rng=rng,
    )

    pnl = outcome["pnl_usd"]
    acct.balance += pnl
    acct.day_trades += 1
    acct.last_trade_at = sim_clock
    acct.rejects.trades_opened += 1
    if outcome.get("closed_at") is not None:
        acct.last_close_by_market[market.id] = outcome["closed_at"]
    acct.peak_balance = max(acct.peak_balance, acct.balance)

    trade = TradeRecord(
        day=news.day,
        opened_at=sim_clock,
        closed_at=outcome["closed_at"] or sim_clock,
        category=news.category,
        profile=profile,
        band=str(getattr(plan, "band", "mid")),
        side=side,
        market_id=market.id,
        entry_price=entry_price,
        quoted_price=quoted,
        implied_prob=quoted,
        amount_usd=actual_amount,
        filled_ratio=fill_ratio,
        abs_z=abs_z,
        phase=timing.phase,
        net_edge_pct=net_edge_pct,
        p_correct_model=outcome.get("p_correct", 0.0),
        is_correct_path=outcome.get("is_correct", False),
        tiers_hit=outcome["tiers_hit"],
        partials=outcome["partials"],
        close_reason=outcome["close_reason"],
        max_pnl_pct_seen=outcome["max_pnl_pct_seen"],
        min_pnl_pct_seen=outcome["min_pnl_pct_seen"],
        duration_minutes=outcome["duration_minutes"],
        final_pnl_usd=pnl,
        final_pnl_pct=outcome["pnl_pct"],
        is_runner=outcome["max_pnl_pct_seen"] >= 200.0,
    )
    acct.trades.append(trade)

    if sample_verbose_left > 0:
        runner_flag = " RUNNER" if trade.is_runner else ""
        _log_news_decision(
            enabled=True,
            account=acct.label,
            news=news,
            stage="TRADE_OPEN",
            detail=(
                f"{profile.upper():<8s} ${actual_amount:5.2f} @ {entry_price:.3f} "
                f"(band={trade.band}, z={abs_z:.2f}, ph={timing.phase}, edge={net_edge_pct:.2f}%, "
                f"p_correct={trade.p_correct_model:.2f}){runner_flag}"
            ),
        )


# ===========================================================================
#   Reporting
# ===========================================================================


def _print_daily_table(summaries: List[Dict]) -> None:
    print("\n" + "=" * 78)
    print("  DAILY ROLL-UP (both accounts replay the SAME news stream)")
    print("=" * 78)
    print(
        f"  {'day':>3} | {'news':>4} | "
        f"{'A trd':>5} | {'A PnL $':>8} | {'A bal':>8} | "
        f"{'B trd':>5} | {'B PnL $':>8} | {'B bal':>8}"
    )
    print("  " + "-" * 74)
    for s in summaries:
        print(
            f"  {s['day']:>3} | {s['news']:>4} | "
            f"{s['a_trades']:>5} | {s['a_pnl']:>+8.2f} | {s['a_balance']:>8.2f} | "
            f"{s['b_trades']:>5} | {s['b_pnl']:>+8.2f} | {s['b_balance']:>8.2f}"
        )


def _print_funnel(acct: AccountState) -> None:
    r = acct.rejects
    scored = (
        r.news_total
        - r.hard_filter_reject
        - r.neutral_impact_reject
        - r.market_no_match
    )
    passed_score = r.trades_opened + r.strategy_reject + (
        r.limiter_reject_cooldown
        + r.limiter_reject_daily
        + r.limiter_reject_reentry
        + r.limiter_reject_other
    )
    print(f"\n  FUNNEL - account {acct.label}")
    print(f"    news seen                     : {r.news_total:>6}")
    print(f"    -> hard-filter reject          : {r.hard_filter_reject:>6}")
    print(f"    -> neutral impact reject       : {r.neutral_impact_reject:>6}")
    print(f"    -> market no-match reject      : {r.market_no_match:>6}")
    print(f"    = scored by scorer            : {scored:>6}")
    print(f"      - phase reject              : {r.phase_reject:>6}")
    print(f"      - |z| reject                : {r.z_reject:>6}")
    print(f"      - edge reject               : {r.edge_reject:>6}")
    print(f"      - fill reject               : {r.fill_reject:>6}")
    print(f"      - score gate other          : {r.score_gate_other:>6}")
    print(f"    = passed score gate           : {passed_score:>6}")
    print(f"      - strategy veto             : {r.strategy_reject:>6}")
    print(f"      - limiter: daily cap        : {r.limiter_reject_daily:>6}")
    print(f"      - limiter: cooldown         : {r.limiter_reject_cooldown:>6}")
    print(f"      - limiter: reentry          : {r.limiter_reject_reentry:>6}")
    print(f"      - limiter: other            : {r.limiter_reject_other:>6}")
    print(f"    = TRADES OPENED               : {r.trades_opened:>6}")


def _trade_table(acct: AccountState) -> None:
    trades = acct.trades
    if not trades:
        print(f"\n  {acct.label}: no trades.")
        return

    print(f"\n  TRADES - account {acct.label}  ({len(trades)} trades)")
    print(
        "  " + "-" * 108
        + "\n  "
        + f"{'d':>3} {'cat':<8s} {'prof':<8s} {'bnd':<4s} "
        f"{'side':<4s} {'entry':>6s} {'size$':>6s} "
        f"{'z':>4s} {'ph':>2s} {'edge%':>6s} "
        f"{'pnl$':>7s} {'pnl%':>7s} {'max%':>6s} {'dur_m':>6s} {'reason':<12s} tiers"
    )
    for t in trades:
        tier_str = (
            "+" + "/+".join(f"{int(x)}%" for x in t.tiers_hit) if t.tiers_hit else "-"
        )
        print(
            "  "
            f"{t.day:>3} {t.category:<8s} {t.profile:<8s} {t.band:<4s} "
            f"{t.side:<4s} {t.entry_price:>6.3f} {t.amount_usd:>6.2f} "
            f"{t.abs_z:>4.2f} {t.phase:>2d} {t.net_edge_pct:>6.2f} "
            f"{t.final_pnl_usd:>+7.2f} {t.final_pnl_pct:>+7.2f} "
            f"{t.max_pnl_pct_seen:>+6.1f} {t.duration_minutes:>6.0f} "
            f"{t.close_reason:<12s} {tier_str}"
        )


def _overall_stats(acct: AccountState) -> None:
    trades = acct.trades
    print(f"\n  STATISTICS - account {acct.label}")
    print(f"    trades opened      : {len(trades)}")
    if not trades:
        return

    wins = [t for t in trades if t.final_pnl_usd > 0]
    losses = [t for t in trades if t.final_pnl_usd <= 0]
    winrate = 100.0 * len(wins) / len(trades)
    total_pnl = sum(t.final_pnl_usd for t in trades)
    print(f"    winners / losers   : {len(wins)}W / {len(losses)}L ({winrate:.1f}%)")
    print(
        f"    total PnL          : {total_pnl:+7.2f}$  "
        f"({100.0 * total_pnl / acct.starting_balance:+.2f}% of start)"
    )
    print(f"    final balance      : ${acct.balance:,.2f}")
    if wins:
        avg_win = statistics.mean(t.final_pnl_usd for t in wins)
        best = max(t.final_pnl_usd for t in wins)
        print(f"    avg win / best     : {avg_win:+.2f}$ / {best:+.2f}$")
    if losses:
        avg_loss = statistics.mean(t.final_pnl_usd for t in losses)
        worst = min(t.final_pnl_usd for t in losses)
        print(f"    avg loss / worst   : {avg_loss:+.2f}$ / {worst:+.2f}$")

    # Drawdown from peak on the balance curve.
    peak = acct.starting_balance
    worst_dd = 0.0
    running = acct.starting_balance
    for t in sorted(trades, key=lambda t: t.closed_at):
        running += t.final_pnl_usd
        peak = max(peak, running)
        dd = (running - peak) / peak * 100 if peak > 0 else 0.0
        worst_dd = min(worst_dd, dd)
    print(f"    max drawdown       : {worst_dd:.2f}%")

    # Profile split.
    core = [t for t in trades if t.profile == "core"]
    low = [t for t in trades if t.profile == "low_prob"]
    for label, bucket in (("CORE", core), ("LOW-PROB", low)):
        if not bucket:
            continue
        wr = 100.0 * sum(1 for t in bucket if t.final_pnl_usd > 0) / len(bucket)
        pnl = sum(t.final_pnl_usd for t in bucket)
        print(
            f"    {label:<10s}      : {len(bucket):>3d} trades, "
            f"WR {wr:5.1f}%, PnL {pnl:+7.2f}$"
        )

    # Tier hit distribution.
    tier_counts: Dict[float, int] = {}
    for t in trades:
        for tier in t.tiers_hit:
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
    if tier_counts:
        print("    tier hits          :")
        for tier in sorted(tier_counts):
            share = 100.0 * tier_counts[tier] / len(trades)
            print(f"      +{tier:>5.0f}%           : {tier_counts[tier]:>3d} ({share:5.1f}%)")

    # Close-reason mix.
    reasons: Dict[str, int] = {}
    for t in trades:
        reasons[t.close_reason] = reasons.get(t.close_reason, 0) + 1
    print("    close reasons      :")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        share = 100.0 * count / len(trades)
        print(f"      {reason:<13s}    : {count:>3d} ({share:5.1f}%)")

    # Runners + payoff distribution.
    runners = [t for t in trades if t.is_runner]
    print(f"    runners (max>=200%) : {len(runners)} ({100.0*len(runners)/len(trades):.1f}%)")
    buckets = [-100, -50, -20, 0, 20, 50, 100, 200, 500, 1000, 10_000]
    labels = [
        "<-50%", "-50..-20%", "-20..0%", "0..+20%", "+20..+50%",
        "+50..+100%", "+100..+200%", "+200..+500%", "+500..+1000%", "+1000%+",
    ]
    bucket_counts = [0] * (len(buckets) - 1)
    for t in trades:
        pct = t.final_pnl_pct
        for i in range(len(buckets) - 1):
            if buckets[i] <= pct < buckets[i + 1]:
                bucket_counts[i] += 1
                break
    print("    PnL% distribution  :")
    for lbl, count in zip(labels, bucket_counts):
        if count == 0:
            continue
        share = 100.0 * count / len(trades)
        print(f"      {lbl:<12s}    : {count:>3d} ({share:5.1f}%)")


def _final_verdict(acct_a: AccountState, acct_b: AccountState) -> None:
    print("\n" + "=" * 78)
    print("  EDGE VERDICT - does the system make money under these assumptions?")
    print("=" * 78)

    def _summary(acct: AccountState) -> None:
        trades = acct.trades
        if not trades:
            print(f"  {acct.label}: no trades - cannot judge EV.")
            return
        roi_pct = 100.0 * (acct.balance - acct.starting_balance) / acct.starting_balance
        wins = sum(1 for t in trades if t.final_pnl_usd > 0)
        winrate = 100.0 * wins / len(trades)
        pnls = [t.final_pnl_usd for t in trades]
        ev_per_trade = statistics.mean(pnls)
        median_trade = statistics.median(pnls)
        sample_stdev = statistics.stdev(pnls) if len(pnls) > 1 else 0.0
        # t-statistic on mean trade PnL - signal vs noise.
        t_stat = (
            (ev_per_trade / (sample_stdev / math.sqrt(len(trades))))
            if sample_stdev > 0
            else 0.0
        )
        runners = sum(1 for t in trades if t.is_runner)
        print(
            f"\n  {acct.label}:"
            f"\n    trades               : {len(trades)}"
            f"\n    winrate              : {winrate:5.1f}%"
            f"\n    EV per trade         : {ev_per_trade:+.3f}$"
            f" (median {median_trade:+.3f}$)"
            f"\n    stdev trade PnL      : {sample_stdev:.3f}$"
            f"\n    t-stat of mean > 0   : {t_stat:+.2f}  "
            f"(>2 -> likely positive EV on this sample)"
            f"\n    runners (max>=200%)   : {runners}"
            f"\n    ROI over period      : {roi_pct:+.2f}%"
            f"\n    verdict              : {_verdict_line(roi_pct, t_stat, runners)}"
        )

    _summary(acct_a)
    _summary(acct_b)


def _verdict_line(roi_pct: float, t_stat: float, runners: int) -> str:
    if roi_pct > 0 and t_stat > 2.0:
        return "positive EV with statistical support"
    if roi_pct > 0 and t_stat > 1.0:
        return "positive PnL but sample too small to confirm edge"
    if roi_pct > 0:
        return "small positive PnL driven by few outliers - inconclusive"
    if roi_pct > -5.0:
        return "roughly break-even - needs more trades or tighter gates"
    return "negative PnL - re-evaluate gates or exit parameters"


# ===========================================================================
#   CLI
# ===========================================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Realistic 30-day Prym Signals simulation with per-stage pipeline logging"
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--sample",
        type=int,
        default=4,
        help="Per-day decision-log samples to print (default 4)",
    )
    parser.add_argument(
        "--no-trade-log",
        action="store_true",
        help="Skip the per-trade table (useful for very long runs)",
    )
    args = parser.parse_args()

    bar = "=" * 78
    print(bar)
    print("  PRYM SIGNALS - 30-DAY REALISTIC SIMULATION")
    print(bar)
    print(f"  Days                 : {args.days}")
    print(f"  Seed                 : {args.seed if args.seed is not None else 'random'}")
    print(
        f"  CORE gates           : z >= {settings.z_min_for_trade}, "
        f"edge >= {settings.min_edge_pct}%, phase in {{1,2}}"
    )
    print(
        f"  LOW-PROB gates       : price <= {settings.low_prob_entry_price}, "
        f"z >= {settings.low_prob_z_min}, edge >= {settings.low_prob_min_edge_pct}%, phase = 1"
    )
    print(
        f"  Sizing bands (%bal)  : low_prob={settings.band_low_prob_pct} "
        f"low={settings.band_low_pct} mid={settings.band_mid_pct} "
        f"high={settings.band_high_pct}"
    )
    print(
        f"  Exit rules           : SL -{settings.hard_sl_pct}% | time exit "
        f"{settings.time_exit_hours}h | ladder "
        + ", ".join(
            f"+{t.pnl_threshold_pct:.0f}%:{t.close_fraction_pct:.0f}%:{t.new_trailing_pct:.0f}%tr"
            for t in settings.partial_tp_tiers
        )
    )
    print(
        f"  Re-entry lockout     : {settings.post_close_reentry_seconds}s   |   "
        f"max trades/day: {settings.max_trades_per_day}"
    )
    print(bar)

    acct_a, acct_b, summaries = run_simulation(
        days=args.days, seed=args.seed, verbose_sample_per_day=args.sample
    )

    _print_daily_table(summaries)
    _print_funnel(acct_a)
    _print_funnel(acct_b)

    if not args.no_trade_log:
        _trade_table(acct_a)
        _trade_table(acct_b)

    _overall_stats(acct_a)
    _overall_stats(acct_b)
    _final_verdict(acct_a, acct_b)


if __name__ == "__main__":
    main()
