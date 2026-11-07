# BTC Signal — Architecture

One question, two answers: **is BTC bullish, bearish or neutral?** and, separately, **trade or no trade?**
All complexity lives in the backend; the frontend shows one decision.

## Pipeline

```
DATA PROVIDERS (adapters, app/providers/*)          free exchange/public APIs + keyed intel APIs + liquidation websockets
        ↓  Collector: concurrent fetch, per-provider cadence, health, backoff, cached-but-ageing results
NORMALIZATION (app/normalize/metrics.py)            canonical metric names + units (e.g. funding → fraction per 8h, OI → BTC)
        ↓
MARKET CONTEXT (app/core/context.py)                immutable-ish snapshot: scalars per provider, series, candles, events
        ↓
DATA QUALITY (engines/data_quality.py)              freshness, completeness, provider disagreement, critical staleness
        ↓
INTELLIGENCE ENGINES 01-17 (app/engines/*)          pure functions ctx → EngineResult (score −100..100, confidence, reasons, flags)
        ↓  inside each engine: CROSS-SOURCE CONFIRMATION via combine()
WEIGHTED SIGNAL (scoring/decision.aggregate)        admin weights × engine confidence, renormalised over available engines × coverage^0.5
        ↓
CONFIDENCE (scoring/decision.confidence)            agreement, strength, completeness, freshness, cross-source, history, conflict penalty
        ↓
DIRECTION (scoring/decision.direction_of)           ±30 thresholds, downgraded to NEUTRAL on low confidence / data quality
        ↓
TRADE QUALITY (scoring/decision.trade_quality)      10 gates; any failure → TRADE NO (conservative by construction)
        ↓
LEVELS + EXPLANATION                                ATR/structure-based entry & invalidation; explanation describes, never decides
        ↓
PERSIST (db/repo.py) → CACHE (Redis or state_kv) → API (FastAPI) → SSE → FRONTEND (Next.js)
```

Key design rule: **engines are pure functions of a `MarketContext`.** Live mode builds the context from
adapters; the backtester builds it from historical data cut at time *t* (`MarketContext.cut`), and runs
the *same* decision code. There is no separate "backtest logic" to drift from production.

## Runtime topology

| Process | Command | Role |
|---|---|---|
| API | `uvicorn app.main:app` | REST + SSE. With `BTCS_RUN_WORKER_IN_API=true` (dev) also runs the cycle. |
| Worker | `python -m app.worker` | Collection cycle (default 60 s), liquidation websockets, 1-minute candle store, outcome evaluation, pruning. Exactly one per deployment. |
| PostgreSQL + TimescaleDB | | Signals, outcomes, observations (hypertable), candles (hypertable), config versions, recommendations, backtests, logs. SQLite works for dev/tests. |
| Redis (optional) | | Latest decision + provider health for multi-process API readers. Falls back to the `state_kv` table. |
| Frontend | `next start` / standalone `server.js` | Mobile-first UI: `/`, `/history`, `/admin`. |

The worker is an asyncio scheduler rather than Celery: the workload is one I/O-bound cycle per minute
plus two websockets, which a single event loop handles with less operational surface. Backtests run in
the API process as background tasks with the CPU-bound replay in a thread. If backtests become heavy,
move `run_backtest` behind a queue (Celery/Dramatiq/Arq) — it is already an isolated async function.

## Provider adapter architecture

`app/providers/base.py` defines `Provider` and the interface markers named in the spec:
`MarketDataProvider, DerivativesProvider, WhaleProvider, OnChainProvider, ETFProvider, MacroProvider,
NetworkProvider, LiquidityProvider`. An adapter declares `id`, `interfaces`, `key_setting` (env var holding
its key, or none), `min_interval_s`, and implements `async fetch(client, now) -> ProviderResult`.

A `ProviderResult` contains only canonical metrics. Swapping CoinGlass for Velo means writing one adapter
that emits the same canonical names; no engine changes. `providers/registry.py` is the only place adapters
are wired.

Failure semantics (collector):
- `unconfigured` — key missing; adapter never called; contributes nothing.
- `disabled` — admin switched it off; cached data dropped.
- `blocked` — reachable but refuses automation (e.g. Farside's Cloudflare challenge); no scraping workaround.
- `error` — exception; the last good result is kept **with its original timestamps** so its freshness decays
  to zero instead of being re-stamped. Failing providers back off exponentially (max 30 min).
- Reliability = catalog base × (0.5 + 0.5 × recent success rate), halved after 3 consecutive errors.

## Source weighting

```
SOURCE CONTRIBUTION = raw_signal × relevance × reliability × freshness × confidence
```

- **relevance** — per source, from the catalog (`btc_relevance` label → 0..1). Sources marked non-directional
  (Solana scanners, token-risk tools, ETH-only data) have relevance 0 and can never move the BTC score.
- **reliability** — dynamic, from provider health.
- **freshness** — per canonical metric: 1.0 up to its expected interval, linear decay to 0 at max age.
- **confidence** — per-signal (e.g. the smart-money signal scales with how many accounts hold BTC).

Engine confidence comes from cross-source confirmation (`engines/base.combine`): ≥3 agreeing sources with
≥75 % weighted agreement = STRONG, 2 = MODERATE, otherwise WEAK, which caps confidence (whale data from a
single feed is capped at 0.4 by rule).

## Security & honesty constraints

- No key is in source; every key is an env var (`BTCS_*`). Admin write endpoints require `BTCS_ADMIN_TOKEN`
  and are disabled when it is unset.
- A provider is shown "ok"/live only after a successful fetch in this process. Keyed adapters written from
  documentation are labelled unverified until a key is supplied and a fetch succeeds.
- Missing data is never imputed. Engines without data report `available: false` and score 0, coverage falls,
  and confidence/data quality fall with it.
- AI does not decide. The explanation is a deterministic rendering of engine reasons. An LLM may later be
  used to rephrase it, but its output can never feed back into direction or trade status.

## Repository layout

```
btc-signal/
  backend/
    app/
      config.py              settings (env)
      core/context.py        MarketContext, Datum, Series, Candle
      normalize/             canonical metric registry, freshness, cross-provider consensus
      providers/             catalog (40 + primary), adapters, collector, liquidation websockets, registry
      engines/               17 directional engines + data quality
      scoring/               config (weights/thresholds/gates), decision pipeline
      backtest/              outcome math, live evaluator, history fetchers, walk-forward replay
      learning/              recommended-weight computation (never auto-applied)
      db/                    ORM models, session (Timescale setup), repository
      service.py             cycle orchestration, cache, SSE fan-out
      main.py                FastAPI app
      worker.py              worker entry point
    tests/                   pytest suite (synthetic scenarios + mocked HTTP)
    scripts/gen_sources_doc.py
  frontend/                  Next.js 16 + TypeScript + Tailwind 4
  docs/                      this file, SIGNAL_SPEC.md, SOURCES.md, schema.sql
  docker-compose.yml
```
