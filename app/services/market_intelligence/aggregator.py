"""Intelligence aggregator — combines the 3 contexts into 2 scalars.

Outputs
-------
* ``market_context_score`` (0..100) — cosmetic health metric, surfaced
  in the feature vector and in future /info embellishments.
* ``edge_adjustment_score`` (± ``max_adjustment``) — the *only* value
  that touches the trading math.  It is added to ``net_edge_pct``
  **before** the scorer's hard gates run.

Invariants
~~~~~~~~~~
* When ``MARKET_INTELLIGENCE_ENABLED=false`` the orchestrator must call
  :func:`neutral_report` so nothing changes downstream.
* ``edge_adjustment_score`` is clipped to ``±max_adjustment`` and
  ``max_adjustment`` defaults to ``settings.mi_max_edge_adjustment_pct``
  (= 2.0 pp out of the box).  Even with the three modules maxed out
  it can never add more than this to ``net_edge_pct``.
* Missing/neutral sub-contexts collapse to a 0.5 "no-opinion"
  contribution that yields zero edge adjustment — the scorer sees
  exactly the unadjusted edge.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, Optional

from app.config.settings import settings
from app.database.models import TraderPosition
from app.services.microstructure import MicrostructureFeatures

from .microstructure import MicrostructureContext, compute_microstructure
from .momentum import MomentumContext, compute_momentum
from .whales import WhaleContext, compute_whales


_NEUTRAL = 0.5


def _to_score100(x: float) -> float:
    return round(max(0.0, min(100.0, x * 100.0)), 2)


def _deviation_from_neutral(sub_score: float) -> float:
    """Map 0..1 to -1..+1 centered on neutral (0.5)."""
    return max(-1.0, min(1.0, (sub_score - _NEUTRAL) * 2.0))


@dataclass(frozen=True)
class IntelligenceReport:
    """Advisory view — never modifies core logic on its own."""

    enabled: bool
    market_context_score: float  # 0..100
    edge_adjustment_score: float  # pp (percentage points)
    microstructure: Optional[MicrostructureContext] = None
    momentum: Optional[MomentumContext] = None
    whales: Optional[WhaleContext] = None

    def as_feature_dict(self) -> Dict[str, Any]:
        """Serialisable view for inclusion in the signal feature vector."""
        out: Dict[str, Any] = {
            "mi_enabled": self.enabled,
            "mi_context_score": self.market_context_score,
            "mi_edge_adjustment_pct": self.edge_adjustment_score,
        }
        if self.microstructure is not None:
            out["mi_micro"] = asdict(self.microstructure)
        if self.momentum is not None:
            out["mi_momentum"] = asdict(self.momentum)
        if self.whales is not None:
            out["mi_whales"] = asdict(self.whales)
        return out


def neutral_report() -> IntelligenceReport:
    """Return the "no-op" report used when the layer is disabled.

    The scorer will receive ``edge_adjustment_score == 0`` and the feature
    vector will record ``mi_enabled=False``.  No other code paths change.
    """
    return IntelligenceReport(
        enabled=False,
        market_context_score=50.0,
        edge_adjustment_score=0.0,
        microstructure=None,
        momentum=None,
        whales=None,
    )


@dataclass
class MarketIntelligenceAggregator:
    """Stateless aggregator that glues the sub-modules together.

    Parameters mirror ``settings.mi_*`` so tests can override them
    without touching the global config object.
    """

    max_adjustment_pct: float = field(
        default_factory=lambda: float(settings.mi_max_edge_adjustment_pct)
    )
    weight_micro: float = field(
        default_factory=lambda: float(settings.mi_weight_microstructure)
    )
    weight_momentum: float = field(
        default_factory=lambda: float(settings.mi_weight_momentum)
    )
    weight_whales: float = field(
        default_factory=lambda: float(settings.mi_weight_whales)
    )
    whale_unusual_usd: float = field(
        default_factory=lambda: float(settings.mi_whale_unusual_usd)
    )
    whale_flow_saturation_usd: float = field(
        default_factory=lambda: float(settings.mi_whale_flow_saturation_usd)
    )

    def compute(
        self,
        *,
        side: str,
        micro: Optional[MicrostructureFeatures],
        previous_spread_bps: Optional[float] = None,
        price_history: Iterable = (),
        whale_positions: Iterable[TraderPosition] = (),
    ) -> IntelligenceReport:
        """Build an advisory :class:`IntelligenceReport` from raw inputs.

        All inputs are optional — every module has a neutral fallback so
        the caller can feed whatever subset it managed to assemble.
        """
        micro_ctx = compute_microstructure(
            micro=micro, previous_spread_bps=previous_spread_bps
        )
        momentum_ctx = compute_momentum(history=price_history, side=side)
        whale_ctx = compute_whales(
            positions=whale_positions,
            side=side,
            unusual_usd=self.whale_unusual_usd,
            flow_saturation_usd=self.whale_flow_saturation_usd,
        )

        # --- Normalise weights so any combo of zeroed-out modules is safe.
        w_sum = self.weight_micro + self.weight_momentum + self.weight_whales
        if w_sum <= 0:
            context = _NEUTRAL
            weighted_dev = 0.0
        else:
            context = (
                self.weight_micro * micro_ctx.context_score
                + self.weight_momentum * momentum_ctx.context_score
                + self.weight_whales * whale_ctx.context_score
            ) / w_sum
            weighted_dev = (
                self.weight_micro * _deviation_from_neutral(micro_ctx.context_score)
                + self.weight_momentum
                * _deviation_from_neutral(momentum_ctx.context_score)
                + self.weight_whales * _deviation_from_neutral(whale_ctx.context_score)
            ) / w_sum

        edge_adjustment = max(
            -self.max_adjustment_pct,
            min(self.max_adjustment_pct, weighted_dev * self.max_adjustment_pct),
        )

        return IntelligenceReport(
            enabled=True,
            market_context_score=_to_score100(context),
            edge_adjustment_score=round(edge_adjustment, 4),
            microstructure=micro_ctx,
            momentum=momentum_ctx,
            whales=whale_ctx,
        )
