-- BTC Signal — PostgreSQL / TimescaleDB schema.
-- The application creates these tables itself (SQLAlchemy, app/db/models.py) and converts the
-- time-series tables to hypertables on startup when the timescaledb extension is available.
-- This file documents the schema and adds retention/compression policies for production.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Every scalar a provider reported, exactly as reported.
CREATE TABLE IF NOT EXISTS metric_observations (
    id          BIGSERIAL,
    ts          TIMESTAMPTZ NOT NULL,          -- provider's data time
    fetched_at  TIMESTAMPTZ NOT NULL,
    metric      VARCHAR(64) NOT NULL,          -- canonical metric (app/normalize/metrics.py)
    provider    VARCHAR(64) NOT NULL,          -- adapter id, optionally adapter:sub
    value       DOUBLE PRECISION NOT NULL,
    meta        JSONB,
    PRIMARY KEY (id, ts)
);
SELECT create_hypertable('metric_observations', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS ix_obs_metric_provider_ts ON metric_observations (metric, provider, ts DESC);

CREATE TABLE IF NOT EXISTS signals (
    id               SERIAL PRIMARY KEY,
    ts               TIMESTAMPTZ NOT NULL,
    price            DOUBLE PRECISION,
    direction        VARCHAR(10) NOT NULL,     -- BULLISH / BEARISH / NEUTRAL
    trade            VARCHAR(3)  NOT NULL,     -- YES / NO
    score            DOUBLE PRECISION NOT NULL,
    confidence       DOUBLE PRECISION NOT NULL,
    confidence_class VARCHAR(8) NOT NULL,
    data_quality     DOUBLE PRECISION NOT NULL,
    entry_low        DOUBLE PRECISION,
    entry_high       DOUBLE PRECISION,
    invalidation     DOUBLE PRECISION,
    regime           VARCHAR(48),
    mode             VARCHAR(10) NOT NULL DEFAULT 'live',
    components       JSONB NOT NULL,           -- per-engine score/confidence/weight/contribution
    sources          JSONB NOT NULL,           -- per-engine source signals and raw provider inputs
    payload          JSONB NOT NULL,           -- full decision document as served
    config_version   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals (ts DESC);

CREATE TABLE IF NOT EXISTS signal_outcomes (
    id               SERIAL PRIMARY KEY,
    signal_id        INTEGER NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
    horizon_min      INTEGER NOT NULL,         -- 15, 30, 60, 240, 720, 1440
    evaluated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    exit_price       DOUBLE PRECISION NOT NULL,
    return_pct       DOUBLE PRECISION NOT NULL, -- in the signal's direction
    mfe_pct          DOUBLE PRECISION NOT NULL,
    mae_pct          DOUBLE PRECISION NOT NULL,
    hit_invalidation BOOLEAN NOT NULL DEFAULT FALSE,
    win              BOOLEAN,                  -- NULL for NEUTRAL
    CONSTRAINT uq_outcome_signal_horizon UNIQUE (signal_id, horizon_min)
);

CREATE TABLE IF NOT EXISTS candles_1m (
    ts TIMESTAMPTZ PRIMARY KEY, open DOUBLE PRECISION, high DOUBLE PRECISION,
    low DOUBLE PRECISION, close DOUBLE PRECISION, volume DOUBLE PRECISION
);
SELECT create_hypertable('candles_1m', 'ts', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS liquidation_events (
    id BIGSERIAL, ts TIMESTAMPTZ NOT NULL, venue VARCHAR(24), side VARCHAR(8),
    btc DOUBLE PRECISION, price DOUBLE PRECISION, usd DOUBLE PRECISION,
    PRIMARY KEY (id, ts)
);
SELECT create_hypertable('liquidation_events', 'ts', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS etf_flows (
    day TIMESTAMPTZ PRIMARY KEY, net_flow_usd DOUBLE PRECISION NOT NULL,
    source VARCHAR(64) NOT NULL, entered_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS config_versions (
    id SERIAL PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    config JSONB NOT NULL, note TEXT, active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS weight_recommendations (
    id SERIAL PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), source VARCHAR(32) NOT NULL,
    active_weights JSONB NOT NULL, recommended_weights JSONB NOT NULL, stats JSONB NOT NULL,
    status VARCHAR(12) NOT NULL DEFAULT 'pending', decided_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id SERIAL PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), status VARCHAR(12) NOT NULL,
    params JSONB NOT NULL, results JSONB, error TEXT
);

CREATE TABLE IF NOT EXISTS state_kv (
    key VARCHAR(64) PRIMARY KEY, value JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS system_logs (
    id SERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT now(), level VARCHAR(10) NOT NULL,
    source VARCHAR(64) NOT NULL, message TEXT NOT NULL
);

-- Production retention / compression (adjust to taste).
SELECT add_retention_policy('metric_observations', INTERVAL '90 days', if_not_exists => TRUE);
SELECT add_retention_policy('liquidation_events', INTERVAL '90 days', if_not_exists => TRUE);
ALTER TABLE candles_1m SET (timescaledb.compress);
SELECT add_compression_policy('candles_1m', INTERVAL '7 days', if_not_exists => TRUE);
