"""Engines: whales (N), smart money, ETF flows (P)."""
from __future__ import annotations

from datetime import timedelta
from statistics import median

from app.core.context import MarketContext
from app.engines.base import EngineResult, combine, make_signal, squash, unavailable
from app.indicators.result import IndicatorResult


def whale(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    """Transfers are classified by entity before any meaning is assigned. Internal exchange or
    custodian movements are never counted as selling. Single-source evidence is WEAK by rule."""
    sources = ctx.event_source.get("whale_transfers", [])
    if not sources:
        return unavailable("whale", "no whale-transfer feed connected (Whale Alert key not set)")
    cut = ctx.now - timedelta(hours=24)
    sigs, detail = [], {}
    for prov in sources:
        evs = [e for e in ctx.events.get("whale_transfers", []) if e["ts"] >= cut]
        by_kind: dict[str, float] = {}
        counts: dict[str, int] = {}
        for e in evs:
            by_kind[e["kind"]] = by_kind.get(e["kind"], 0.0) + e["usd"]
            counts[e["kind"]] = counts.get(e["kind"], 0) + 1
        net = by_kind.get("from_exchange", 0.0) - by_kind.get("to_exchange", 0.0)
        scale = (cfg or {}).get("whale_net_scale_usd", 300e6)
        sigs.append(make_signal(ctx, prov, "WHALE_TRANSFERS", ctx.fetched_at.get(prov, ctx.now), 100 * squash(net, scale),
                                note=f"net exchange withdrawal ${net/1e6:+.0f}M (24h)"))
        detail[prov] = {"usd_by_kind": {k: round(v) for k, v in by_kind.items()}, "counts": counts, "net_withdrawal_usd": round(net)}
    res = combine("whale", sigs, single_source_cap=0.4)
    d = next(iter(detail.values()))
    net = d["net_withdrawal_usd"]
    c = d["counts"]
    txt = (f"Whales {'withdrew' if net > 0 else 'deposited'} net ${abs(net)/1e6:.0f}M {'from' if net > 0 else 'to'} exchanges in 24h "
           f"({c.get('to_exchange', 0)} whale→exchange, {c.get('from_exchange', 0)} exchange→whale/cold storage, "
           f"{c.get('wallet_to_wallet', 0)} whale→whale, {c.get('internal', 0) + c.get('exchange_to_exchange', 0)} internal/custodian ignored)")
    res.reasons = [txt, f"Whale confirmation: {res.confirmation} ({len(sources)} independent source(s))"]
    res.inputs["by_source"] = detail
    res.indicators = [IndicatorResult("whale_flow", {"net_withdrawal_usd": net, **d["usd_by_kind"]}, res.score,
                                      res.confidence, txt, ctx.now, "24h")]
    return res


def smart_money(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    nets = ctx.get_scalars("SMART_MONEY_NET_BTC")
    if not nets:
        return unavailable("smart_money", "no smart-money positioning data")
    gross = {d.provider: d.value for d in ctx.get_scalars("SMART_MONEY_GROSS_BTC")}
    sigs, reasons = [], []
    for d in nets:
        g = gross.get(d.provider) or abs(d.value) or 1.0
        bias = d.value / g
        s = ctx.series.get("SMART_MONEY_NET_BTC", {}).get(d.provider)
        delta = s.abs_change(4 * 3600) if s else None
        raw = 60 * bias + (40 * squash(delta, 0.05 * g) if delta is not None else 0.0)
        holders = int(d.meta.get("longs", 0)) + int(d.meta.get("shorts", 0))
        sigs.append(make_signal(ctx, d.provider, "SMART_MONEY_NET_BTC", d.ts, raw, confidence=min(1.0, holders / 8),
                                note=f"net {d.value:+.1f} BTC of {g:.1f} gross, {holders} holders"))
        reasons.append(f"Top Hyperliquid traders net {'long' if d.value > 0 else 'short'} {abs(d.value):,.0f} BTC "
                       f"({d.meta.get('longs', '?')} long / {d.meta.get('shorts', '?')} short of {d.meta.get('traders', '?')} tracked)")
        if delta is not None:
            reasons.append(f"Cohort net position {delta:+,.0f} BTC over 4h")
    res = combine("smart_money", sigs, single_source_cap=0.5)
    res.reasons = reasons + ["Leaderboard profitability is not assumed to predict direction (capped confidence)"]
    res.indicators = [IndicatorResult("smart_money_net", {"net_btc": nets[0].value}, res.score, res.confidence,
                                      reasons[0], nets[0].ts, "15m")]
    return res


def etf(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    """Daily, 7-day and 30-day net flow plus persistence. A single unusual day is clipped to
    2× the median absolute daily flow so it cannot dominate."""
    series = ctx.get_series("ETF_NET_FLOW_USD")
    if not series:
        return unavailable("etf", "no ETF flow source available (Farside blocked; CoinGlass key or admin import needed)")
    sigs, inds, info = [], [], {}
    for prov, s in series.items():
        vals = [v for _, v in s.points[-30:]]
        mad = median([abs(v) for v in vals]) or 1.0
        clipped = [max(-2 * mad, min(2 * mad, v)) for v in vals]
        last, d7, d30 = vals[-1], sum(clipped[-7:]), sum(clipped)
        w7 = vals[-7:]
        pos7 = sum(1 for v in w7 if v > 0) / len(w7)
        persistence = (sum(1 for v in w7 if v > 0) - sum(1 for v in w7 if v < 0)) / len(w7)
        raw = 100 * (0.2 * squash(clipped[-1], 400e6) + 0.4 * squash(d7, 1.5e9)
                     + 0.25 * squash(d30, 4e9) + 0.15 * persistence)
        sigs.append(make_signal(ctx, prov, "ETF_NET_FLOW_USD", s.last_ts, raw,
                                note=f"{s.last_ts:%Y-%m-%d} ${last/1e6:+.0f}M, 7d ${d7/1e6:+.0f}M, 30d ${d30/1e6:+.0f}M"))
        info[prov] = {"last_day": s.last_ts.date().isoformat(), "last_usd": round(last), "sum_7d_usd": round(d7),
                      "sum_30d_usd": round(d30), "positive_days_7d": round(pos7, 2)}
        txt = (f"ETF flow {s.last_ts:%Y-%m-%d} ${last/1e6:+,.0f}M; 7d ${d7/1e6:+,.0f}M; 30d ${d30/1e6:+,.0f}M; "
               f"{pos7:.0%} inflow days this week")
        inds.append(IndicatorResult("etf_flow", info[prov], raw, 0.7, f"{prov}: {txt}", s.last_ts, "1D"))
    res = combine("etf", sigs)
    best = max(info.values(), key=lambda v: v["last_day"])
    persistent = "persistent inflows — institutional demand" if best["positive_days_7d"] >= 0.7 and best["sum_7d_usd"] > 0 else \
        "persistent outflows — institutional weakness" if best["positive_days_7d"] <= 0.3 and best["sum_7d_usd"] < 0 else "mixed"
    res.reasons = [f"Spot BTC ETFs {'inflow' if best['last_usd'] >= 0 else 'outflow'} ${abs(best['last_usd'])/1e6:.0f}M on "
                   f"{best['last_day']}; 7d ${best['sum_7d_usd']/1e6:+,.0f}M, 30d ${best['sum_30d_usd']/1e6:+,.0f}M ({persistent})"]
    res.inputs["by_source"] = info
    res.indicators = inds
    return res
