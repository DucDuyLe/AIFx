# Full-Stack Spec: Own Engine (Stock/FX Agent System)

This doc describes how to build **your own full stack** for an AI trading system: backend, DB, agents, and broker integration — no QuantConnect dependency.

Default rollout path for this repo:

1. **Phase 1:** Intraday liquid US stocks/ETFs in paper mode.
2. **Phase 2:** Add controlled margin after gates pass.
3. **Phase 3 (optional):** Add FX connectors and macro handling.

### 3-Agent vs 4-Agent Layout

We originally had **4 agents** in mind:

| # | Role | Does what |
|---|------|-----------|
| 1 | **Data / News** | Pulls prices + news from APIs (no scraping). Writes to `candles` / raw feeds. |
| 2 | **Model / Signal** | Reads data + optional “online models” (LLM, sentiment API). Outputs **signals** (base + news delta). |
| 3 | **Risk & Sizing** | Reads signals + account + risk_config. Outputs **proposed_orders** (size, SL, TP). |
| 4 | **Execution** | Reads proposed_orders, calls broker API, updates **orders** and **positions**. |

Because we use **APIs for data** (no separate scraping agent), the spec **merges 1 and 2** into one **“Data & Signal”** agent, giving **3 agents**:

- **Agent 1 — Data & Signal**: Data + news APIs → features → ranking/model → **signals** table.
- **Agent 2 — Risk & Sizing**: **proposed_orders**.
- **Agent 3 — Execution**: Broker API, **orders**, **positions**.

If you prefer the **4-agent** split, use: **Agent 1 = Data/News only** (writes candles/features), **Agent 2 = Model/Signal only** (reads features, writes signals), **Agent 3 = Risk & Sizing**, **Agent 4 = Execution**. The DB and API stay the same; you just split the first step into two jobs.

---

## 1. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  FRONTEND (Next.js)                                                          │
│  Dashboard • Charts • Positions • Signals • Risk limits • Manual override   │
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
│   Postgres   │  │    Redis     │  │  Data APIs   │  │  Broker API  │
│  (state DB)  │  │  (cache)     │  │  (prices,    │  │  (OANDA /    │
│              │  │              │  │   news)      │  │   cTrader)   │
└──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘
          ▲                ▲                ▲                ▲
          │                │                │                │
┌─────────────────────────────────────────────────────────────────────────────┐
│  AGENT LAYER (Python workers / cron / queues)                                │
│  3-agent: Data & Signal → Risk & Sizing → Execution                         │
│  4-agent: Data → Model/Signal → Risk & Sizing → Execution                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

- **Frontend**: Reads from your API only (no direct DB). Shows signals, positions, P/L, risk metrics, and a kill switch for live trading.
- **Backend**: Single source of truth. All agents read/write via the API or directly to the DB (you choose).
- **Agents**: Can be separate processes (e.g. cron jobs or queue workers) that call the backend or DB; or they can live inside the backend as scheduled tasks.

---

## 2. Tech Stack (Aligned With Your Plan)

| Layer        | Choice              | Notes |
|-------------|----------------------|--------|
| Frontend    | Next.js 15, Tailwind, shadcn/ui | Same as plan.md; add FX pair selector, risk dashboard. |
| Backend     | Python, FastAPI      | Async, Pydantic; add routes for signals, risk, orders. |
| Database    | Postgres (Supabase/Neon) | Extend schema with `signals`, `features`, `orders`, `positions`, `risk_config`. |
| Cache       | Redis (Upstash)      | Cache latest quotes, throttle external APIs. |
| Data        | Stock/FX price API + news API | Polygon/Alpaca for stocks, OANDA/cTrader for FX; news/sentiment as in plan (or LLM). |
| Execution   | Broker API          | **IBKR paper first** for stocks; OANDA/cTrader reserved for later FX phase. |
| Deploy      | Vercel (frontend), Railway (backend + workers) | Same as plan. |

---

## 3. Database Schema (Additions for FX 3-Agent Flow)

Keep your existing `candles` table; use it for stocks or FX with an `interval` like `5m`. Add these tables:

```sql
-- Optional: store per-symbol, per-time features (from Agent 1)
create table if not exists public.features (
    id bigint generated always as identity primary key,
    symbol text not null,
    ts timestamptz not null,
    feature_json jsonb not null,   -- e.g. { "returns_5m": 0.01, "vol_20": 0.02, "sentiment": 0.3 }
    inserted_at timestamptz not null default now()
);
create index features_symbol_ts_idx on public.features(symbol, ts desc);

-- Signal table: output of Agent 1 (and any “base + delta” model)
create table if not exists public.signals (
    id bigint generated always as identity primary key,
    symbol text not null,
    ts timestamptz not null,
    direction smallint not null check (direction in (-1, 0, 1)),   -- -1 short, 0 flat, 1 long
    score numeric not null,        -- e.g. -1 to 1 or 0–100
    confidence numeric,
    base_signal numeric,           -- optional: from price model
    news_delta numeric,             -- optional: from news/sentiment
    meta jsonb,
    inserted_at timestamptz not null default now()
);
create index signals_symbol_ts_idx on public.signals(symbol, ts desc);

-- Risk config: global and per-symbol limits (read by Agent 2)
create table if not exists public.risk_config (
    id bigint generated always as identity primary key,
    key text unique not null,       -- e.g. "max_risk_pct_per_trade", "daily_loss_limit_pct"
    value_json jsonb not null,
    updated_at timestamptz not null default now()
);

-- Proposed orders: output of Agent 2, input to Agent 3
create table if not exists public.proposed_orders (
    id bigint generated always as identity primary key,
    symbol text not null,
    side text not null check (side in ('buy','sell')),
    size numeric not null,
    order_type text not null default 'market',
    stop_loss numeric,
    take_profit numeric,
    signal_id bigint references public.signals(id),
    status text not null default 'pending' check (status in ('pending','approved','rejected','sent','filled','cancelled')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index proposed_orders_status_idx on public.proposed_orders(status);

-- Orders: what was actually sent to the broker (Agent 3)
create table if not exists public.orders (
    id bigint generated always as identity primary key,
    proposed_order_id bigint references public.proposed_orders(id),
    broker_order_id text,
    symbol text not null,
    side text not null,
    size numeric not null,
    filled_size numeric default 0,
    status text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Positions: snapshot of open risk (from broker or reconciled by Agent 3)
create table if not exists public.positions (
    id bigint generated always as identity primary key,
    symbol text not null,
    side text not null,
    size numeric not null,
    entry_price numeric,
    unrealized_pnl numeric,
    updated_at timestamptz not null default now(),
    unique(symbol)
);
```

You can add `account_snapshots` (equity, balance over time) later for risk and reporting.

---

## 4. Backend API Surface (Minimal for 3 Agents)

- **Market**
  - `GET /api/market/candles?symbol=EURUSD&interval=5m&from=&to=` — OHLCV (from DB or broker).
  - `GET /api/market/quotes?symbols=EURUSD,USDJPY` — latest bid/ask (cache or broker).

- **Signals (Agent 1 output)**
  - `POST /api/signals` — insert signal rows (used by Agent 1).
  - `GET /api/signals?symbol=&limit=` — for UI and Agent 2.

- **Risk (Agent 2 reads/writes)**
  - `GET /api/risk/config` — risk limits.
  - `PUT /api/risk/config` — update limits (admin).
  - `GET /api/risk/account` — equity, open risk (for sizing).
  - `POST /api/risk/proposed-orders` — Agent 2 writes proposed orders here.

- **Orders & Execution (Agent 3)**
  - `GET /api/orders/proposed?status=pending` — Agent 3 polls.
  - `PATCH /api/orders/proposed/{id}` — set status to approved/rejected/sent/filled.
  - `POST /api/orders/send` — Agent 3 calls broker and records in `orders`.
  - `GET /api/positions` — open positions (from broker or DB).

- **Control**
  - `POST /api/control/live-trading` — enable/disable live execution (kill switch).

---

## 5. The Three Agents in Detail

### Agent 1: Data & Signal (or split into Agent 1 + Agent 2)

- **Runs**: On a schedule (e.g. every 5 min) or triggered by a job queue.
- **Inputs**: FX price API (and optionally news/sentiment API or LLM).
- **Logic**:
  1. Pull latest candles for your universe (e.g. EURUSD, USDJPY, …).
  2. Compute features (returns, volatility, maybe sentiment) → write to `features` if you use that table.
  3. Run your **ranking / simple model** (e.g. trend + volatility score) and optionally add a **news delta**.
  4. Write one row per symbol to `signals` (symbol, ts, direction, score, confidence, base_signal, news_delta).
- **Output**: `signals` table updated. No orders yet.

**4-agent variant**: Agent 1 only does steps 1–2 (data + features). A separate **Agent 2 (Model/Signal)** does steps 3–4 (reads `features`, calls any online model, writes `signals`).

### Agent 2: Risk & Sizing

- **Runs**: After Agent 1 (e.g. 1 min later) or on the same schedule.
- **Inputs**: `signals` (latest per symbol), `risk_config`, account equity, current `positions`.
- **Logic**:
  1. Read risk limits (max % per trade, daily loss limit, max open risk, etc.).
  2. For each signal with |score| above threshold:
     - Compute position size (e.g. volatility-scaled, capped by risk %).
     - Compute stop_loss and take_profit (e.g. ATR-based).
  3. Insert rows into `proposed_orders` with status `pending`.
- **Output**: `proposed_orders` table. Still no broker calls.

### Agent 3: Execution & Monitoring

- **Runs**: On a short interval (e.g. every 30 s) or via queue.
- **Inputs**: `proposed_orders` where status = `pending` or `approved`, broker API, risk config.
- **Logic**:
  1. If live trading is disabled, skip or only log.
  2. Fetch `proposed_orders` with status `approved` (or auto-approve if you want full automation).
  3. For each order: check spread, slippage, daily loss limit again → then call broker API to place order.
  4. Update `proposed_orders.status` to `sent`, and insert into `orders` with `broker_order_id`.
  5. Periodically poll broker for fills and update `orders` and `positions`.
- **Output**: Real orders in the broker; DB tables `orders` and `positions` updated.

---

## 6. Where Agents Run (Options)

- **Option A — Inside the backend**: Implement Agent 1–3 as FastAPI background tasks or scheduled jobs (e.g. APScheduler). Simple; good for one server.
- **Option B — Separate workers**: Run each agent as a separate Python process (or Railway worker) that calls your FastAPI endpoints or DB. Better for scaling and isolation.
- **Option C — Queue-driven**: Agent 1 enqueues “new signals” → Agent 2 consumes and enqueues “proposed orders” → Agent 3 consumes and sends. Use Redis or SQS. Best for reliability and retries.

Start with **Option A**; move to B or C when you need it.

---

## 7. Build Order (Phased)

1. **Phase 1 — Backend + DB**
   - Extend `db/schema.sql` with the tables above; apply to Postgres.
   - Add FastAPI routes: `/api/market/candles`, `/api/signals`, `/api/risk/config`, `/api/orders/proposed`, `/api/positions`.
   - No agents yet; you can seed signals manually for testing.

2. **Phase 2 — Agent 1**
   - Connect to one FX data source (e.g. OANDA or your broker’s price API); persist candles.
   - Implement ranking model (e.g. simple trend + vol) and write to `signals`.
   - Run on a schedule (e.g. every 5 min).

3. **Phase 3 — Agent 2**
   - Read latest signals + risk_config + account state; compute size, SL, TP.
   - Write to `proposed_orders`. Optionally add a simple UI to approve/reject.

4. **Phase 4 — Agent 3 + Broker**
   - Implement broker connector (OANDA or cTrader) in **paper/demo** first.
   - Agent 3 reads `proposed_orders`, sends orders via connector, updates `orders` and `positions`.
   - Add kill switch (disable live trading in config).

5. **Phase 5 — Frontend**
   - Dashboard: signals, proposed orders, positions, P/L.
   - Risk config form and live-trading toggle.

6. **Phase 6 — Go live**
   - Switch to live broker account with small size; monitor and tighten risk limits.

---

## 8. Summary

- **Full stack** = your backend (FastAPI) + Postgres + Redis + 3 agents + broker API + (optionally) Next.js dashboard.
- **Agent 1** fills `signals` from data APIs + a simple ranking model (and optional news delta).
- **Agent 2** reads `signals` and risk config, writes `proposed_orders` (sizing + SL/TP).
- **Agent 3** reads `proposed_orders`, talks to the broker, updates `orders` and `positions`.
- Build in order: schema + API -> Agent 1 -> Agent 2 -> Agent 3 (paper) -> UI -> live micro-size -> controlled margin -> optional FX.

If you want, next step can be: **add the new tables to your repo’s `db/schema.sql`** and a **minimal FastAPI app** with the routes above so you can start implementing the agents.
