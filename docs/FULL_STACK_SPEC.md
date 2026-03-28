---
title: Full-Stack Spec — SPAI500 Trading System
date: 2026-03-29
---

# Full-Stack Spec: SPAI500 Trading System (v4)

This doc describes the full stack for the SPAI500 automated intraday stock trading system: deterministic feature pipeline, 22 strategy engine, LLM-powered agents, broker integration, hard-cap enforcement, and future ML upgrades.

Canonical references:
- `docs/SERVICES_AND_ROADMAP.md` — provider choices and phased rollout
- `docs/AGENT_SPEC_AND_IO.md` — per-layer inputs, outputs, cadence, token budgets
- `docs/OPENROUTER_MODEL_SHORTLIST.md` — per-agent model matrix with costs
- `docs/FEATURE_SPEC_V1.md` — feature contract for `features.feature_json` (42 keys)
- `docs/SENTIMENT_DESIGN.md` — FinBERT sentiment pipeline design

---

## 1. High-Level Architecture

```
+---------------------------------------------------------------------------------+
|  FRONTEND (Next.js — future)                                                    |
|  Dashboard - Charts - Positions - Signals - Risk limits - Kill switch           |
+---------------------------------------------------------------------------------+
                                      |
                                      v
+---------------------------------------------------------------------------------+
|  BACKEND API (FastAPI)                                                          |
|  /market/*  /signals/*  /risk/*  /orders/*  /positions/*  /config/*             |
+---------------------------------------------------------------------------------+
          |                |                |                |
          v                v                v                v
+--------------+  +--------------+  +--------------+  +--------------+
| PostgreSQL17 |  |    Redis     |  |  Alpaca APIs |  |  OpenRouter  |
|  (state DB)  |  |  (later)     |  |  (data+exec) |  |  (LLM)      |
+--------------+  +--------------+  +--------------+  +--------------+
          ^                ^                ^                ^
          |                |                |                |
+---------------------------------------------------------------------------------+
|                                                                                 |
|  FEATURE PIPELINE (deterministic Python + FinBERT, no OpenRouter cost)          |
|  candles_5m + news_raw -> 42 features -> features table                         |
|                                                                                 |
|  22 STRATEGY ENGINE (deterministic) -> trigger signals + confluence scores      |
|  [Future: Meta-Classifier (XGBoost) -> meta_confidence score]                   |
|  [Future: TFT Sidecar -> multi-horizon forecasts added to features]             |
|                                                                                 |
|  SIGNAL AGENT (LLM) -> reads features + triggers + headlines -> signals         |
|  RISK AGENT (LLM) -> reads signals + account state -> proposed_orders           |
|  [Future: DRL Shadow -> shadow sizing logged alongside LLM decisions]           |
|  EXECUTION LAYER (mostly deterministic) -> send-time checks -> broker           |
|  HARD-CAP LAYER (deterministic) -> 3u/trade, 7u/day, consec-loss cooldown      |
|                                                                                 |
|  POST-LOSS REVIEWER (async LLM) — triggers after halt / EOD                    |
|  STRATEGY RESEARCHER (async LLM, optional)                                      |
|                                                                                 |
+---------------------------------------------------------------------------------+
```

**Key architectural principles:**
- Deterministic work (features, sentiment, risk enforcement, strategy evaluation) is handled by code pipelines.
- LLM agents handle only reasoning tasks: signal interpretation, trade selection/sizing, loss review.
- Strategies are "pre-digested logic" — deterministic code does the math, LLM synthesizes and decides.
- Two-tiered sentiment: FinBERT for bulk numeric scoring, LLM reads raw headlines for contextual audit.
- Confluence scoring handles strategy interconnections; meta-classifier (future) learns optimal combinations.
- Hard caps are always outside any ML model — DRL, TFT, or LLM can never bypass them.

Two execution modes:
- **Auto**: Execution layer sends orders immediately when all checks pass.
- **Semi-auto**: Risk Agent creates `proposed_orders`; human approves before execution.

---

## 2. Tech Stack

| Layer | Choice | Notes |
|-------|--------|-------|
| Frontend | Next.js, Tailwind, shadcn/ui | Dashboard, risk controls, approvals (future) |
| Backend | Python, FastAPI | Async, Pydantic; routes for signals, risk, orders |
| Database | PostgreSQL 17 (local, port 5434) | DB: `spai500`. 14+ tables (schema v2) |
| Cache | Redis (optional, later) | Quote/news caching and throttling |
| Market Data | Alpaca Market Data API | 5m bars, IEX feed (free), SIP upgrade later |
| News | Alpaca News API | REST + WebSocket (Benzinga via Alpaca) |
| Sentiment | FinBERT (`ProsusAI/finbert`, local) | Article-level scoring, no API cost |
| Execution | Alpaca Trading API | Paper first, then live |
| LLM | OpenRouter (per-agent models) | GPT-4.1, DeepSeek V3.2, Claude Sonnet, Gemini |
| Future ML | TFT (local), XGBoost (local), SAC/DRL (Stable-Baselines3) | Phases B1/B2/C |
| Deploy | Local-first | Self-host; Vercel (frontend) + Railway (backend) later |

---

## 3. Database Schema (v2 — 14+ Tables)

Full schema lives in `db/schema.sql`. Key tables by domain:

**Ingestion:**
- `instruments` — symbol universe (PK: symbol; asset_type, exchange, is_active)
- `candles_5m` — 5m OHLCV bars (PK: symbol, ts, feed; Alpaca fields: o/h/l/c/v/vw/n)
- `news_raw` — raw news articles (PK: provider, news_id)
- `news_symbol_map` — news <-> symbol many-to-many
- `ingestion_runs` — job audit log
- `ingestion_errors` — dead-letter queue

**Feature pipeline outputs:**
- `features` — per-symbol, per-time features (42 keys in feature_json v1)
- `news_sentiment_cache` — per-article FinBERT scores (cached for reuse)

**Signal pipeline (LLM):**
- `signals` — Signal Agent output (direction, score, confidence, strategy_id, regime_tag)

**Risk & execution:**
- `risk_config` — global and per-symbol limits (key-value JSONB)
- `promotion_gates` — paper -> live -> margin progression gates
- `proposed_orders` — Risk Agent output (size_u, confidence_bucket, reasoning_json)
- `orders` — broker orders (execution layer sends, tracks fills/slippage)
- `positions` — open risk snapshot
- `execution_events` — append-only audit log

---

## 3.1 Feature Pipeline Data Products

The feature pipeline is deterministic Python code. It writes to the `features` table and reads from `candles_5m`, `news_raw`, `news_symbol_map`, and `news_sentiment_cache`.

- **Feature store contract**
  - `features.symbol` = ticker
  - `features.ts` = exact 5-minute boundary in UTC
  - `features.feature_set_version` = `v1`
  - `features.feature_json` = flat JSON object of 42 numeric/categorical features
- **Feature groups:** price/returns (5), trend/momentum (9), volatility (4), volume (4), daily levels (4), session (3), cross-sectional (1), regime (2), sentiment (10)
- **Sentiment pipeline**
  - Tier 1: FinBERT (`ProsusAI/finbert`) scores each article (headline + summary) -> cached in `news_sentiment_cache` -> window aggregates in `feature_json`
  - Tier 2: Signal Agent receives top 3-5 raw headlines at prompt time (contextual audit)
- **Anti-lookahead rules**
  - At bar `ts`, only use candles/news with timestamps `<= ts`
  - No forward-fill from future bars
  - Session/window features are computed from data available at decision time
- **Signal Agent handoff contract**
  - Signal Agent reads finalized `feature_set_version = 'v1'` + 22 strategy triggers + confluence
  - Missing sentiment windows must be explicit defaults (not nulls)
  - All timestamps are UTC and aligned to `:00/:05/:10/...`

---

## 4. Backend API Surface

- **Market**
  - `GET /api/market/candles?symbol=AAPL&interval=5m&from=&to=` — OHLCV from DB
  - `GET /api/market/quotes?symbols=AAPL,NVDA` — latest price (from Alpaca or cache)

- **Signals (Signal Agent output)**
  - `POST /api/signals` — insert signal rows
  - `GET /api/signals?symbol=&limit=` — for UI and Risk Agent

- **Risk (Risk Agent reads/writes)**
  - `GET /api/risk/config` — risk limits
  - `PUT /api/risk/config` — update limits (admin)
  - `GET /api/risk/account` — equity, open risk (for sizing)
  - `POST /api/risk/proposed-orders` — Risk Agent writes proposed orders

- **Orders & Execution (Execution Layer)**
  - `GET /api/orders/proposed?status=pending` — Execution polls
  - `PATCH /api/orders/proposed/{id}` — approve/reject/update status
  - `POST /api/orders/send` — Execution calls broker, records in `orders`
  - `GET /api/positions` — open positions

- **Control**
  - `POST /api/control/live-trading` — enable/disable execution (kill switch)

---

## 5. System Layers

### Layer 0 — Feature Pipeline (deterministic, no LLM)

- Computes 42 features (EMA, RSI, MACD, ATR, volume z-scores, daily levels, etc.) from `candles_5m`
- Runs FinBERT sentiment scoring on `news_raw` articles (local inference, CPU)
- Aggregates sentiment into time windows with relevancy weighting
- Writes versioned rows to `features` table
- Cadence: runs after each 5m bar close (incremental) + historical backfill (one-time)

### Layer 0.5 — 22 Strategy Engine (deterministic, no LLM)

- Evaluates 22 strategies per bar per symbol using `feature_json`
- Each strategy returns a trigger signal (1 = long, -1 = short, 0 = no trigger)
- Computes confluence scores (long_count, short_count, confluence_score)
- Tracks theoretical trigger outcomes for alpha decay detection
- Cadence: runs after feature pipeline completes

### Layer 1 — Signal Agent (LLM-powered)

- Reads features + strategy triggers + confluence + top raw headlines
- Reasons about trade candidates: synthesizes strategy triggers, news context, regime
- Produces directional signals with confidence, strategy attribution, and explanation
- Primary model: `openai/gpt-4.1` (budget: `deepseek/deepseek-v3.2`)
- Cadence: every 5m bar close, after strategy engine

### Layer 2 — Risk Agent (LLM-powered, DRL shadow alongside)

- Reads `signals` + account state + risk config + rolling performance stats
- Selects best actionable candidate(s), determines position sizing (0u-3u)
- Produces explicit "why this trade / why this size" reasoning
- DRL shadow (Phase C): logs shadow sizing for comparison, trains on real outcomes
- Primary model: `openai/gpt-4.1`
- Cadence: after Signal Agent

### Layer 3 — Execution Layer (mostly deterministic)

- Runs send-time checks: kill switch, session filter, spread/slippage, daily halt, idempotency
- Optional light LLM safety review before send
- Sends to Alpaca Trading API (paper first)
- Writes to `orders`, `positions`, `execution_events`

### Layer 4 — Hard-Cap Layer (deterministic, NOT an agent)

- Runs AFTER execution layer, before final broker submission
- Max 3u risk per trade
- Max 7u daily loss -> halt_for_day
- Max consecutive losses (default: 3) -> cooldown
- Pure code — no LLM, no override possible. DRL/TFT/meta-classifier cannot bypass.

### Async — Post-Loss Reviewer (LLM)

- Triggers after daily halt or EOD
- Analyzes losses, proposes rule/model/data improvements
- Includes alpha decay detection from theoretical trigger stats
- Primary model: `anthropic/claude-sonnet-4.5`

### Async — Strategy Researcher (optional, LLM)

- Rolling performance analysis, strategy ranking updates
- Can suggest changes but cannot bypass hard constraints

---

## 6. Where Layers Run

- **Option A — Inside the backend**: Pipeline + agents as FastAPI background tasks or APScheduler jobs. Simple; good for one server.
- **Option B — Separate workers**: Each layer as a separate Python process. Better for isolation.
- **Option C — Queue-driven**: Pipeline enqueues features -> Signal Agent consumes -> Risk Agent -> Execution. Best for reliability.

Start with **Option A**; move to B or C when needed.

---

## 7. Build Order (Phased)

1. **Phase 1 — DB + Ingestion** *(done)*
   - [x] Schema v2 (14 tables) applied to `spai500`
   - [x] Seed instruments + risk config
   - [x] Backfill 2 years of 5m bars (~285k rows)
   - [x] News ingestion (historical backfill, 6 months)
   - [ ] News ingestion (realtime websocket stream)
   - [x] Quality checks on bar data

2. **Phase 2 — Feature Pipeline + Sentiment + 22-Strategy Backtester** *(active)*
   - [x] Feature pipeline: 42 features -> `features` table
   - [x] FinBERT sentiment scoring -> `news_sentiment_cache`
   - [ ] Add 4 daily-level features (day_high, day_low, prev_day_high, prev_day_low)
   - [ ] Feature QA: coverage, distributions, anomaly checks
   - [ ] 22-strategy backtester with ATR-based exits, per-symbol tracking, confluence scoring, theoretical trigger tracking
   - [ ] Run backtests on walk-forward test split, review results

3. **Phase 3 — Signal Agent**
   - LLM reads features + 22 strategy triggers + confluence + raw headlines -> produces `signals`
   - Strategy attribution and explanation in `meta`

4. **Phase 4 — Risk Agent**
   - LLM-driven ranking/sizing -> `proposed_orders`
   - Store `risk_checks`, `reject_reason`, `reasoning_json`

5. **Phase 5 — Execution Layer + Hard-Cap**
   - Broker connector (Alpaca paper)
   - Send-time checks + optional LLM safety review
   - External hard-cap enforcement
   - Kill switch

6. **Phase 6 — Post-Loss Reviewer**
   - Trigger on daily halt or EOD
   - Generate review artifacts with alpha decay flags

7. **Phase B1 — Meta-Classifier** *(after backtest data exists)*
   - Train XGBoost/LightGBM on strategy trigger combinations + regime + outcomes
   - Output: meta_confidence score consumed by Signal Agent

8. **Phase B2 — TFT Sidecar** *(after architecture stable)*
   - Train Temporal Fusion Transformer on historical feature sequences
   - Output: multi-horizon forecasts added to feature pipeline

9. **Phase C — DRL Shadow** *(from day 1 of paper/live trading)*
   - SAC agent trains in shadow mode alongside LLM Risk Agent
   - Logs shadow sizing vs actual sizing vs outcome

10. **Phase 7 — Backend API + Frontend**
    - FastAPI routes for all domains
    - Next.js dashboard: signals, positions, P/L, risk config, kill switch

11. **Phase 8 — Go Live**
    - Switch Alpaca paper -> live (micro-size)
    - Monitor execution realism (slippage, partial fills, rejects)

12. **Phase D — DRL Active Promotion** *(manual decision after 500+ shadow trades)*
    - Promote DRL to active sizing if shadow record is convincing

---

## 8. Summary

- **Full stack** = FastAPI backend + PostgreSQL 17 + deterministic feature pipeline (42 features, FinBERT + technical indicators) + 22 deterministic strategy engine + LLM-powered agents (Signal, Risk, Post-Loss, Strategy Research via OpenRouter) + Alpaca (data + news + execution) + external hard-cap layer + (future) TFT sidecar + meta-classifier + DRL shadow + Next.js dashboard
- **Feature Pipeline** computes all deterministic work (42 features + FinBERT sentiment) — no LLM cost
- **22 Strategy Engine** evaluates deterministic strategies, produces trigger signals + confluence — "pre-digested logic" for the LLM
- **Signal Agent** reads features + triggers + headlines, reasons about direction/confidence/strategy -> `signals`
- **Risk Agent** selects best trade, sizes it (0u-3u) with explicit reasoning -> `proposed_orders`
- **DRL Shadow** (future) trains alongside, never controls until manually promoted
- **Execution Layer** runs send-time checks -> sends to broker
- **Hard-Cap Layer** enforces 3u/trade, 7u/day, consecutive-loss cooldown — not inside any agent, not bypassable by any model
- Build order: DB/ingestion -> features/sentiment/backtester -> signals -> sizing -> execution -> review -> ML upgrades -> UI -> live
