"""Engine 18: data quality. Never manufactures data — it measures what is missing."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.context import MarketContext
from app.engines.base import EngineResult
from app.normalize.consensus import consensus


@dataclass
class DataQuality:
    score: float
    label: str
    critical_stale: bool
    price: float | None
    price_consensus: dict
    categories: dict = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"score": round(self.score, 1), "label": self.label, "critical_stale": self.critical_stale,
                "price": self.price, "price_consensus": self.price_consensus,
                "categories": self.categories, "missing": self.missing, "reasons": self.reasons}


# (category, weight, critical, engines that represent it)
CATEGORIES = (
    ("price", 22, True, ()),
    ("candles", 15, True, ("trend", "structure")),
    ("derivatives", 20, True, ("open_interest", "funding")),
    ("order_flow", 10, False, ("spot_flow", "cvd", "volume", "vwap")),
    ("positioning", 13, False, ("liquidation", "whale", "smart_money", "etf", "exchange_flow", "long_short")),
    ("onchain_network", 8, False, ("onchain", "network")),
    ("macro_liquidity", 7, False, ("macro", "stablecoin", "cross_market", "volatility")),
    ("agreement", 5, False, ()),
)


def best_price(ctx: MarketContext) -> tuple[float | None, dict]:
    c = consensus(ctx, "BTC_PRICE_USD")
    if c.value is None:
        c = consensus(ctx, "BTC_PRICE_USDT")
    if c.value is None:
        cs = ctx.get_candles("spot_1h")
        if cs:
            return cs[-1].close, {"metric": "CANDLES_SPOT_1H", "value": cs[-1].close, "n": 1, "fallback": True}
        return None, {}
    return c.value, c.as_dict()


def assess(ctx: MarketContext, engines: dict[str, EngineResult]) -> DataQuality:
    price, pc = best_price(ctx)
    cats, missing, reasons = {}, [], []
    critical_stale = False
    for name, weight, critical, members in CATEGORIES:
        if name == "price":
            if price is None:
                q = 0.0
            elif pc.get("fallback"):
                q = 0.4  # only the last candle close
            else:
                q = 0.5 * min(1.0, pc.get("n", 1) / 3) + 0.5 * pc.get("freshness", 1.0)
            if pc.get("dispersion", 0) > 0.005:
                q *= 0.6
                reasons.append(f"Price feeds disagree ({pc['dispersion']:.2%} dispersion)")
        elif name == "agreement":
            agrees = [e.inputs.get("agreement", 0.5) for e in engines.values() if e.available and e.inputs.get("sources", 0) > 1]
            q = sum(agrees) / len(agrees) if agrees else 0.5
        else:
            parts = []
            for m in members:
                e = engines.get(m)
                if not e or not e.available:
                    parts.append(0.0)
                    continue
                fresh = sum(s.freshness for s in e.signals) / len(e.signals) if e.signals else 0.5
                parts.append(fresh)
            q = sum(parts) / len(parts) if parts else 0.0
            if name == "positioning":  # any one positioning feed is meaningful coverage
                q = min(1.0, sum(parts) / 2)
        cats[name] = round(q, 3)
        if q < 0.3:
            missing.append(name)
        if critical and q < 0.5:
            critical_stale = True
            reasons.append(f"Critical data '{name}' unavailable or stale")
    score = sum(w * cats[n] for n, w, _, _ in CATEGORIES) / sum(w for _, w, _, _ in CATEGORIES) * 100
    label = "HIGH" if score >= 80 else "MEDIUM" if score >= 60 else "LOW"
    if missing:
        reasons.append("Missing/weak: " + ", ".join(missing))
    down = [n for n, e in engines.items() if not e.available]
    if down:
        reasons.append("Engines without data: " + ", ".join(down))
    return DataQuality(score=score, label=label, critical_stale=critical_stale, price=price,
                       price_consensus=pc, categories=cats, missing=missing, reasons=reasons)
