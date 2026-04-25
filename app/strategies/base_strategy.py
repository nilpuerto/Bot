"""Strategy interface.

Any strategy must expose :meth:`evaluate` returning a :class:`StrategyDecision`,
and :meth:`sizing` that derives USD amount + SL/TP from a user's balance.
This separation keeps risk rules out of the matching/scoring pipeline.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional

from app.integrations.mistral_client import AIAnalysis
from app.integrations.polymarket_client import MarketSnapshot
from app.services.signal_scoring import ScoreBreakdown


@dataclass
class StrategyDecision:
    should_enter: bool
    reason: str
    side: Optional[str] = None  # 'yes' / 'no'


@dataclass
class SizingPlan:
    amount_usd: float
    entry_price: float
    stop_loss: Optional[float]  # None ⇒ stop loss disabled for this trade
    # ``take_profit`` is now optional and informational only.  Under the
    # repricing exit strategy there is NO hard TP ceiling — exits are
    # driven by the partial-TP ladder + trailing stop in the monitor.
    take_profit: Optional[float] = None
    high_confidence: bool = False
    # v2: band label ("low" / "mid" / "high") set by the sizing engine so
    # the trade row can record which confidence level it came from.
    band: Optional[str] = None


class BaseStrategy(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def evaluate(
        self,
        *,
        ai: AIAnalysis,
        market: MarketSnapshot,
        score: ScoreBreakdown,
    ) -> StrategyDecision: ...

    @abc.abstractmethod
    def sizing(
        self,
        *,
        balance: float,
        risk_pct: float,
        entry_price: float,
        high_confidence: bool = False,
        stop_loss_enabled: bool = True,
    ) -> SizingPlan: ...
