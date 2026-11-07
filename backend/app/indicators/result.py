"""Uniform indicator output: raw value, normalized score, signal, confidence, explanation, timestamp."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class IndicatorResult:
    name: str
    raw: dict  # the actual computed values (e.g. {"rsi": 71.2})
    score: float  # −100..+100
    confidence: float  # 0..1
    explanation: str
    ts: datetime
    timeframe: str = ""
    flags: dict = field(default_factory=dict)

    @property
    def signal(self) -> str:
        return "BULLISH" if self.score >= 15 else "BEARISH" if self.score <= -15 else "NEUTRAL"

    def as_dict(self) -> dict:
        return {"name": self.name, "timeframe": self.timeframe, "raw": _round(self.raw), "score": round(self.score, 2),
                "signal": self.signal, "confidence": round(self.confidence, 3), "explanation": self.explanation,
                "ts": self.ts.isoformat(), "flags": self.flags}


def _round(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        out[k] = round(v, 6) if isinstance(v, float) else v
    return out
