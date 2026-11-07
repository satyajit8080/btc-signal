# BTC SIGNAL

Answers two separate questions from live data:

1. **Is BTC bullish, bearish or neutral?** — signal score −100…+100, confidence 0–100, data quality 0–100
2. **Trade or no trade?** — a conservative gate check; when uncertain the answer is NO

The backend reads exchange, derivatives, on-chain, liquidity, macro and (when keyed) whale/ETF/exchange-flow
data, normalizes it, scores it through 17 engines with cross-source confirmation, and stores every signal
with its outcome at 15m–24h. The frontend shows one decision.

> Market intelligence, not financial advice. No result is guaranteed. See "Honest status" below.

## Quick start (no login, no API key, no paid plan)

Windows: double-click `start.bat`. macOS/Linux: `bash start.sh`. Then open http://localhost:3000.
Full instructions, server deployment and GitHub upload: **[DEPLOY.md](DEPLOY.md)**.

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — pipeline, processes, adapter architecture, weighting formula
- [docs/SOURCES.md](docs/SOURCES.md) — all 40 reference sources: API availability, access, relevance, metrics, engines (generated from the catalog)
- [docs/SIGNAL_SPEC.md](docs/SIGNAL_SPEC.md) — every engine, aggregation, confidence, trade gates, levels, backtest & learning
- [docs/schema.sql](docs/schema.sql) — PostgreSQL/TimescaleDB schema with retention policies

## Run locally (no Docker)

Backend (Python 3.12):

```bash
cd backend && python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
```

```bash
cd backend && BTCS_ADMIN_TOKEN=pick-a-local-token .venv/Scripts/python -m uvicorn app.main:app --port 8000
```

On macOS/Linux use `.venv/bin/` instead of `.venv/Scripts/`. SQLite is used by default (`btc_signal.db`). The first signal arrives after one cycle (~30–60 s).

Frontend (Node 20+):

```bash
cd frontend && npm install && npm run dev
```

Open http://localhost:3000. Set `NEXT_PUBLIC_API_URL` if the API is not on `http://localhost:8000`.

Tests:

```bash
cd backend && .venv/Scripts/python -m pytest --cov=app
```

## Run with Docker

```bash
cp .env.example .env
```

Edit `.env` (at minimum `POSTGRES_PASSWORD`, `BTCS_ADMIN_TOKEN`), then:

```bash
docker compose up --build -d
```

The stack runs TimescaleDB, Redis, the API (port 8000), a single worker that owns the collection cycle, and the web app (port 3000).

## API

| Method | Path | |
|---|---|---|
| GET | `/api/v1/btc/signal` | the decision (direction, score, trade, confidence, entry, invalidation, why) |
| GET | `/api/v1/btc/market` | price consensus, funding, OI, volatility inputs |
| GET | `/api/v1/btc/components` | every engine with sources, contributions and trade gates |
| GET | `/api/v1/btc/sources` | 40-source catalog with live status |
| GET | `/api/v1/btc/history?window=24h\|7d\|30d\|90d` | stored signals with outcomes |
| GET | `/api/v1/btc/performance` | live outcome statistics + latest backtest |
| GET | `/api/v1/btc/stream` | Server-Sent Events, one `signal` event per cycle |
| GET | `/api/v1/system/data-quality` · `/providers` · `/health` | |
| GET/PUT | `/api/v1/admin/config` | weights, thresholds, trade gates, engine params (versioned) |
| POST | `/api/v1/admin/providers/{id}` | enable/disable a provider |
| GET/POST | `/api/v1/admin/recommendations[/{id}/approve\|reject]` | recommended vs active weights |
| POST/GET | `/api/v1/admin/backtest` · `/admin/backtests` | run / list walk-forward backtests |
| POST | `/api/v1/admin/etf-flows` | import published ETF daily totals |
| GET | `/api/v1/admin/logs` · `/admin/signals/{id}` | |

Admin routes work without a token from the same machine. From anywhere else they need `Authorization: Bearer $BTCS_ADMIN_TOKEN`.

## Honest status (as built, 2026-10-06)

**Live without any key** — Binance spot & futures, Bybit, Hyperliquid (market + top-trader positions),
Coinbase, Kraken, Deribit (DVOL), Esplora (mempool.space with blockstream.info fallback), Blockchain.com
charts, DeFiLlama, alternative.me, CoinGecko, Yahoo (unofficial; reliability discounted), DexScreener,
GeckoTerminal, Coin Metrics community (MVRV, active addresses, exchange in/outflows and reserves — free), and
Binance + Bybit liquidation websockets. 19 of 21 directional engines run on free data (all but whales and ETF flows); 41+ indicators are computed
(EMA/ADX on 5 timeframes, RSI, MACD, volume, ATR, Bollinger, VWAP, CVD, structure, divergences, regime, S/R).

**Not live without action**
- Whales (Whale Alert), hourly exchange netflow and SOPR / LTH supply (CryptoQuant / Glassnode / Dune) and aggregated derivatives
  (CoinGlass) need keys. The adapters follow each provider's documentation but were not verified
  against a live response, because no key was available.
- ETF flows: Farside serves a Cloudflare challenge to automated clients, and the adapter reports
  `blocked` rather than working around it. Use a CoinGlass key, or import the published daily totals via
  the admin "ETF import" tab.
- Arkham, Nansen, Velo and CoinAnk are catalogued but have no adapter: their schemas can't be verified
  without access, and Velo and CoinAnk aggregate the same exchange APIs this engine already reads directly.
  Lookonchain, Cielo, Kolscan, FOMO, GMGN and InsightX have no public API.
- OKX timed out from the build machine's network. The adapter is tested against mocked payloads only.

**Backtest v1 (90 days to 2026-10-05, 4h steps, partial-engine replay).** On free history only, so
whale/ETF/exchange-flow/liquidation/smart-money are excluded:

| split | TRADE YES count | 4h win rate | 4h expectancy | 24h win rate |
|---|---|---|---|---|
| train | 22 | 36 % | −0.20 % | 9 % |
| validation | 5 | 60 % | −0.33 % | 0 % |
| test (out-of-sample) | 12 | 67 % | +0.21 % | 17 % |

These samples are far too small to support any accuracy claim. Trades held toward 24h mostly hit their
invalidation. **No edge has been demonstrated.** Treat the system as a disciplined market-condition
summary until live outcomes accumulate. The learning module recommended reducing the volatility and
crypto-regime weights; that recommendation is pending admin review, not applied.

**Per-indicator backtest (v2, 90 days, 4h horizon).** Most of the 41 indicators sit between 45 % and 55 %
directional accuracy: noise at this sample size. Spot CVD (55 % over 423 calls) and higher-timeframe structure
are mildly positive; 1h ADX and 4h RSI are mildly negative. The full table is in Admin → Backtest. See
[docs/SIGNAL_SPEC.md](docs/SIGNAL_SPEC.md) for every indicator's formula and rule.
