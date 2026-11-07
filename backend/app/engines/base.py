"""Shared engine types and the source-contribution formula.

    SOURCE CONTRIBUTION = raw_signal × relevance × reliability × freshness × confidence

An engine's score is the contribution-weighted mean of its source signals. Its
confidence reflects cross-source confirmation: one source alone is weak evidence.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from app.core.context import MarketContext


def clamp(x: float, lo: float = -100.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def squash(x: float, scale: float) -> float:
    """Map an unbounded value to (-1, 1); `scale` is the value giving ~0.76."""
    return math.tanh(x / scale) if scale else 0.0


@dataclass
class SourceSignal:
    provider: str
    raw: float  # -100..100
    relevance: float
    reliability: float
    freshness: float
    confidence: float = 1.0
    note: str = ""

    @property
    def weight(self) -> float:
        return max(0.0, self.relevance * self.reliability * self.freshness * self.confidence)

    @property
    def contribution(self) -> float:
        return self.raw * self.weight

    def as_dict(self) -> dict:
        return {"provider": self.provider, "raw": round(self.raw, 2), "relevance": round(self.relevance, 3),
                "reliability": round(self.reliability, 3), "freshness": round(self.freshness, 3),
                "confidence": round(self.confidence, 3), "weight": round(self.weight, 4),
                "contribution": round(self.contribution, 3), "note": self.note}


def make_signal(ctx: MarketContext, provider: str, metric: str, ts: datetime | None, raw: float,
                confidence: float = 1.0, note: str = "", relevance: float | None = None) -> SourceSignal:
    return SourceSignal(provider=provider, raw=clamp(raw),
                        relevance=ctx.relevance(provider) if relevance is None else relevance,
                        reliability=ctx.reliab(provider), freshness=ctx.fresh(metric, ts),
                        confidence=confidence, note=note)


@dataclass
class EngineResult:
    engine: str
    available: bool
    score: float = 0.0
    confidence: float = 0.0  # 0..1
    reasons: list[str] = field(default_factory=list)
    signals: list[SourceSignal] = field(default_factory=list)
    flags: dict = field(default_factory=dict)
    inputs: dict = field(default_factory=dict)
    levels: dict = field(default_factory=dict)
    directional: bool = True
    confirmation: str = "NONE"  # STRONG / MODERATE / WEAK / NONE
    indicators: list = field(default_factory=list)  # IndicatorResult

    @property
    def stance(self) -> str:
        if not self.available:
            return "UNAVAILABLE"
        if self.score >= 15:
            return "BULLISH"
        if self.score <= -15:
            return "BEARISH"
        return "NEUTRAL"

    def as_dict(self) -> dict:
        return {"engine": self.engine, "available": self.available, "score": round(self.score, 2),
                "confidence": round(self.confidence, 3), "stance": self.stance,
                "confirmation": self.confirmation, "reasons": self.reasons,
                "signals": [s.as_dict() for s in self.signals], "flags": self.flags,
                "inputs": self.inputs, "levels": self.levels, "directional": self.directional,
                "indicators": [i.as_dict() for i in self.indicators]}


def unavailable(engine: str, why: str, directional: bool = True) -> EngineResult:
    return EngineResult(engine=engine, available=False, reasons=[why], directional=directional)


def combine(engine: str, signals: list[SourceSignal], *, single_source_cap: float = 0.6,
            min_sources_strong: int = 3) -> EngineResult:
    """Aggregate source signals with cross-source confirmation."""
    usable = [s for s in signals if s.weight > 0]
    if not usable:
        return unavailable(engine, "no fresh source data")
    total_w = sum(s.weight for s in usable)
    score = clamp(sum(s.contribution for s in usable) / total_w)
    sign = 1 if score > 0 else -1 if score < 0 else 0
    if len(usable) == 1:
        agreement = 1.0
        conf = min(single_source_cap, 0.3 + 0.5 * abs(score) / 100)
        confirmation = "WEAK"
    else:
        directional = [s for s in usable if abs(s.raw) >= 5]
        agree_w = sum(s.weight for s in directional if (s.raw > 0) == (sign > 0)) if sign else 0.0
        dir_w = sum(s.weight for s in directional) or 1.0
        agreement = agree_w / dir_w if directional else 0.5
        n_agree = sum(1 for s in directional if sign and (s.raw > 0) == (sign > 0))
        confirmation = "STRONG" if n_agree >= min_sources_strong and agreement >= 0.75 else \
            "MODERATE" if n_agree >= 2 and agreement >= 0.6 else "WEAK"
        conf = 0.25 + 0.45 * agreement + 0.3 * min(1.0, abs(score) / 60)
        if confirmation == "WEAK":
            conf = min(conf, single_source_cap)
    avg_fresh = sum(s.freshness for s in usable) / len(usable)
    conf *= 0.6 + 0.4 * avg_fresh
    return EngineResult(engine=engine, available=True, score=score, confidence=round(min(1.0, conf), 4),
                        signals=usable, confirmation=confirmation,
                        inputs={"agreement": round(agreement, 3), "sources": len(usable)})


def weighted_indicators(items: list[tuple]) -> tuple[float, float]:
    """(IndicatorResult | None, weight) pairs → (confidence-weighted score, mean confidence)."""
    used = [(i, w * i.confidence) for i, w in items if i is not None and w > 0]
    tw = sum(w for _, w in used)
    if not tw:
        return 0.0, 0.0
    return sum(i.score * w for i, w in used) / tw, sum(i.confidence for i, _ in used) / len(used)
