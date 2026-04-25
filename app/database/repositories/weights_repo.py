"""Component-weight repository.

Weights are multiplicative modifiers applied on top of each scoring
pillar.  They float in the bounded interval ``[0.5, 1.5]`` and are
nudged by :mod:`app.services.feedback_loop` after every closed trade.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ComponentWeight


DEFAULT_WEIGHTS: dict[str, Decimal] = {
    "news": Decimal("1.000"),
    "liquidity": Decimal("1.000"),
    "mispricing": Decimal("1.000"),
    "timing": Decimal("1.000"),
}

MIN_WEIGHT = Decimal("0.500")
MAX_WEIGHT = Decimal("1.500")


class WeightsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_all(self) -> dict[str, Decimal]:
        res = await self.session.execute(select(ComponentWeight))
        weights = {w.name: Decimal(w.weight) for w in res.scalars()}
        for k, default in DEFAULT_WEIGHTS.items():
            weights.setdefault(k, default)
        return weights

    async def upsert_many(self, values: Mapping[str, Decimal]) -> None:
        for name, weight in values.items():
            clipped = _clip(Decimal(weight))
            stmt = (
                pg_insert(ComponentWeight)
                .values(name=name, weight=clipped)
                .on_conflict_do_update(
                    index_elements=[ComponentWeight.name],
                    set_={"weight": clipped},
                )
            )
            await self.session.execute(stmt)


def _clip(value: Decimal) -> Decimal:
    if value < MIN_WEIGHT:
        return MIN_WEIGHT
    if value > MAX_WEIGHT:
        return MAX_WEIGHT
    return value
