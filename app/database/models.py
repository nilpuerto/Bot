"""SQLAlchemy ORM models mirroring ``database.sql``.

Keep this file in lock-step with the SQL schema.  Each model uses
PostgreSQL-native enums so the ORM and raw SQL stay consistent.
"""
from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------- Enums -----------------------------------------------------------

class UserMode(str, enum.Enum):
    SAFE = "safe"
    SEMI = "semi"
    AUTO = "auto"
    # Crypto Mode - dedicated BTC 5min/1h/1d lag-arb pipeline.  When active,
    # the user is excluded from the news/cluster routing entirely; only the
    # crypto orchestrator opens trades for them.
    CRYPTO = "crypto"
    # MAX Mode - clock-snipe BTC 5-minute Up/Down binaries at T-10s using
    # window-delta-dominant TA composite signal; aggressive sizing (only
    # cumulative profit is at risk; falls back to 30 % bankroll on a fresh
    # account).  Excluded from news routing.
    MAX = "max"


class SignalImpact(str, enum.Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class SignalStatus(str, enum.Enum):
    NEW = "new"
    SENT = "sent"
    ACTED = "acted"
    IGNORED = "ignored"
    EXPIRED = "expired"


class TradeSide(str, enum.Enum):
    YES = "yes"
    NO = "no"


class TradeStatus(str, enum.Enum):
    PENDING = "pending"
    OPEN = "open"
    CLOSED = "closed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CloseReason(str, enum.Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    TIME_EXIT = "time_exit"
    MANUAL = "manual"
    EXPIRY = "expiry"
    ERROR = "error"


# ---------- Models ----------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(Text)
    balance: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0"))
    mode: Mapped[UserMode] = mapped_column(
        Enum(UserMode, name="user_mode", values_callable=lambda e: [m.value for m in e]),
        default=UserMode.SAFE,
        nullable=False,
    )
    risk_pct: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=Decimal("10.0"))
    max_trades_per_day: Mapped[int] = mapped_column(Integer, default=4)
    auto_urgency_threshold: Mapped[int] = mapped_column(Integer, default=9)
    auto_score_threshold: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("80.0"))
    stop_loss_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    trades: Mapped[list["Trade"]] = relationship(back_populates="user")


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        CheckConstraint("urgency BETWEEN 0 AND 10", name="chk_signal_urgency"),
        Index("idx_signals_created_at", "created_at"),
        Index("idx_signals_status", "status"),
        Index("idx_signals_market_id", "market_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    news_title: Mapped[str] = mapped_column(Text, nullable=False)
    news_url: Mapped[Optional[str]] = mapped_column(Text)
    news_source: Mapped[Optional[str]] = mapped_column(Text)
    news_published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    news_hash: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)

    market_id: Mapped[Optional[str]] = mapped_column(Text)
    market_question: Mapped[Optional[str]] = mapped_column(Text)
    market_slug: Mapped[Optional[str]] = mapped_column(Text)
    market_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6))
    market_volume_24h: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4))

    impact: Mapped[SignalImpact] = mapped_column(
        Enum(
            SignalImpact,
            name="signal_impact",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=SignalImpact.NEUTRAL,
    )
    urgency: Mapped[int] = mapped_column(Integer, default=0)
    ai_raw: Mapped[Optional[dict]] = mapped_column(JSONB)
    score: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("0"))
    trader_confirmation: Mapped[bool] = mapped_column(Boolean, default=False)
    trader_aligned_count: Mapped[int] = mapped_column(Integer, default=0)
    trader_conviction_usd: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=Decimal("0"))
    status: Mapped[SignalStatus] = mapped_column(
        Enum(
            SignalStatus,
            name="signal_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=SignalStatus.NEW,
    )

    # ---- v2 intelligence fields ---------------------------------------
    quality_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    category: Mapped[Optional[str]] = mapped_column(Text)
    magnitude: Mapped[Optional[int]] = mapped_column(Integer)
    rarity: Mapped[Optional[int]] = mapped_column(Integer)
    timing_phase: Mapped[Optional[int]] = mapped_column(Integer)
    mispricing_z: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    liquidity_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2))
    expected_edge_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 3))
    slippage_bps: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2))
    entities: Mapped[Optional[list]] = mapped_column(JSONB)
    feature_vector: Mapped[Optional[dict]] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    trades: Mapped[list["Trade"]] = relationship(back_populates="signal")


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        Index("idx_trades_user_status", "user_id", "status"),
        Index("idx_trades_market", "market_id"),
        Index("idx_trades_opened_at", "opened_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    signal_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("signals.id", ondelete="SET NULL")
    )

    market_id: Mapped[str] = mapped_column(Text, nullable=False)
    market_question: Mapped[Optional[str]] = mapped_column(Text)
    market_slug: Mapped[Optional[str]] = mapped_column(Text)
    side: Mapped[TradeSide] = mapped_column(
        Enum(TradeSide, name="trade_side", values_callable=lambda e: [m.value for m in e]),
        default=TradeSide.YES,
    )

    entry_price: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    current_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6))
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    shares: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)

    stop_loss: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6))
    take_profit: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6))
    status: Mapped[TradeStatus] = mapped_column(
        Enum(TradeStatus, name="trade_status", values_callable=lambda e: [m.value for m in e]),
        default=TradeStatus.PENDING,
    )
    pnl: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0"))
    pnl_pct: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal("0"))
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=True)
    clob_order_id: Mapped[Optional[str]] = mapped_column(Text)
    close_reason: Mapped[Optional[CloseReason]] = mapped_column(
        Enum(CloseReason, name="close_reason", values_callable=lambda e: [m.value for m in e])
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # ---- v2 trailing-stop + feedback fields ---------------------------
    peak_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6))
    trailing_active: Mapped[bool] = mapped_column(Boolean, default=False)
    band: Mapped[Optional[str]] = mapped_column(Text)
    feature_vector: Mapped[Optional[dict]] = mapped_column(JSONB)

    # ---- Repricing exit-strategy state --------------------------------
    # Single JSONB bag that tracks the partial-TP ladder for this trade:
    #
    #   {
    #     "tiers_hit": [40, 100],
    #     "trailing_pct": 20.0,
    #     "max_pnl_pct_seen": 147.3,
    #     "realized_pnl_usd": 4.82,
    #     "partials": [ {tier, price, shares, pnl, at}, ... ]
    #   }
    exit_state: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict
    )

    user: Mapped["User"] = relationship(back_populates="trades")
    signal: Mapped[Optional["Signal"]] = relationship(back_populates="trades")


class TopTrader(Base):
    __tablename__ = "top_traders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    wallet_address: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    label: Mapped[Optional[str]] = mapped_column(Text)
    roi_30d: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    winrate: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 3))
    volume_30d_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4))
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    positions: Mapped[list["TraderPosition"]] = relationship(back_populates="trader")


class TraderPosition(Base):
    __tablename__ = "trader_positions"
    __table_args__ = (
        Index("idx_trader_positions_market_time", "market_id", "observed_at"),
        Index("idx_trader_positions_trader", "trader_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trader_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("top_traders.id", ondelete="CASCADE")
    )
    market_id: Mapped[str] = mapped_column(Text, nullable=False)
    market_slug: Mapped[Optional[str]] = mapped_column(Text)
    side: Mapped[TradeSide] = mapped_column(
        Enum(TradeSide, name="trade_side", values_callable=lambda e: [m.value for m in e]),
        default=TradeSide.YES,
    )
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6))
    size_usd: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=Decimal("0"))
    tx_hash: Mapped[Optional[str]] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    trader: Mapped["TopTrader"] = relationship(back_populates="positions")


class NewsSeen(Base):
    __tablename__ = "news_seen"

    hash: Mapped[str] = mapped_column(String(40), primary_key=True)
    source: Mapped[Optional[str]] = mapped_column(Text)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DailyCounter(Base):
    __tablename__ = "daily_counters"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_daily_counters_user_day"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    day: Mapped[date] = mapped_column(Date, nullable=False)
    trades_count: Mapped[int] = mapped_column(Integer, default=0)
    last_trade_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MarketPriceHistory(Base):
    """Rolling price samples used by the mispricing z-score service."""

    __tablename__ = "market_price_history"
    __table_args__ = (
        Index(
            "idx_market_price_history_market_time",
            "market_id",
            "observed_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    market_id: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6))
    volume_24h: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ComponentWeight(Base):
    """Weights for the four-pillar scoring engine, tuned by the feedback loop."""

    __tablename__ = "component_weights"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    weight: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=Decimal("1.000"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
