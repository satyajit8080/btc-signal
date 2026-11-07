"""Default, admin-editable scoring configuration. Initial weights are a prior, not a truth:
the learning module recommends changes and an admin approves them."""
from __future__ import annotations

import copy

from pydantic import BaseModel, Field, field_validator

from app.engines import CATEGORIES, DIRECTIONAL_ENGINES

# Weights of engines *inside* their category.
DEFAULT_WEIGHTS = {
    "trend": 12, "structure": 8, "momentum": 10, "spot_flow": 12, "cvd": 10, "volume": 6, "vwap": 5, "etf": 8,
    "open_interest": 8, "funding": 6, "liquidation": 8, "long_short": 4, "whale": 10, "smart_money": 3,
    "exchange_flow": 7, "onchain": 7, "network": 3, "stablecoin": 4, "macro": 5, "cross_market": 2, "volatility": 4,
}
# Weights of categories in the final score (a prior; backtests recommend changes, admins approve).
DEFAULT_CATEGORY_WEIGHTS = {
    "TREND": 20, "MOMENTUM": 10, "FLOW": 22, "DERIVATIVES": 16, "POSITIONING": 12, "ON-CHAIN": 10,
    "MACRO": 6, "VOLATILITY": 4,
}


class Thresholds(BaseModel):
    bullish: float = 30
    bearish: float = -30
    min_confidence_for_direction: float = 35
    min_data_quality_for_direction: float = 40
    coverage_exponent: float = 0.5


class TradeGates(BaseModel):
    min_data_quality: float = 60
    min_confidence: float = 55
    min_agreement: float = 0.6
    min_invalidation_pct: float = 0.4
    max_invalidation_pct: float = 4.0
    block_on_liquidation_cascade: bool = True
    block_on_extreme_volatility: bool = True
    block_on_fast_oi_expansion: bool = True
    htf_opposition_limit: float = 0.5
    max_conflict: float = 0.3  # share of effective weight strongly opposing the score
    max_spread_bps: float = 10.0  # poor liquidity
    min_volume_ratio: float = 0.35  # 24h volume vs 7-day average
    min_reward_risk: float = 1.5  # distance to nearest resistance/support vs distance to invalidation
    block_on_regime_transition: bool = True
    divergence_confidence_penalty: float = 5.0  # per opposing divergence, max 3


class EngineParams(BaseModel):
    oi_fast_pct: float = 8.0
    crowded_funding_pct: float = 0.03
    crowded_funding_pct_short: float = 0.02
    extreme_funding_pct: float = 0.05
    crowded_ls: float = 2.2
    liq_min_uptime_s: float = 1800
    liq_stream_4h_usd: float = 20e6
    liq_stream_cascade_1h_usd: float = 25e6
    liq_agg_4h_usd: float = 100e6
    liq_agg_cascade_1h_usd: float = 150e6
    whale_net_scale_usd: float = 300e6
    dvol_extreme: float = 85


class ScoringConfig(BaseModel):
    weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    category_weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_CATEGORY_WEIGHTS))
    thresholds: Thresholds = Field(default_factory=Thresholds)
    trade: TradeGates = Field(default_factory=TradeGates)
    engine_params: EngineParams = Field(default_factory=EngineParams)
    providers_enabled: dict[str, bool] = Field(default_factory=dict)

    @field_validator("weights")
    @classmethod
    def _check_weights(cls, v: dict[str, float]) -> dict[str, float]:
        unknown = set(v) - set(DIRECTIONAL_ENGINES)
        if unknown:
            raise ValueError(f"unknown engines in weights: {sorted(unknown)}")
        if any(w < 0 for w in v.values()):
            raise ValueError("weights must be ≥ 0")
        if sum(v.values()) <= 0:
            raise ValueError("at least one weight must be positive")
        return {**{k: w for k, w in DEFAULT_WEIGHTS.items() if k not in v}, **v}  # configs from older versions

    @field_validator("category_weights")
    @classmethod
    def _check_categories(cls, v: dict[str, float]) -> dict[str, float]:
        unknown = set(v) - set(CATEGORIES)
        if unknown:
            raise ValueError(f"unknown categories: {sorted(unknown)}")
        if any(w < 0 for w in v.values()) or sum(v.values()) <= 0:
            raise ValueError("category weights must be ≥ 0 with a positive total")
        return {**{k: w for k, w in DEFAULT_CATEGORY_WEIGHTS.items() if k not in v}, **v}

    def provider_enabled(self, pid: str) -> bool:
        return self.providers_enabled.get(pid, True)


def default_config() -> ScoringConfig:
    return ScoringConfig()


def merge(base: ScoringConfig, patch: dict) -> ScoringConfig:
    data = copy.deepcopy(base.model_dump())
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(data.get(k), dict):
            data[k].update(v)
        else:
            data[k] = v
    return ScoringConfig.model_validate(data)
