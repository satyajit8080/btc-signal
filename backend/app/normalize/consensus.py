"""Cross-provider consensus for a single canonical metric.

Agreement raises confidence; disagreement lowers it. Outliers are reported, never
silently averaged in or silently dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.context import Datum, MarketContext
from app.normalize.metrics import spec


@dataclass
class Consensus:
    metric: str
    value: float | None
    providers: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    dispersion: float = 0.0  # relative (median absolute deviation / |median|)
    agreement: float = 0.0  # 0..1
    outliers: list[str] = field(default_factory=list)
    freshness: float = 0.0

    @property
    def n(self) -> int:
        return len(self.providers)

    def as_dict(self) -> dict:
        return {
            "metric": self.metric, "value": self.value, "providers": self.providers,
            "dispersion": round(self.dispersion, 6), "agreement": round(self.agreement, 3),
            "outliers": self.outliers, "freshness": round(self.freshness, 3), "n": self.n,
        }


def weighted_median(values: list[tuple[float, float]]) -> float:
    pts = sorted(values)
    total = sum(w for _, w in pts)
    acc = 0.0
    for v, w in pts:
        acc += w
        if acc >= total / 2:
            return v
    return pts[-1][0]


def consensus(ctx: MarketContext, metric: str, datums: list[Datum] | None = None) -> Consensus:
    ds = datums if datums is not None else ctx.get_scalars(metric)
    out = Consensus(metric=metric, value=None)
    weighted: list[tuple[float, float]] = []
    for d in ds:
        f = ctx.fresh(metric, d.ts)
        w = max(ctx.relevance(d.provider), 0.05) * ctx.reliab(d.provider) * f
        if w <= 0:
            continue
        out.providers[d.provider] = d.value
        out.weights[d.provider] = w
        weighted.append((d.value, w))
    if not weighted:
        return out
    med = weighted_median(weighted)
    out.value = med
    total_w = sum(w for _, w in weighted)
    out.freshness = sum(ctx.fresh(metric, d.ts) for d in ds if d.provider in out.providers) / len(out.providers)
    if len(weighted) == 1:
        out.agreement = 0.5  # a single source is not confirmation
        return out
    tol = spec(metric).tolerance or 0.01
    scale = abs(med) if med else 1.0
    devs = {p: abs(v - med) / scale for p, v in out.providers.items()}
    out.dispersion = sorted(devs.values())[len(devs) // 2]
    out.outliers = [p for p, dv in devs.items() if dv > tol]
    agreeing_w = sum(out.weights[p] for p, dv in devs.items() if dv <= tol)
    out.agreement = agreeing_w / total_w
    if out.outliers and len(out.providers) - len(out.outliers) >= 1:
        # Re-centre on the agreeing set so one broken feed cannot drag the value.
        kept = [(v, out.weights[p]) for p, v in out.providers.items() if p not in out.outliers]
        out.value = weighted_median(kept)
    return out
