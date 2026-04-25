"""User repository — auth, mode, settings, balance."""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User, UserMode


class UsersRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_telegram_id(self, telegram_id: int) -> Optional[User]:
        res = await self.session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        return res.scalar_one_or_none()

    async def get_or_create(
        self, telegram_id: int, username: Optional[str] = None
    ) -> User:
        existing = await self.get_by_telegram_id(telegram_id)
        if existing is not None:
            return existing
        user = User(telegram_id=telegram_id, username=username)
        self.session.add(user)
        await self.session.flush()
        return user

    async def list_allowed(self) -> list[User]:
        res = await self.session.execute(select(User).where(User.is_allowed.is_(True)))
        return list(res.scalars())

    async def set_mode(self, user_id: int, mode: UserMode) -> None:
        await self.session.execute(
            update(User).where(User.id == user_id).values(mode=mode)
        )

    async def set_balance(self, user_id: int, balance: Decimal) -> None:
        await self.session.execute(
            update(User).where(User.id == user_id).values(balance=balance)
        )

    async def update_settings(
        self,
        user_id: int,
        *,
        risk_pct: Optional[Decimal] = None,
        max_trades_per_day: Optional[int] = None,
        auto_urgency_threshold: Optional[int] = None,
        auto_score_threshold: Optional[Decimal] = None,
        stop_loss_enabled: Optional[bool] = None,
        notifications_enabled: Optional[bool] = None,
        is_active: Optional[bool] = None,
    ) -> None:
        values: dict = {}
        if risk_pct is not None:
            values["risk_pct"] = risk_pct
        if max_trades_per_day is not None:
            values["max_trades_per_day"] = max_trades_per_day
        if auto_urgency_threshold is not None:
            values["auto_urgency_threshold"] = auto_urgency_threshold
        if auto_score_threshold is not None:
            values["auto_score_threshold"] = auto_score_threshold
        if stop_loss_enabled is not None:
            values["stop_loss_enabled"] = stop_loss_enabled
        if notifications_enabled is not None:
            values["notifications_enabled"] = notifications_enabled
        if is_active is not None:
            values["is_active"] = is_active
        if not values:
            return
        await self.session.execute(
            update(User).where(User.id == user_id).values(**values)
        )

    async def toggle_flag(self, user_id: int, flag: str) -> bool:
        """Flip a boolean flag and return its new value.

        ``flag`` must be one of: ``stop_loss_enabled``,
        ``notifications_enabled``, ``is_active``.
        """
        allowed = {"stop_loss_enabled", "notifications_enabled", "is_active"}
        if flag not in allowed:
            raise ValueError(f"Unknown flag: {flag}")
        user = await self.session.get(User, user_id)
        if user is None:
            raise ValueError(f"User {user_id} not found")
        new_value = not bool(getattr(user, flag))
        setattr(user, flag, new_value)
        await self.session.flush()
        return new_value
