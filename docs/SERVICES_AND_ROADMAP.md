---
title: Services and Roadmap (SPAI500)
date: 2026-03-28
---

# Services and Roadmap (Now + Future)

This doc is the single "what we're using" + "what we're building next" reference for SPAI500.

---

## Current Decision (v2)

### Instruments / Style

- **Primary**: intraday stocks using **5-minute OHLCV**
- **Universe**: SPY, AMD, AAPL, MSFT, NVDA, AMZN, QQQ
- **Reason**: 5m bars are fast enough for agents; no need for tick-level infra

### Execution Broker

- **Now**: **Alpaca Trading API (Paper)**
  - Paper endpoint: `https://paper-api.alpaca.markets`
  - Use this to build the execution state machine, approval flow, and risk enforcement safely.
- **Next**: **Alpaca Trading API (Live micro-size)**
  - Same API surface; change keys/base URL.

Notes:
- Paper trading is a simulation; it does **not** fully model queue position, latency slippage, price improvement, market impact, etc.
  - Reference: `https://docs.alpaca.markets/docs/paper-trading`

### Market Data

- **Now**: **Alpaca Market Data API (stocks, 5m bars)**
  - Historical bars: `GET /v2/stocks/bars` via `https://data.alpaca.markets`
  - Feed: `iex` (free tier); upgrade to `sip` ($99/mo) later for NBBO
  - Auth: same Alpaca key pair (`APCA-API-KEY-ID`, `APCA-API-SECRET-KEY`)

### News

- **Now**: **Alpaca News API** (source: Benzinga via Alpaca)
  - REST: `GET /v1beta1/news` via `https://data.alpaca.markets`
  - WebSocket: `wss://stream.data.alpaca.markets/v1beta1/news`
  - Supports per-symbol filtering, pagination, date range

Plan detail:
- Historical backfill done (6 months, ~10k articles).
- Realtime WebSocket stream: deferred until pipeline is stable.

### Sentiment Analysis

- **FinBERT** (`ProsusAI/finbert`) — local inference, CPU, no API cost
- Scores each news article (headline + summary) → cached in `news_sentiment_cache`
- Window aggregates (30m, 2h, 1d) with relevancy weighting → merged into `feature_json`
- This is a **pipeline tool**, not an LLM agent — no OpenRouter cost

### Feature Pipeline

- **Deterministic Python** — computes all technical indicators and sentiment aggregates
- Writes versioned rows to `features` table (`feature_set_version = 'v1'`)
- Anti-leakage: only data with timestamps `<= bar ts` used
- See `docs/FEATURE_SPEC_V1.md` for full contract

### LLM Provider (Agents)

- **Access**: OpenRouter (routing multiple models behind one API)
  - Models: `https://openrouter.ai/docs/guides/overview/models`
  - Pricing: `http://openrouter.ai/pricing`

Per-agent model assignments:
- Signal Agent: `openai/gpt-4.1` (~$2/$8 per 1M tokens)
- Risk Agent: `openai/gpt-4.1` (~$2/$8 per 1M tokens)
- Execution (optional LLM safety): `openai/gpt-4.1` or `google/gemini-2.5-flash`
- Post-Loss Reviewer: `anthropic/claude-sonnet-4.5` (~$3/$15 per 1M tokens)
- Strategy Researcher: `openai/gpt-4.1` (fallback chains per agent, see `docs/OPENROUTER_MODEL_SHORTLIST.md`)

Policy:
- LLM handles only reasoning tasks: signal interpretation, trade selection/sizing, loss review, strategy research.
- All deterministic work (features, sentiment, risk enforcement) is handled by code pipelines.
- LLM must **not** bypass hard risk caps or directly execute trades.

### Database

- **Now**: **PostgreSQL 17** (local install, port 5434)
  - DB name: `spai500`, user: `postgres`
  - Keep state in Postgres: `candles_5m`, `features`, `news_sentiment_cache`, `signals`, `proposed_orders`, `orders`, `positions`, `execution_events`, configs.

### Cache (optional)

- **Later**: Redis (only if needed)
  - Useful for throttling API calls and caching latest quotes/news fetches.

---

## "Now Plan" (What we do first)

## Next Active Phase

**Phase 2 — Feature Pipeline + Sentiment + Backtest**

### Phase 0 — Accounts + Keys (Paper) ✓

- [x] Create Alpaca Trading API paper keys
- [x] Verify Alpaca paper account working (100k paper balance)
- [x] Confirm Alpaca Market Data API access (5m bars fetched for AAPL/SPY)
- [x] Confirm Alpaca News API access
- [x] Set up OpenRouter API key
- [x] Decide initial universe: SPY, AMD, AAPL, MSFT, NVDA, AMZN, QQQ
- [ ] Regenerate API keys (exposed in terminal output)

### Phase 1 — Data Ingestion + Schema ✓

- [x] Schema overhaul — rewrite `db/schema.sql` (Binance-era -> Alpaca fields)
- [x] Fetch Alpaca news + asset samples to confirm field mapping
- [x] Load schema into `spai500` database
- [x] Build direct-to-DB ingestion (Python -> Alpaca REST -> PostgreSQL)
- [x] Backfill 2 years of 5m bars for active universe (~285k rows)
- [x] News ingestion — historical backfill (6 months, ~10k articles)
- [ ] News ingestion — realtime WebSocket stream (deferred)
- [x] Quality checks (gaps, duplicates, bad OHLCV, completeness per symbol/day)
- [x] Create `.env.example` with all env vars (no real values)

### Phase 2 — Feature Pipeline + Sentiment + Backtest *(active — planning)*

- [ ] Freeze Feature Spec v1 keys, defaults, and anti-leakage rules
- [ ] Finalize FinBERT sentiment pipeline design (article scoring + relevancy weighting)
- [ ] Add `news_sentiment_cache` table to schema
- [ ] Build deterministic feature pipeline (Python → `features` table)
- [ ] Build FinBERT sentiment scoring pipeline (Python → `news_sentiment_cache`)
- [ ] Run historical feature build for full range
- [ ] Feature QA: coverage, distributions, anomaly checks
- [ ] Local backtesting: 3 strategy families with realistic costs + walk-forward
- [ ] Decide go/no-go for Signal Agent readiness

### Phase 3 — Signal Agent (LLM)

- [ ] Signal Agent reads `features`, produces `signals` rows
- [ ] Strategy attribution and explanation in `signals.meta`
- [ ] Run on 5m bar close schedule

### Phase 4 — Risk Agent (LLM)

- [ ] Risk Agent reads `signals` + account state → `proposed_orders`
- [ ] LLM-driven sizing (0u–3u) with explicit reasoning
- [ ] Store `risk_checks`, `reject_reason`, `reasoning_json`

### Phase 5 — Execution Layer + Hard-Cap

Support both modes:
- **Semi-auto**: require approval before sending
- **Auto**: send if all checks pass

Send-time checks (deterministic):
- kill switch, session filter, spread/slippage, daily halt, idempotency
- Optional LLM safety review

External hard-cap layer (runs AFTER execution, before broker submission):
- Max 3u risk per trade, max 7u daily loss → halt, max consecutive losses → cooldown

Always write append-only audit events:
- `execution_events` (sent/rejected/filled/risk_blocked/session_blocked)

### Phase 6 — Post-Loss Reviewer (async LLM)

When daily loss reaches **7u**:
- halt new entries
- generate a review artifact:
  - what trades happened
  - what checks passed/failed
  - what market/news happened
  - what to change next (rule vs model vs data)

---

## "Future Plan" (After it works)

### Phase 7 — Live Micro-Size

- Switch Alpaca from paper → live
- Keep identical risk rules, smaller size
- Focus on execution realism (slippage, partial fills, rejects)

### Phase 8 — Scale Safely (Margin / More Symbols)

- Increase symbol count slowly
- Add margin only after gates are passed (stable drawdown + execution quality)
- Add promotion gates (paper → live → margin) as explicit config

### Phase 9 — Broker Upgrade (Optional)

If you need pro-grade execution/coverage later:
- **IBKR** for execution (more ops overhead: TWS/IB Gateway, restarts, permissions)
  - Initial setup reference: `https://interactivebrokers.github.io/tws-api/initial_setup.html`

### Phase 10 — News/Data Upgrades (Optional)

- Upgrade Alpaca feed from `iex` to `sip` for NBBO data
- Add higher-end news feeds if needed (Benzinga direct, Finnhub, Tiingo)
- Strategy Researcher for rolling performance analysis

---

## Final Decision Summary (one-liner)

**Alpaca (data + news + paper → live) + PostgreSQL 17 + FinBERT sentiment (local) + deterministic feature pipeline + LLM agents (Signal, Risk, Post-Loss, Strategy Research via OpenRouter) + strict 3u/7u risk caps + external hard-cap layer.**
