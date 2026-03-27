---
title: Full-Stack Spec — SPAI500 Trading System
date: 2026-03-28
---

# Full-Stack Spec: SPAI500 Trading System (v3)

This doc describes the full stack for the SPAI500 automated intraday stock trading system: deterministic feature pipeline, LLM-powered agents, broker integration, and hard-cap enforcement.

Canonical references:
- `docs/SERVICES_AND_ROADMAP.md` — provider choices and phased rollout
- `docs/AGENT_SPEC_AND_IO.md` — per-layer inputs, outputs, cadence, token budgets
- `docs/OPENROUTER_MODEL_SHORTLIST.md` — per-agent model matrix with costs
- `docs/FEATURE_SPEC_V1.md` — feature contract for `features.feature_json`
- `docs/SENTIMENT_DESIGN.md` — FinBERT sentiment pipeline design

---

## 1. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  FRONTEND (Next.js — future)                                                │
│  Dashboard • Charts • Positions • Signals • Risk limits • Kill switch      │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  BACKEND API (FastAPI)                                                       │
│  /market/*  /signals/*  /risk/*  /orders/*  /positions/*  /config/*          │
└─────────────────────────────────────────────────────────────────────────────┘
          │                │                │                │
          ▼                ▼                ▼                ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ PostgreSQL17 │  │    Redis     │  │  Alpaca APIs │  │  OpenRouter  │
│  (state DB)  │  │  (later)     │  │  (data+exec) │  │  (LLM)      │
└──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘
          ▲                ▲                ▲                ▲
          │                │                │                │
┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                              │
│  FEATURE PIPELINE (deterministic Python + FinBERT, no OpenRouter cost)      │
│  candles_5m + news_raw → technical indicators + sentiment → features table  │
│                                                                              │
│  SIGNAL AGENT (LLM) → reads features → produces signals                    │
│  RISK AGENT (LLM) → reads signals + account state → proposed_orders        │
│  EXECUTION LAYER (mostly deterministic) → send-time checks → broker        │
│  HARD-CAP LAYER (deterministic) → 3u/trade, 7u/day, consec-loss cooldown  │
│                                                                              │
│  POST-LOSS REVIEWER (async LLM) — triggers after halt / EOD               │
│  STRATEGY RESEARCHER (async LLM, optional)                                  │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Key architectural principle**: Deterministic work (feature computation, FinBERT sentiment, risk enforcement) is handled by code pipelines. LLM agents handle only the tasks that benefit from reasoning: signal interpretation, trade selection/sizing, loss review, and strategy research.

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
| LLM | OpenRouter (per-agent models) | GPT-4.1 (Signal/Risk), Claude Sonnet (Post-Loss), Gemini Flash (fallback) |
| Deploy | Local-first | Self-host; Vercel (frontend) + Railway (backend) later |

---

## 3. Database Schema (v2 — 14+ Tables)

Full schema lives in `db/schema.sql`. Key tables by domain:

**Ingestion:**
- `instruments` — symbol universe (PK: symbol; asset_type, exchange, is_active)
- `candles_5m` — 5m OHLCV bars (PK: symbol, ts, feed; Alpaca fields: o/h/l/c/v/vw/n)
- `news_raw` — raw news articles (PK: provider, news_id)
- `news_symbol_map` — news ↔ symbol many-to-many
- `ingestion_runs` — job audit log
- `ingestion_errors` — dead-letter queue

**Feature pipeline outputs:**
- `features` — per-symbol, per-time features from deterministic pipeline
- `news_sentiment_cache` — per-article FinBERT scores (cached for reuse)

**Signal pipeline (LLM):**
- `signals` — Signal Agent output (direction, score, confidence, strategy_id, regime_tag)

**Risk & execution:**
- `risk_config` — global and per-symbol limits (key-value JSONB)
- `promotion_gates` — paper → live → margin progression gates
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
  - `features.feature_json` = flat JSON object of numeric/categorical features
- **Sentiment pipeline**
  - FinBERT (`ProsusAI/finbert`) scores each article (headline + summary)
  - Scores cached in `news_sentiment_cache`
  - Window aggregates (30m, 2h, 1d) with relevancy weighting joined into `feature_json`
- **Anti-lookahead rules**
  - At bar `ts`, only use candles/news with timestamps `<= ts`
  - No forward-fill from future bars
  - Session/window features are computed from data available at decision time
- **Signal Agent handoff contract**
  - Signal Agent reads only finalized `feature_set_version = 'v1'`
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

- Computes technical indicators (EMA, RSI, MACD, ATR, volume z-scores, etc.) from `candles_5m`
- Runs FinBERT sentiment scoring on `news_raw` articles (local inference, CPU)
- Aggregates sentiment into time windows with relevancy weighting
- Writes versioned rows to `features` table
- Cadence: runs after each 5m bar close (incremental) + historical backfill (one-time)

### Layer 1 — Signal Agent (LLM-powered)

- Reads pre-computed `features` for all active symbols
- Reasons about trade candidates across multiple strategies
- Produces directional signals with confidence, strategy attribution, and explanation
- Primary model: `openai/gpt-4.1`
- Cadence: every 5m bar close, after feature pipeline

### Layer 2 — Risk Agent (LLM-powered)

- Reads `signals` + account state + risk config + rolling performance stats
- Selects best actionable candidate(s), determines position sizing (0u–3u)
- Produces explicit "why this trade / why this size" reasoning
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
- Max 7u daily loss → halt_for_day
- Max consecutive losses (default: 3) → cooldown
- Pure code — no LLM, no override possible

### Async — Post-Loss Reviewer (LLM)

- Triggers after daily halt or EOD
- Analyzes losses, proposes rule/model/data improvements
- Primary model: `anthropic/claude-sonnet-4.5`

### Async — Strategy Researcher (optional, LLM)

- Rolling performance analysis, strategy ranking updates
- Can suggest changes but cannot bypass hard constraints

---

## 6. Where Layers Run

- **Option A — Inside the backend**: Pipeline + agents as FastAPI background tasks or APScheduler jobs. Simple; good for one server.
- **Option B — Separate workers**: Each layer as a separate Python process. Better for isolation.
- **Option C — Queue-driven**: Pipeline enqueues features → Signal Agent consumes → Risk Agent → Execution. Best for reliability.

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

2. **Phase 2 — Feature Pipeline + Sentiment + Backtest** *(active)*
   - Feature pipeline: technical indicators → `features` table
   - FinBERT sentiment scoring → `news_sentiment_cache` + aggregates in `feature_json`
   - Feature QA: coverage, distributions, anomaly checks
   - Local backtesting: 3 strategy families with realistic costs

3. **Phase 3 — Signal Agent**
   - LLM reads features, produces `signals` rows
   - Strategy attribution and explanation in `meta`

4. **Phase 4 — Risk Agent**
   - LLM-driven ranking/sizing → `proposed_orders`
   - Store `risk_checks`, `reject_reason`, `reasoning_json`

5. **Phase 5 — Execution Layer + Hard-Cap**
   - Broker connector (Alpaca paper)
   - Send-time checks + optional LLM safety review
   - External hard-cap enforcement
   - Kill switch

6. **Phase 6 — Post-Loss Reviewer**
   - Trigger on daily halt or EOD
   - Generate review artifacts

7. **Phase 7 — Backend API + Frontend**
   - FastAPI routes for all domains
   - Next.js dashboard: signals, positions, P/L, risk config, kill switch

8. **Phase 8 — Go Live**
   - Switch Alpaca paper → live (micro-size)
   - Monitor execution realism (slippage, partial fills, rejects)

---

## 8. Summary

- **Full stack** = FastAPI backend + PostgreSQL 17 + deterministic feature pipeline (FinBERT + technical indicators) + LLM-powered agents (Signal, Risk, Post-Loss, Strategy Research) + Alpaca (data + news + execution) + OpenRouter (LLM routing) + external hard-cap layer + (future) Next.js dashboard
- **Feature Pipeline** computes all deterministic work (indicators + FinBERT sentiment) — no LLM cost
- **Signal Agent** reads features, reasons about direction/confidence/strategy → `signals`
- **Risk Agent** selects best trade, sizes it (0u–3u) with explicit reasoning → `proposed_orders`
- **Execution Layer** runs send-time checks → sends to broker
- **Hard-Cap Layer** enforces 3u/trade, 7u/day, consecutive-loss cooldown — not inside any agent
- **Post-Loss Reviewer** analyzes losses and proposes improvements (async)
- **Strategy Researcher** researches strategy performance (optional, async)
- Build order: DB/ingestion → features/sentiment → signals → sizing → execution → review → UI → live
