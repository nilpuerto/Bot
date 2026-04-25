"""Live balance provider — single source of truth for the bot's bankroll.

The bot needs to know *how much USDC is actually free on-chain* before
every sizing decision so that:

* it never tries to spend more than what is liquid in the wallet,
* the user does not have to keep ``user.balance`` in sync with the real
  on-chain state manually,
* manual positions or external withdrawals reduce the available size
  automatically (because they reduce the on-chain balance).

``user.balance`` becomes an **optional ceiling**: when > 0 it caps the
effective balance; when 0 (or unset) the bot uses 100 % of the liquid
USDC.  ``risk_pct`` and the sizing bands are still applied on top.

A short TTL cache (default 60 s) keeps the RPC hit-rate low without
letting the balance drift far from reality.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.config.settings import settings
from app.database.models import User
from app.integrations.polymarket_client import PolymarketClient
from app.utils.logger import get_logger
from app.utils.time import utcnow


logger = get_logger(__name__)


@dataclass
class BalanceBreakdown:
    """All the balance figures UX and sizing code can ever need."""

    # Raw on-chain liquid USDC (0 when we could not fetch it).
    liquid_usdc: Decimal
    # ``user.balance`` as configured by the user (0 / None ⇒ no cap).
    configured_cap: Decimal
    # What the bot will actually treat as the bankroll for sizing.
    effective: Decimal


class LiveBalanceProvider:
    def __init__(
        self,
        polymarket: PolymarketClient,
        ttl_seconds: int = 60,
    ) -> None:
        self._poly = polymarket
        self._ttl = max(1, int(ttl_seconds))
        self._cached_value: Optional[Decimal] = None
        self._cached_at = None  # type: Optional[object]
        self._stale: bool = False

    async def liquid_usdc(self) -> Decimal:
        """Return the on-chain USDC balance, cached for ``ttl_seconds``.

        In simulation mode we short-circuit and return ``0`` so nothing
        depends on the RPC during tests / offline runs.
        """
        if settings.simulation_mode:
            return Decimal("0")

        now = utcnow()
        if self._cached_value is not None and self._cached_at is not None:
            age = (now - self._cached_at).total_seconds()  # type: ignore[operator]
            if age < self._ttl:
                return self._cached_value

        try:
            value = await self._poly.get_usdc_balance()
            self._cached_value = value
            self._cached_at = now
            self._stale = False
            return value
        except Exception as exc:  # noqa: BLE001
            # Keep serving the last known value so an RPC blip does not
            # freeze trading.  It is flagged stale for logging only.
            self._stale = True
            logger.warning("usdc_fetch_failed", error=str(exc))
            return self._cached_value if self._cached_value is not None else Decimal("0")

    async def effective_balance(self, user: User) -> BalanceBreakdown:
        """Return the authoritative bankroll numbers for ``user``.

        Rules:
          * simulation mode ⇒ ``user.balance`` wins (no RPC).
          * live mode + ``user.balance > 0`` ⇒ ``min(usdc, user.balance)``
            so the configured value acts as a hard ceiling.
          * live mode + ``user.balance <= 0`` ⇒ the full liquid USDC
            is used (auto mode, no manual upkeep).
        """
        configured = Decimal(user.balance or 0)

        if settings.simulation_mode:
            return BalanceBreakdown(
                liquid_usdc=Decimal("0"),
                configured_cap=configured,
                effective=configured,
            )

        usdc = await self.liquid_usdc()
        if configured > 0:
            effective = min(usdc, configured)
        else:
            effective = usdc
        return BalanceBreakdown(
            liquid_usdc=usdc,
            configured_cap=configured,
            effective=effective,
        )
