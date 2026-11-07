# Signal-engine specification (v2)

Indicators are evidence; they are not the decision.

```
indicators (raw value → score −100..+100, signal, confidence, explanation, timestamp)
   → engines (one view of the market each, e.g. "momentum" = RSI + MACD on 1h/4h)
      → categories (TREND, MOMENTUM, FLOW, DERIVATIVES, POSITIONING, ON-CHAIN, MACRO, VOLATILITY)
         → final score → direction
                        → trade quality (independent gates) → TRADE YES / NO
context engines (regime, support/resistance, divergence) modify interpretation, levels and confidence
```

Every indicator is an `IndicatorResult` (`app/indicators/result.py`) with `raw`, `score`, `signal`
(BULLISH ≥ +15, BEARISH ≤ −15, else NEUTRAL), `confidence`, `explanation`, `ts`, `timeframe`. The API returns
them under `components.<engine>.indicators`. Engines whose inputs are provider readings rather than
indicator math expose one indicator record per source.

## Indicators (spec §27)

| § | Indicator | Calculation | Interpretation | Where |
|---|---|---|---|---|
| A | EMA trend | EMA 20/50/100/200 (SMA-seeded) | P>20>50>200 → +80; reverse → −80; otherwise the sign sum of the stack ×60/4; EMA20×50 cross within 3 bars ±20 (momentum, never a trade alone) | `technical.ema_trend` |
| A | ADX(14) | Wilder +DI/−DI/DX/ADX | ADX>25 trend meaningful, <20 ranging; trend score ×0.5 when ranging, ×0.75 at 20–25 | `technical.adx`, `trend_bias` |
| B | RSI(14) | Wilder | 50–70 bullish momentum (0..+50), 30–50 bearish (0..−50). >70: TRENDING BULL +40 (can stay overbought), RANGE −40 (reversal warning), else +10. <30 mirrored | `technical.rsi` |
| C | MACD(12,26,9) | EMA line, signal, histogram | line>signal & hist rising +50; line<signal & hist falling −50; fading ±15; crossover ±20; zero-line ±10; acceleration reported | `technical.macd` |
| D | Volume | SMA20 of volume; ratio of last closed bar | price↑vol↑ (>1.2×) +25..+60; price↓vol↑ −25..−60; price↑vol↓ (<0.8×) weak rally +8; price↓vol↓ weak selling −8 | `technical.volume` |
| E | ATR(14) | Wilder ATR, ATR% = ATR/price | 30-day percentile → LOW/NORMAL/HIGH/EXTREME; used for entry zone, invalidation, stop distance, regime, confidence. No fixed % stops | `technical.atr` |
| F | Bollinger(20,2) | SMA, σ, bands, %B, bandwidth + percentile | breakout only with %B>1, band expansion and volume>1.2× (±40); in RANGE %B>1 → −20, <0 → +20; touching a band alone is **not** bearish/bullish; squeeze = bandwidth <20th pct | `technical.bollinger_bands` |
| G | Support/resistance | 1h/4h/1D pivots, previous day & week H/L, session VWAP, 30-day volume-profile nodes, liquidation clusters; levels within 0.3 % merged and strengths summed | nearest/major support & resistance; stops anchor to the nearest support/resistance; reward targets must have strength ≥ 3 | `technical.support_resistance` |
| H | VWAP | session (UTC day) VWAP on 5m; VWAP anchored at the last major 4h swing | price vs VWAP ±40 scaled by distance; ×1.25 when session CVD agrees, ×0.6 when it disagrees; anchored VWAP ±15 | `technical.vwap_bias` |
| I | CVD | taker-buy delta as % of volume, relative to the venue's own 30-day norm, spot and futures | price↑CVD↑ confirmed buying; price↑CVD↓ bearish divergence; price↓CVD↓ confirmed selling; price↓CVD↑ absorption. Never read alone | `technical.cvd_matrix` |
| J | Open interest | coin-denominated OI change (USD OI de-priced) vs price change, 4h/24h, per venue | matrix: new positions / short covering / new shorts / long liquidation; new longs counted "healthy" only with positive spot CVD and funding < 0.03 %, else halved | `derivatives.open_interest` |
| K | Funding | median across venues, 30-day percentile (Binance history), trend (last 3 vs prior 9) | moderate positive/negative = normal positioning (±10..25); **extremes shrink back toward 0** and raise crowding flags that block TRADE — they do not reverse direction | `derivatives.funding` |
| L | Liquidations | long/short USD 1h/4h, velocity (1h vs 4h rate), clusters, cascade | short liqs + spot buying +20 confirmation; long liqs + spot selling −20; disagreement with spot halves; big liquidations + OI falling → reset flag | `derivatives.liquidation` |
| M | Long/short ratio | account ratio per venue | only extremes: ≥2.0 downside-squeeze risk (negative), ≤0.6 upside-squeeze risk (positive); 0.75–1.33 is zero | `derivatives.long_short` |
| N | Whale flow | Whale Alert, classified by entity: whale→exchange, exchange→whale/cold storage, whale→whale, internal (same entity, exchange↔exchange, custodian/ETF/prime) | only exchange deposits (−) and withdrawals (+) count; internal and custodian moves are ignored; single source capped at 0.4 confidence | `flows.whale` |
| O | Exchange netflow | Netflow = inflow − outflow. Coin Metrics daily (free); CryptoQuant/Glassnode hourly (keyed) | z-scored against own history: daily 1d & 7d; hourly 1h/4h/24h. Positive (into exchanges) bearish | `chain.exchange_flow` |
| P | ETF flows | daily, 7-day, 30-day, persistence (inflow days − outflow days) | each day clipped to 2× median absolute daily flow so one outlier cannot dominate | `flows.etf` |
| Q | On-chain | MVRV, active addresses (Coin Metrics, free), SOPR & LTH supply (Glassnode, keyed), exchange reserves | **historical percentiles**, not fixed thresholds; extremes matter, middle damped; magnitude capped (slow context) | `chain.onchain` |
| R | Network | hash rate (7d vs 30d), difficulty (30d), mempool size & fee pressure, block height | low weight; only hash/difficulty trend is directional (±30 max); fees/mempool are context | `chain.network` |
| S | Structure | pivots on 1h/4h/1D: HH/HL vs LH/LL, break of structure, change of character, 20-bar range breakout/breakdown | weighted 1h 0.25 / 4h 0.40 / 1D 0.35 | `technical.structure` |
| T | Multi-timeframe | trend bias on 5m, 15m (short), 1h, 4h (medium), 1D (higher) | final = 0.45 HTF + 0.40 medium + 0.15 short; a short-term move against a strong (≥40) HTF with neutral/agreeing medium is labelled "pullback"/"bounce" and cannot flip the trend score | `market.trend` |
| U | Divergence | last two pivots on 1h and 4h vs RSI, MACD histogram, cumulative CVD, volume, OI | non-directional; each divergence against the call removes 5 confidence points (max 3) | `technical.divergences` |
| V | Regime | ADX(1h/4h), 4h EMA stack, ATR percentile, 72h CVD, exchange netflow, liquidations | TRENDING BULL/BEAR, RANGE, ACCUMULATION, DISTRIBUTION, TRANSITION, LIQUIDATION EVENT + HIGH/LOW VOLATILITY. RSI and Bollinger interpretation depends on it; TRANSITION (or a fresh 4h EMA cross, or a 1D-vs-4h conflict) blocks trades | `market.regime` |

Additional engines: spot flow (Coinbase premium vs its 10-day norm), smart money (Hyperliquid top-trader net
position), stablecoin liquidity, macro (DXY, US10Y, S&P 500, VIX), crypto regime (Fear & Greed extremes,
total market cap, DeFi TVL).

## Categories (W) — no double counting

| Category | Engines (intra-category weight) | Category weight |
|---|---|---|
| TREND | trend 12, structure 8 | 20 |
| MOMENTUM | momentum 10 | 10 |
| FLOW | spot flow 12, CVD 10, volume 6, VWAP 5, ETF 8 | 22 |
| DERIVATIVES | open interest 8, funding 6, liquidations 8 | 16 |
| POSITIONING | long/short 4, whales 10, smart money 3 | 12 |
| ON-CHAIN | exchange netflow 7, on-chain 7, network 3, stablecoin 4 | 10 |
| MACRO | macro 5, crypto regime 2 | 6 |
| VOLATILITY | volatility 4 | 4 |

```
category_score = Σ w_e (0.5 + 0.5 conf_e) score_e / Σ w_e (0.5 + 0.5 conf_e)     over available engines
raw            = Σ W_c (0.5 + 0.5 conf_c) category_score_c / Σ …                  over available categories
coverage       = Σ W_c · (available intra-weight share)_c / Σ W_c
SCORE          = raw × coverage^0.5
```

Agreement and conflict are measured **across categories**, so EMA + MACD + RSI agreeing is one confirmation,
not three. Both weight sets are admin-editable and versioned.

## Direction

BULLISH ≥ +30, BEARISH ≤ −30, else NEUTRAL; downgraded to NEUTRAL (with the reason) if confidence < 35 or
data quality < 40.

## Confidence (0–100)

`100 × (0.28 agreement + 0.18 strength + 0.18 completeness + 0.12 freshness + 0.12 cross-source + 0.12 history)
− 25 × conflict − 5 × opposing divergences (max 3)`. History uses live win rates only with ≥ 30 samples.
HIGH ≥ 70, MEDIUM ≥ 45, LOW otherwise.

## Trade quality (X, Y) — every gate must pass

| Gate | Blocks when |
|---|---|
| direction | NEUTRAL |
| data_quality | DQ < 60 or a critical category (price, candles, derivatives) missing/stale |
| confidence | < 55 |
| agreement | < 60 % of category weight agrees |
| conflict | > 30 % of weight strongly (|score| ≥ 40) opposes — signals strongly conflict |
| crowding | funding/positioning dangerously crowded in the signal direction |
| volatility | regime EXTREME (ATR percentile > 97 % or DVOL > 85) |
| liquidation_cascade | cascade in progress |
| leverage | fast OI build (> 8 %/24h) with crowded funding in the signal direction |
| higher_timeframe | the call fights the 1D trend (|bias| ≥ 0.5) |
| regime | unclear transition regime |
| liquidity | Coinbase spread > 10 bps or 24h volume < 0.35× the 7-day average |
| risk | invalidation < 0.4 % or > 4 % away |
| reward_risk | distance to the next significant level < 1.5 × distance to invalidation |

So BULLISH + TRADE NO and BEARISH + TRADE NO are normal, explained outputs.

## Levels

Entry zone: bullish `[P − 0.5 ATR, P + 0.1 ATR]` (bearish mirrored). Invalidation: nearest support (resistance)
beyond the zone within 5 % − 0.25 ATR, else 2 ATR. Target: nearest level of strength ≥ 3 within 10 %.

## Backtesting every indicator (Z)

The walk-forward replay stores every indicator score, every category score and the final score at each
step. `indicator_stats` reports, for the full period and the out-of-sample TEST split, at a 4h horizon:
accuracy, precision and recall (bullish and bearish), expectancy, MFE/MAE, annualized Sharpe and maximum
drawdown. Steps overlap and samples are small, so Sharpe values are inflated and treated as relative only.

Weight learning (engine level) uses train IC confirmed on validation. Recommendations are never applied
without admin approval.

### Result as built (90 days to 2026-10-05, 535 steps)

41 indicators were scored. Accuracy ranged roughly from 40 % (1h ADX) to 61 % (on-chain tx count, 137 calls).
Spot CVD reached 55 % over 423 calls, and 1D structure and MVRV were around 52 %. Short-timeframe trend
(5m/15m) and 4h RSI were at or below a coin flip. The final score made 32 directional calls in 90 days. That is
**not enough evidence for any accuracy claim**: the system has not yet earned evidence-based weights, and the
defaults remain a prior.
