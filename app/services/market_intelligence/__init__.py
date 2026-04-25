"""Market Intelligence — read-only *feature layer* on top of Prym Signals.

This package is a **strictly advisory** module.  Its only public outputs
are two scalars plus a diagnostic breakdown:

* ``market_context_score`` (0–100) — how "healthy" the market looks
  right now (microstructure quality + momentum regularity + whale
  presence).  Used for observability / feature vectors / logging.
* ``edge_adjustment_score`` (``±settings.mi_max_edge_adjustment_pct``) —
  a small signed delta (in percentage points) that can be **added** to
  the measured ``net_edge_pct`` before the scorer's hard gates run.
  Positive when the context confirms the signal, negative when it
  weakens it.  Strictly bounded so it can never single-handedly push a
  bad trade through.

The layer never decides to trade, never sizes, never manages risk.  It
can be disabled with one flag (``MARKET_INTELLIGENCE_ENABLED=false``) and
will transparently return a neutral :class:`IntelligenceReport`, leaving
the rest of the pipeline byte-for-byte identical to the pre-layer
behaviour.

Design rules (enforced by tests):

1. **Pure outputs, no side effects.**  The aggregator doesn't write to
   the DB or place orders.  The sub-modules only read.
2. **Bounded adjustments.**  ``edge_adjustment_score`` is clipped to
   ``±settings.mi_max_edge_adjustment_pct`` (default ``2.0``) so the
   module can nudge borderline trades but cannot override the hard
   ``min_edge_pct`` by more than that bound.
3. **Disabled default.**  The feature flag is OFF out of the box.
4. **Safe neutral.**  Every sub-module returns a neutral feature on
   missing data rather than raising; the aggregator therefore always
   produces a report, even for brand-new markets.
"""
from __future__ import annotations

from .aggregator import (
    IntelligenceReport,
    MarketIntelligenceAggregator,
    neutral_report,
)
from .microstructure import MicrostructureContext, compute_microstructure
from .momentum import MomentumContext, compute_momentum
from .whales import WhaleContext, compute_whales

__all__ = [
    "IntelligenceReport",
    "MarketIntelligenceAggregator",
    "MicrostructureContext",
    "MomentumContext",
    "WhaleContext",
    "compute_microstructure",
    "compute_momentum",
    "compute_whales",
    "neutral_report",
]
