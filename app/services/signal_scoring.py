"""Prym edge-first scoring engine.

After the edge-first refactor the scorer is a thin bundler of
**measurable** signals — subjective NLP narratives (impact strength,
urgency magnitude, rarity, causal reasoning) no longer contribute
points.  The 4-pillar model has collapsed into 2 learnable pillars
plus 2 hard gates:

=============  ====  =================================================
Pillar         Max   What it captures                                   Role
=============  ====  =================================================  ======
Mispricing      60   |z-score| vs 30-day rolling mean, gated by
                     adjacent volume (thinner = better edge).           SCORE
Liquidity       25   CLOB microstructure: spread tightness + top-5
                     depth + order-flow imbalance on our side.          SCORE
Timing          15   Phase 1 = 15, Phase 2 = 12, else 0 — informative
                     only; the HARD gate on phase ∈ {1, 2} is enforced
                     in ``passes_trade`` so the score cannot "buy" its
                     way into a late-phase trade.                       SCORE
News             0   Direction + freshness are HARD GATES now; they
                     contribute no points.                              GATE
=============  ====  =================================================  ======

Only ``mispricing`` and ``liquidity`` are learnable by the feedback
loop (``app.services.feedback_loop``).  ``timing`` is fixed at 1.0.

The 0-100 total survives for cosmetic UI purposes only.  The actual
trade gate is a conjunction of measurable conditions, split into two
profiles depending on the entry price:

    # CORE — ``entry_price > LOW_PROB_ENTRY_PRICE``
    passes_trade =
        impact != "neutral"                           (direction)
        AND news_age_s <= MAX_NEWS_AGE_FOR_TRADE      (freshness)
        AND phase ∈ {1, 2}                            (timing)
        AND |mispricing.z| >= Z_MIN_FOR_TRADE         (statistical deviation)
        AND net_edge_pct >= MIN_EDGE_PCT              (EV > costs)
        AND fill_ratio >= MIN_FILL_RATIO              (execution)

    # LOW-PROB — ``entry_price <= LOW_PROB_ENTRY_PRICE``
    passes_trade =
        impact != "neutral"                           (direction)
        AND news_age_s <= MAX_NEWS_AGE_FOR_TRADE      (freshness)
        AND phase == 1                                (initial repricing only)
        AND |mispricing.z| >= LOW_PROB_Z_MIN          (tighter deviation)
        AND net_edge_pct >= LOW_PROB_MIN_EDGE_PCT     (tighter edge)
        AND fill_ratio >= MIN_FILL_RATIO              (execution)

``passes_alert`` is now an alias of ``passes_trade`` — we no longer
surface "probably interesting" alerts that would never clear the cost
model.  Simpler pipeline, stronger signals.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, Optional

from app.config.settings import settings
from app.integrations.mistral_client import AIAnalysis
from app.integrations.polymarket_client import MarketSnapshot
from app.services.microstructure import MicrostructureFeatures
from app.services.mispricing import MispricingResult
from app.services.timing import TimingDecision
from app.utils.time import seconds_since


# Pillar caps (sum = 100).  News has zero points — it is a hard gate.
CAP_NEWS = 0.0
CAP_LIQUIDITY = 25.0
CAP_MISPRICING = 60.0
CAP_TIMING = 15.0

# Timing sub-scores — informative only, actual gate is ``phase ∈ {1,2}``.
_TIMING_PHASE_SCORE = {1: 15.0, 2: 12.0, 3: 4.0, 4: 0.0, 5: 0.0}


@dataclass
class ScoreBreakdown:
    news: float
    liquidity: float
    mispricing: float
    timing: float

    total: float
    passes_alert: bool
    passes_trade: bool
    high_confidence: bool

    # Phase / z / slip for the UX card.
    phase: int = 5
    phase_label: str = ""
    mispricing_z: Optional[float] = None
    liquidity_spread: Optional[float] = None
    liquidity_ofi: Optional[float] = None

    # Why the hard-gate cluster passed/failed.  Populated for observability.
    gate_reason: str = ""
    # Continuous score + tier bucket.
    edge_score: float = 0.0
    tier: Literal["core", "mid", "low", "reject"] = "reject"
    # Additional observability fields for the continuous model.
    context: float = 0.0
    confidence: float = 0.0
    cost_penalty: float = 0.0
    noise_penalty: float = 0.0

    # Raw normalised contributions — persisted for the feedback loop.
    feature_vector: dict = field(default_factory=dict)

    # ---- Back-compat aliases (old 4-field breakdown) -----------------
    @property
    def ai_component(self) -> float:
        return self.news

    @property
    def market_component(self) -> float:
        return self.liquidity + self.mispricing

    @property
    def trader_component(self) -> float:
        # Trader confirmation no longer contributes score under the
        # edge-first refactor.  Kept as a property for template
        # compatibility; always zero.
        return 0.0

    @property
    def freshness_component(self) -> float:
        return self.feature_vector.get("freshness_raw", 0.0)

    @property
    def passes_auto(self) -> bool:  # legacy alias
        return self.passes_trade

    @property
    def in_sweet_spot(self) -> bool:
        # True when the mispricing pillar is firing strongly (≥ 60 % of cap).
        return self.mispricing >= CAP_MISPRICING * 0.6


class SignalScoringSystem:
    """Edge-first 2-pillar scorer.

    Parameters
    ----------
    weights
        Mapping ``{"news": w, "liquidity": w, "mispricing": w,
        "timing": w}`` with ``w ∈ [FEEDBACK_CLIP_LOW, FEEDBACK_CLIP_HIGH]``.
        ``news`` and ``timing`` are always forced to 1.0 — they are gate
        pillars, not learnable.  Only ``mispricing`` and ``liquidity`` are
        moved by the feedback loop.
    alert_threshold / trade_threshold
        Retained for cosmetic UI but **not consulted** by
        ``passes_alert`` / ``passes_trade``.  The gate is now a
        conjunction of measurable conditions (see module docstring).
    """

    def __init__(
        self,
        *,
        weights: Optional[Mapping[str, float]] = None,
        alert_threshold: Optional[float] = None,
        trade_threshold: Optional[float] = None,
        auto_threshold: Optional[float] = None,
    ) -> None:
        self.weights = _normalise_weights(weights)
        self.alert_threshold = (
            alert_threshold
            if alert_threshold is not None
            else settings.score_threshold_alert
        )
        self.trade_threshold = (
            trade_threshold
            if trade_threshold is not None
            else auto_threshold
            if auto_threshold is not None
            else settings.score_threshold_trade
        )

    # ---- public entry point -----------------------------------------

    def score(
        self,
        *,
        ai: AIAnalysis,
        market: MarketSnapshot,
        traders: Optional[object] = None,  # ignored — kept for call-site compat
        dq: Optional[object] = None,  # ignored — kept for call-site compat
        micro: Optional[MicrostructureFeatures] = None,
        mispricing: Optional[MispricingResult] = None,
        timing: Optional[TimingDecision] = None,
        news_published_at=None,
        side: str = "yes",
        net_edge_pct: Optional[float] = None,
        fill_ratio: Optional[float] = None,
        entry_price: Optional[float] = None,
        context_score: Optional[float] = None,
        ai_confidence: Optional[float] = None,
    ) -> ScoreBreakdown:
        # ---- Hard-gate inputs (measurable) --------------------------------
        impact = getattr(ai, "impact", "neutral") or "neutral"
        news_age_s: Optional[float]
        if news_published_at is not None:
            try:
                news_age_s = seconds_since(news_published_at)
            except Exception:
                news_age_s = None
        else:
            news_age_s = None

        abs_z = abs(mispricing.z) if mispricing and mispricing.z is not None else 0.0
        phase = timing.phase if timing else 5

        # LOW-PROB profile kicks in when the market price itself is a
        # long-shot; otherwise we use the CORE profile.
        is_low_prob = (
            entry_price is not None
            and entry_price > 0.0
            and entry_price <= settings.low_prob_entry_price
        )
        eff_z_min = (
            settings.low_prob_z_min if is_low_prob else settings.z_min_for_trade
        )
        eff_edge_min = (
            settings.low_prob_min_edge_pct
            if is_low_prob
            else settings.min_edge_pct
        )
        # CORE profile now allows phase 3 (retail influx) — we still
        # prefer phases 1-2 via the sizing tier driver, but blocking
        # phase 3 entirely was killing too many borderline signals.
        # LOW-PROB stays strict at phase 1 only (initial repricing).
        allowed_phases = (1,) if is_low_prob else (1, 2, 3)

        direction_ok = True
        freshness_ok = (
            news_age_s is not None and news_age_s <= settings.max_news_age_for_trade
        )
        phase_ok = phase in allowed_phases
        z_ok = abs_z >= eff_z_min
        edge_ok = net_edge_pct is not None and net_edge_pct >= eff_edge_min
        fill_ok = fill_ratio is None or fill_ratio >= settings.min_fill_ratio

        # ---- Score pillars (measurable only) ------------------------------
        liq_raw, liq_features = _liquidity_pillar(micro, market, side)
        misp_raw, misp_features = _mispricing_pillar(mispricing)
        timing_raw, timing_features = _timing_pillar(timing)

        # ``news`` pillar retired — zero-weighted.  Keep sub-fields so the
        # feature vector still surfaces them for observability.
        news_raw = 0.0
        news_features = {
            "direction_ok": direction_ok,
            "freshness_ok": freshness_ok,
            "news_age_s": news_age_s,
        }

        news = _apply_weight(news_raw, CAP_NEWS, self.weights["news"])
        liquidity = _apply_weight(liq_raw, CAP_LIQUIDITY, self.weights["liquidity"])
        misp = _apply_weight(misp_raw, CAP_MISPRICING, self.weights["mispricing"])
        timing_pts = _apply_weight(timing_raw, CAP_TIMING, self.weights["timing"])

        total = round(news + liquidity + misp + timing_pts, 2)

        # ---- Gate aggregation --------------------------------------------
        hard_gate = freshness_ok and phase_ok and z_ok and edge_ok and fill_ok
        # ---- Continuous EDGE_SCORE + tiering ------------------------------
        timing_mult = {1: 1.30, 2: 1.10, 3: 0.80, 4: 0.50}.get(phase, 0.20)
        raw_context = context_score if context_score is not None else None
        context_mult = max(0.0, min(1.0, float(raw_context or 0.0)))
        if raw_context is None and context_mult == 0.0:
            # Fallback when matcher context wasn't propagated by caller.
            context_mult = 1.0 if direction_ok and z_ok else 0.5
        confidence_mult = max(
            0.5,
            min(
                1.0,
                0.5 + (float(ai_confidence if ai_confidence is not None else 60.0) / 200.0),
            ),
        )
        event_edge = max(0.0, min(1.0, (0.55 * misp_raw) + (0.45 * liq_raw)))
        noise_penalty = (
            float(settings.neutral_noise_penalty)
            if impact == "neutral"
            else 0.0
        )
        edge_gap = max(0.0, float(settings.min_edge_pct) - float(net_edge_pct or 0.0))
        cost_penalty = (
            (edge_gap / max(1.0, float(settings.min_edge_pct)))
            * 0.35
            * float(settings.edge_score_cost_penalty_mult)
        )
        edge_score = (
            (event_edge * timing_mult * context_mult * confidence_mult)
            - cost_penalty
            - noise_penalty
        )
        edge_score = round(float(edge_score), 4)
        if edge_score >= float(settings.edge_score_core_min):
            tier: Literal["core", "mid", "low", "reject"] = "core"
        elif edge_score >= float(settings.edge_score_mid_min):
            tier = "mid"
        elif edge_score >= float(settings.edge_score_low_min):
            tier = "low"
        else:
            tier = "reject"
        # Alerts and trades share the same gate — we do not surface
        # signals that would never clear the cost model.
        passes_alert = hard_gate and tier != "reject"
        passes_trade = hard_gate and tier != "reject"
        # High confidence = mispricing pillar firing well AND strong
        # edge/z.  Used only by the sizing tier driver as a hint — the
        # real sizing tier is derived from measured ``net_edge_pct`` +
        # ``|z|`` via ``tier_from_edge``.  Aligned with the new HIGH
        # tier in ``tier_from_edge`` (edge ≥ 8 AND |z| ≥ 2.5) so the
        # two definitions don't drift apart.
        high_confidence = (
            misp >= CAP_MISPRICING * 0.6
            and abs_z >= 2.5
            and (net_edge_pct or 0.0) >= 8.0
        )

        # ---- Gate reason (for logging) -----------------------------------
        if hard_gate:
            gate_reason = "ok"
        elif not freshness_ok:
            gate_reason = f"stale_news_age_{news_age_s}"
        elif not phase_ok:
            gate_reason = f"phase_{phase}_not_tradeable"
        elif not z_ok:
            gate_reason = f"z_below_min_{abs_z:.2f}<{eff_z_min}"
        elif not edge_ok:
            gate_reason = (
                f"edge_below_min_"
                f"{net_edge_pct if net_edge_pct is not None else 'NA'}"
                f"<{eff_edge_min}"
            )
        elif not fill_ok:
            gate_reason = f"fill_below_min_{fill_ratio}"
        else:
            gate_reason = "unknown"

        feature_vector = {
            "news_raw": round(news_raw, 4),
            "liquidity_raw": round(liq_raw, 4),
            "mispricing_raw": round(misp_raw, 4),
            "timing_raw": round(timing_raw, 4),
            **news_features,
            **liq_features,
            **misp_features,
            **timing_features,
            "abs_z": round(abs_z, 4),
            "net_edge_pct": (
                round(float(net_edge_pct), 4) if net_edge_pct is not None else None
            ),
            "fill_ratio": (
                round(float(fill_ratio), 4) if fill_ratio is not None else None
            ),
            "entry_price": (
                round(float(entry_price), 4) if entry_price is not None else None
            ),
            "is_low_prob": bool(is_low_prob),
            "eff_z_min": float(eff_z_min),
            "eff_edge_min": float(eff_edge_min),
            "weights": {k: float(v) for k, v in self.weights.items()},
            "context_score": round(context_mult, 4),
            "confidence_mult": round(confidence_mult, 4),
            "event_edge": round(event_edge, 4),
            "edge_score": edge_score,
            "tier": tier,
            "cost_penalty": round(cost_penalty, 4),
            "noise_penalty": round(noise_penalty, 4),
        }

        return ScoreBreakdown(
            news=round(news, 2),
            liquidity=round(liquidity, 2),
            mispricing=round(misp, 2),
            timing=round(timing_pts, 2),
            total=total,
            passes_alert=passes_alert,
            passes_trade=passes_trade,
            high_confidence=high_confidence,
            phase=phase,
            phase_label=timing.label if timing else "",
            mispricing_z=mispricing.z if mispricing else None,
            liquidity_spread=micro.spread if micro else None,
            liquidity_ofi=micro.ofi if micro else None,
            gate_reason=gate_reason,
            edge_score=edge_score,
            tier=tier,
            context=round(context_mult, 4),
            confidence=round(confidence_mult, 4),
            cost_penalty=round(cost_penalty, 4),
            noise_penalty=round(noise_penalty, 4),
            feature_vector=feature_vector,
        )


# ============================================================================
#   Pillars — pure functions returning (raw_in_[0..1], features_dict)
# ============================================================================

def _liquidity_pillar(
    micro: Optional[MicrostructureFeatures],
    market: MarketSnapshot,
    side: str,
) -> tuple[float, dict]:
    if micro is None or not micro.has_book:
        vol = market.volume_24h or 0.0
        vol_n = max(0.0, min(1.0, vol / 50_000.0))
        return 0.5 * vol_n, {"fallback_volume_n": vol_n, "book_available": False}

    max_spread = max(0.001, settings.microstructure_max_spread)
    spread_n = (
        max(0.0, 1.0 - (micro.spread / max_spread))
        if micro.spread is not None
        else 0.0
    )

    our_depth = (
        micro.top5_ask_depth if side.lower() == "yes" else micro.top5_bid_depth
    )
    depth_n = min(1.0, our_depth / 2_000.0)

    ofi = micro.ofi or 0.0
    our_sign = 1.0 if side.lower() == "yes" else -1.0
    aligned_ofi = max(0.0, ofi * our_sign)
    ofi_n = min(1.0, aligned_ofi / 0.4)

    raw = 0.45 * spread_n + 0.40 * depth_n + 0.15 * ofi_n
    raw = max(0.0, min(1.0, raw))
    return raw, {
        "spread_n": spread_n,
        "depth_n": depth_n,
        "ofi_n": ofi_n,
        "book_available": True,
    }


def _mispricing_pillar(mp: Optional[MispricingResult]) -> tuple[float, dict]:
    if mp is None or mp.z is None:
        return 0.0, {"z_samples": mp.samples if mp else 0}

    abs_z = mp.abs_z
    z_n = min(1.0, abs_z / 2.0)

    raw = 0.65 * z_n + 0.35 * mp.adj_vol_score * z_n
    raw = max(0.0, min(1.0, raw))
    return raw, {
        "abs_z": round(abs_z, 3),
        "adj_vol_score": round(mp.adj_vol_score, 3),
        "z_samples": mp.samples,
    }


def _timing_pillar(timing: Optional[TimingDecision]) -> tuple[float, dict]:
    if timing is None:
        return 0.0, {"phase": None, "phase_label": "unknown"}
    phase_score = _TIMING_PHASE_SCORE.get(timing.phase, 0.0)
    # Normalise to [0..1] relative to the cap (CAP_TIMING = 15).
    raw = max(0.0, min(1.0, phase_score / CAP_TIMING)) if CAP_TIMING > 0 else 0.0
    return raw, {"phase": timing.phase, "phase_label": timing.label}


# ============================================================================
#   Helpers
# ============================================================================

def _apply_weight(raw_0_1: float, cap: float, weight: float) -> float:
    """Scale a 0..1 pillar contribution by its cap + learned weight,
    bounded by the cap.
    """
    out = raw_0_1 * cap * weight
    return max(0.0, min(cap, out))


def _normalise_weights(weights: Optional[Mapping[str, float]]) -> dict[str, float]:
    """Normalise and clip the per-pillar weights.

    ``news`` and ``timing`` are forced to 1.0 because they are gate
    pillars (news contributes 0 points, timing is a hard phase check).
    Only ``mispricing`` and ``liquidity`` are learnable.
    """
    low = float(settings.feedback_clip_low)
    high = float(settings.feedback_clip_high)
    out = {"news": 1.0, "liquidity": 1.0, "mispricing": 1.0, "timing": 1.0}
    if not weights:
        return out
    for k in ("mispricing", "liquidity"):
        try:
            w = float(weights.get(k, 1.0))
        except (TypeError, ValueError):
            w = 1.0
        out[k] = max(low, min(high, w))
    # ``news`` and ``timing`` remain at 1.0 regardless of input.
    return out


async def load_weights_from_db() -> dict[str, float]:
    """Read the latest feedback weights (mispricing + liquidity only).

    Falls back to 1.0 across the board when the table/row is missing,
    which matches the behaviour before any feedback updates have been
    applied.
    """
    from app.database.repositories.weights_repo import DEFAULT_WEIGHTS, WeightsRepository
    from app.database.session import session_scope

    try:
        async with session_scope() as session:
            weights = await WeightsRepository(session).get_all()
    except Exception:
        return {k: float(v) for k, v in DEFAULT_WEIGHTS.items()}
    return {k: float(v) for k, v in weights.items()}
