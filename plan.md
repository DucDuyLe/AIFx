## AIXS — AI-Powered Financial Agent MVP Plan

Tagline: Agentic analytics and predictive signals for modern markets.

### Team and Roles
- **Duc (Backend/LLM Lead)**: API design, data ingestion, models/LLM, infra
- **Thao (Frontend/UI Lead)**: Next.js app, UI/UX, charts, interactions, launch site

### Tech Stack
- **Frontend**: Next.js 15, Tailwind CSS, shadcn/ui, React Query, Framer Motion
- **Backend**: Python, FastAPI, async HTTP, pydantic
- **Database**: Postgres (Supabase/Neon) with TimescaleDB + pgvector
- **Cache**: Redis (Upstash)
- **Data Providers**: Binance (crypto intraday for prototyping), Polygon.io later for equities
- **CI/CD & Deploy**: GitHub → Vercel (frontend), Railway (backend)
- **Design**: Midjourney (concepts), Figma (wireframes → components)

### 6-Week Timeline (Milestones)
- **Week 1**: Foundation — repos, schema draft, design alignment
- **Week 2**: Data & API prototype — ingest OHLCV, serve candles, chart in UI
- **Week 3**: Prediction — simple model + LLM explanation, /api/predict wired to UI
- **Week 4**: Strategy & Backtest — /api/backtest, metrics, visualization
- **Week 5**: Agent & RAG — chat sidebar, pgvector search, polish
- **Week 6**: Test, deploy, launch — public demo, landing page, video

### Weekly Plan (Detailed)

#### Week 1 — Foundation & Design Alignment
- Define persona and top use cases (investor/analyst/student) — Owner: Both
- User journey: Landing → Dashboard → Stock View → Agent Chat — Owner: Both
- Generate UI moodboard in Midjourney; select direction — Owner: Thao
- Recreate best concept in Figma with shadcn/ui components — Owner: Thao
- Initialize repos (`frontend`, `backend`) and FastAPI skeleton — Owner: Duc
- Create Supabase project; connect locally — Owner: Duc

Deliverables: Figma wireframes, repo skeletons, /healthcheck live locally

#### Week 2 — Data Infrastructure & API Prototype
- Design DB schema: `users`, `symbols`, `candles` (hypertable), `signals` — Owner: Duc
- Implement Polygon.io ingestor; upsert into `candles` — Owner: Duc
- Expose `/api/market/candles` (tf, symbol, from/to) — Owner: Duc
- Build Symbol selector + OHLCV chart page; connect to API — Owner: Thao

Deliverables: Live endpoint returning real data; chart renders in UI

#### Week 3 — LLM + Prediction Core
- Define `/api/predict` contract (inputs/outputs) — Owner: Duc
- Implement simple model (LightGBM/LSTM) for daily horizon — Owner: Duc
- Add LLM agent with function-calling for reasoning/explanation — Owner: Duc
- Frontend Prediction card shows % change, confidence, reasoning — Owner: Thao
- Log predictions to `signals` table — Owner: Duc

Deliverables: Prediction + explanation visible in UI; signals persisted

#### Week 4 — Strategy & Backtest Engine
- Define `/api/backtest` contract — Owner: Duc
- Implement `strategy.py` (momentum, mean-reversion) + engine — Owner: Duc
- Persist metrics (ROI, Sharpe, drawdown) to DB — Owner: Duc
- Backtest UI: form, results chart, metrics table; add subtle animations — Owner: Thao

Deliverables: Backtests run end-to-end; results stored and visualized

#### Week 5 — Agent Flow + RAG + Polish
- Enable pgvector; ingest sample docs (news/filings) — Owner: Duc
- Build `/api/rag/search` endpoint — Owner: Duc
- Data Scout chat sidebar UX (send → thinking → response) — Owner: Thao
- Style consistency, responsive layout — Owner: Thao

Deliverables: Chat agent working; cohesive UI; performance sanity-checked

#### Week 6 — Testing, Deployment & Launch
- API tests (pytest with mocked providers) — Owner: Duc
- UI QA (desktop/mobile) — Owner: Thao
- Deploy backend to Railway; frontend to Vercel — Owners: Duc/Thao
- Record demo video; publish landing page; social launch — Owners: Both

Deliverables: Public demo URL, README, video walkthrough

### API Surface (Initial)
- `GET /api/market/candles?symbol=AAPL&tf=1d&from=YYYY-MM-DD&to=YYYY-MM-DD` — OHLCV
- `POST /api/predict` — { symbols: string[], horizon: "1d" | "1w" } → scores, confidence, reasoning
- `POST /api/backtest` — { strategy: string, universe: string[], params: json, from, to } → metrics, equity curve
- `POST /api/rag/search` — { query: string, k?: number } → passages with citations

### Database Sketch
- `users(id, email, name, created_at)`
- `symbols(symbol PK, asset_class, exchange, meta jsonb)`
- `candles(symbol FK, ts timestamptz PK, open, high, low, close, volume, source)` — Timescale hypertable
- `signals(id, project_id?, symbol, ts, score numeric, confidence numeric, meta jsonb)`
- `backtests(id, name, params jsonb, started_at, finished_at, metrics jsonb, artifact_url)`
- Vector/RAG tables (later): `finance_docs`, `finance_chunks(embedding vector)`

### Notion Import Template (Copy into a Notion Table)

| Week | Task | Owner | Priority | Status | Notes / References |
|---|---|---|---|---|---|
| 1 | Define target user persona & use cases | Both | 🔥 High | ☐ To-Do | Lean Canvas |
| 1 | Write high-level feature list | Duc | 🔥 High | ☐ To-Do | — |
| 1 | Sketch user journey (Landing → Dashboard → Stock → Chat) | Both | 🔥 High | ☐ To-Do | Miro/Whimsical |
| 1 | Generate UI moodboards via Midjourney | Thao | 🔥 High | ☐ To-Do | Prompt: fintech dashboard minimal tailwind |
| 1 | Recreate best design in Figma | Thao | 🔥 High | ☐ To-Do | shadcn/ui kit |
| 1 | Create GitHub repos (frontend, backend) | Duc | 🔥 High | ☐ To-Do | — |
| 1 | Initialize Next.js + Tailwind | Thao | 🔥 High | ☐ To-Do | Next.js Quickstart |
| 1 | Initialize FastAPI backend skeleton | Duc | 🔥 High | ☐ To-Do | FastAPI Tutorial |
| 1 | Set up Supabase & connect locally | Duc | ⚙️ Medium | ☐ To-Do | Supabase Setup |
| 2 | Design DB schema (users, symbols, candles, signals) | Duc | 🔥 High | ☐ To-Do | Timescale + pgvector |
| 2 | Implement Polygon.io ingestor | Duc | 🔥 High | ☐ To-Do | Polygon Docs |
| 2 | Test Supabase upserts | Duc | 🔥 High | ☐ To-Do | — |
| 2 | Create hypertable for candles | Duc | 🔥 High | ☐ To-Do | Timescale |
| 2 | Build Symbol selector + Chart page | Thao | 🔥 High | ☐ To-Do | Recharts/ECharts |
| 2 | Connect API to chart | Thao | 🔥 High | ☐ To-Do | — |
| 2 | Create .env and local setup guide | Duc | ⚙️ Medium | ☐ To-Do | — |
| 3 | Design `/api/predict` contract | Duc | 🔥 High | ☐ To-Do | — |
| 3 | Implement simple prediction model | Duc | 🔥 High | ☐ To-Do | LightGBM/LSTM |
| 3 | Build LLM agent with function-calling | Duc | 🔥 High | ☐ To-Do | OpenAI Functions |
| 3 | Frontend AI Prediction card | Thao | 🔥 High | ☐ To-Do | %change + confidence |
| 3 | Log predictions to DB (signals) | Duc | ⚙️ Medium | ☐ To-Do | — |
| 4 | Design `/api/backtest` I/O | Duc | 🔥 High | ☐ To-Do | — |
| 4 | Implement strategy.py + engine | Duc | 🔥 High | ☐ To-Do | backtesting.py |
| 4 | Backtest UI (form + chart + metrics) | Thao | 🔥 High | ☐ To-Do | — |
| 4 | Animate results (Framer Motion) | Thao | ⚙️ Medium | ☐ To-Do | Framer Motion |
| 4 | Cache backtests in Redis | Duc | ⚙️ Low | ☐ To-Do | Upstash |
| 5 | Integrate pgvector | Duc | 🔥 High | ☐ To-Do | pgvector docs |
| 5 | Build `/api/rag/search` | Duc | 🔥 High | ☐ To-Do | — |
| 5 | "Data Scout Chat" sidebar UI | Thao | 🔥 High | ☐ To-Do | Chat flow |
| 5 | Style overall UI + responsive | Thao | ⚙️ Medium | ☐ To-Do | — |
| 6 | Write API & UI tests | Duc | 🔥 High | ☐ To-Do | pytest |
| 6 | QA responsive design | Thao | 🔥 High | ☐ To-Do | DevTools |
| 6 | Deploy backend → Railway | Duc | 🔥 High | ☐ To-Do | Railway |
| 6 | Deploy frontend → Vercel | Thao | 🔥 High | ☐ To-Do | Vercel |
| 6 | Record demo video | Both | ⚙️ Medium | ☐ To-Do | Loom/OBS |
| 6 | Launch landing page | Thao | ⚙️ Medium | ☐ To-Do | Framer |
| 6 | Share on ProductHunt/LinkedIn | Both | ⚙️ Low | ☐ To-Do | — |

How to import:
1) Copy the table above.
2) In Notion, create a new page → Table (or Board).
3) Paste the table; set property types: Week (Number), Owner (Multi-select), Priority (Select), Status (Select or Checkbox).
4) Create Board view grouped by Week for sprints.

### References (Quick Links)
- FastAPI docs — [fastapi.tiangolo.com](https://fastapi.tiangolo.com)
- Next.js — [nextjs.org/docs](https://nextjs.org/docs)
- shadcn/ui — [ui.shadcn.com](https://ui.shadcn.com)
- TimescaleDB — [docs.timescale.com](https://docs.timescale.com)
- pgvector — [github.com/pgvector/pgvector](https://github.com/pgvector/pgvector)
- Polygon.io — [polygon.io/docs](https://polygon.io/docs)
- Binance Klines — [binance.com/en/binance-api](https://binance-docs.github.io/apidocs/spot/en/#kline-candlestick-data)

### Data Downloader Quickstart

- Install deps (PowerShell):

```bash
pip install -r requirements.txt
```

- Download 1 year of 1-minute BTCUSDT candles:

```bash
python scripts/binance_downloader.py --symbol BTCUSDT --interval 1m --days 365
```

- Download 5-minute ETHUSDT between specific dates (UTC):

```bash
python scripts/binance_downloader.py --symbol ETHUSDT --interval 5m --start 2024-01-01 --end 2025-01-01
```

Output CSVs are written to `data/` by default.

### Database: Supabase Postgres

- Create a Supabase project → copy the Postgres connection URI (Settings → Database → Connection string → URI). Ensure `sslmode=require`.
- Apply schema locally or via Supabase SQL editor:

```sql
-- file: db/schema.sql
create table if not exists public.candles (
    symbol text not null,
    interval text not null check (interval in ('1m','5m')),
    open_time timestamptz not null,
    open numeric not null,
    high numeric not null,
    low numeric not null,
    close numeric not null,
    volume numeric not null,
    close_time timestamptz not null,
    quote_asset_volume numeric,
    number_of_trades integer,
    taker_buy_base_volume numeric,
    taker_buy_quote_volume numeric,
    source text default 'binance' not null,
    inserted_at timestamptz not null default now(),
    constraint candles_pk primary key (symbol, interval, open_time)
);
```

### Ingest flow (AXS example)

1) Download 1y of 1-minute AXSUSDT (Binance):

```bash
python scripts/binance_downloader.py --symbol AXSUSDT --interval 1m --days 365
```

2) Set your `DATABASE_URL` in the shell (PowerShell example):

```bash
$env:DATABASE_URL="postgresql://user:pass@host:5432/dbname?sslmode=require"
```

3) Ingest the CSV:

```bash
python scripts/ingest_csv_to_postgres.py --csv data/AXSUSDT_1m_YYYY-MM-DD_YYYY-MM-DD.csv
```

Tip: Replace the placeholders in the CSV path with the actual dates from the generated filename.

### Equities: 5-minute bars for Top-200 (Polygon)

- Prepare a tickers file `data/tickers_top200.txt` (one per line, e.g., S&P 500 top names or your own list).
- Set `POLYGON_API_KEY` (see Polygon dashboard) and download 1y 5m bars:

```bash
$env:POLYGON_API_KEY="your_key"
python scripts/polygon_downloader.py --tickers-file data/tickers_top200.txt --interval 5m --days 365
```

- Ingest per-ticker CSVs into Postgres (override source):

```bash
$env:DATABASE_URL="postgresql://user:pass@host:5432/db?sslmode=require"
python scripts/ingest_csv_to_postgres.py --csv data/AAPL_5m_YYYY-MM-DD_YYYY-MM-DD.csv --source polygon
```

Repeat the ingest command for each CSV, or we can add a small helper later to bulk ingest a folder.

Generate tickers file automatically (first 200 from S&P 500):

```bash
python scripts/generate_sp500_tickers.py --out data/tickers_top200.txt --limit 200
```
- backtesting.py — [kernc.github.io/backtesting.py](https://kernc.github.io/backtesting.py)
- Vercel — [vercel.com/docs](https://vercel.com/docs)
- Railway — [railway.app](https://railway.app)

### Naming & Brand
Project codename: **AIXS** (sneaks "AI" into the name). Keep copy consistent across repo, app, and landing.


