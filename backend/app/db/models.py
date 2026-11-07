"""ORM models. Portable across SQLite (dev/tests) and PostgreSQL/TimescaleDB (production);
see docs/schema.sql for the Timescale hypertable/retention setup."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


class Observation(Base):
    """Every scalar a provider reported, as reported (append-only, hypertable in Timescale)."""
    __tablename__ = "metric_observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    metric: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    __table_args__ = (Index("ix_obs_metric_provider_ts", "metric", "provider", "ts"),)


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    direction: Mapped[str] = mapped_column(String(10))
    trade: Mapped[str] = mapped_column(String(3))
    score: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    confidence_class: Mapped[str] = mapped_column(String(8))
    data_quality: Mapped[float] = mapped_column(Float)
    entry_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    invalidation: Mapped[float | None] = mapped_column(Float, nullable=True)
    regime: Mapped[str | None] = mapped_column(String(48), nullable=True)
    mode: Mapped[str] = mapped_column(String(10), default="live")
    components: Mapped[dict] = mapped_column(JSON)  # per-engine score/confidence/contribution
    sources: Mapped[dict] = mapped_column(JSON)  # per-engine source signals and provider values
    payload: Mapped[dict] = mapped_column(JSON)  # full decision document
    config_version: Mapped[int] = mapped_column(Integer, default=1)


class SignalOutcome(Base):
    __tablename__ = "signal_outcomes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(Integer, index=True)
    horizon_min: Mapped[int] = mapped_column(Integer)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    exit_price: Mapped[float] = mapped_column(Float)
    return_pct: Mapped[float] = mapped_column(Float)  # in the signal's direction (raw move for NEUTRAL)
    mfe_pct: Mapped[float] = mapped_column(Float)
    mae_pct: Mapped[float] = mapped_column(Float)
    hit_invalidation: Mapped[bool] = mapped_column(Boolean, default=False)
    win: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    __table_args__ = (UniqueConstraint("signal_id", "horizon_min", name="uq_outcome_signal_horizon"),)


class Candle1m(Base):
    """Local 1-minute BTCUSDT history so outcome evaluation never depends on a live fetch."""
    __tablename__ = "candles_1m"
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)


class LiquidationEvent(Base):
    __tablename__ = "liquidation_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    venue: Mapped[str] = mapped_column(String(24))
    side: Mapped[str] = mapped_column(String(8))
    btc: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    usd: Mapped[float] = mapped_column(Float)


class EtfFlow(Base):
    """Operator-imported published ETF net flows (e.g. transcribed from Farside)."""
    __tablename__ = "etf_flows"
    day: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    net_flow_usd: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(64))
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ConfigVersion(Base):
    __tablename__ = "config_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    config: Mapped[dict] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class WeightRecommendation(Base):
    __tablename__ = "weight_recommendations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    source: Mapped[str] = mapped_column(String(32))  # "backtest:<id>" or "live"
    active_weights: Mapped[dict] = mapped_column(JSON)
    recommended_weights: Mapped[dict] = mapped_column(JSON)
    stats: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending/approved/rejected
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    status: Mapped[str] = mapped_column(String(12), default="running")
    params: Mapped[dict] = mapped_column(JSON)
    results: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class StateKV(Base):
    """Cross-process latest state (latest decision, provider health) when Redis is absent."""
    __tablename__ = "state_kv"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SystemLog(Base):
    __tablename__ = "system_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    level: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
