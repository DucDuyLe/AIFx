-- SPAI500 schema v2 — Alpaca-native, 5-agent architecture
-- PostgreSQL 17 | port 5434 | db: spai500
-- Clean rewrite: run on empty DB or DROP all first.

-- ============================================================
-- 1. INSTRUMENTS — symbol universe
-- ============================================================
create table if not exists public.instruments (
    symbol          text primary key,
    asset_type      text not null check (asset_type in ('us_equity','etf')),
    exchange        text,
    name            text,
    is_active       boolean not null default true,
    timezone        text not null default 'America/New_York',
    added_at        timestamptz not null default now()
);

-- ============================================================
-- 2. CANDLES_5M — primary price data (Alpaca 5m bars)
-- ============================================================
create table if not exists public.candles_5m (
    symbol          text not null references public.instruments(symbol),
    ts              timestamptz not null,
    feed            text not null default 'iex' check (feed in ('iex','sip')),
    open            numeric not null,
    high            numeric not null,
    low             numeric not null,
    close           numeric not null,
    volume          bigint not null,
    vwap            numeric,
    trade_count     integer,
    is_final        boolean not null default true,
    inserted_at     timestamptz not null default now(),

    constraint candles_5m_pk primary key (symbol, ts, feed),
    constraint candles_5m_ohlc_check check (
        high >= low and high >= open and high >= close
        and low <= open and low <= close
        and volume >= 0
    )
);

create index if not exists candles_5m_symbol_ts_idx
    on public.candles_5m (symbol, ts desc);

create index if not exists candles_5m_final_idx
    on public.candles_5m (symbol, ts desc)
    where is_final = true;

-- ============================================================
-- 3. NEWS_RAW — raw news articles
-- ============================================================
create table if not exists public.news_raw (
    provider        text not null default 'alpaca',
    news_id         text not null,
    headline        text not null,
    summary         text,
    content         text,
    source          text,
    url             text,
    author          text,
    created_at      timestamptz not null,
    updated_at      timestamptz,
    images_json     jsonb,
    inserted_at     timestamptz not null default now(),

    constraint news_raw_pk primary key (provider, news_id)
);

create index if not exists news_raw_created_at_idx
    on public.news_raw (created_at desc);

-- ============================================================
-- 4. NEWS_SYMBOL_MAP — news <-> symbol many-to-many
-- ============================================================
create table if not exists public.news_symbol_map (
    provider        text not null,
    news_id         text not null,
    symbol          text not null references public.instruments(symbol),

    constraint news_symbol_map_pk primary key (provider, news_id, symbol),
    constraint news_symbol_map_news_fk
        foreign key (provider, news_id) references public.news_raw(provider, news_id)
);

create index if not exists news_symbol_map_symbol_idx
    on public.news_symbol_map (symbol, provider, news_id);

-- ============================================================
-- 4b. NEWS_SENTIMENT_CACHE — FinBERT per-article scores
-- ============================================================
create table if not exists public.news_sentiment_cache (
    provider        text        not null,
    news_id         text        not null,
    sentiment_score numeric(6,4) not null,
    positive_prob   numeric(6,4),
    negative_prob   numeric(6,4),
    neutral_prob    numeric(6,4),
    model_version   text        not null default 'ProsusAI/finbert',
    scored_at       timestamptz not null default now(),

    primary key (provider, news_id),
    foreign key (provider, news_id) references public.news_raw(provider, news_id)
);

-- ============================================================
-- 5. INGESTION_RUNS — job audit log
-- ============================================================
create table if not exists public.ingestion_runs (
    id              bigint generated always as identity primary key,
    job_type        text not null check (job_type in (
                        'bars_backfill','bars_realtime','news_backfill','news_stream',
                        'sentiment_scoring','feature_build'
                    )),
    symbol          text,
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    status          text not null default 'running' check (status in (
                        'running','success','partial','failed'
                    )),
    rows_inserted   integer default 0,
    rows_skipped    integer default 0,
    error_message   text,
    meta            jsonb
);

create index if not exists ingestion_runs_type_started_idx
    on public.ingestion_runs (job_type, started_at desc);

-- ============================================================
-- 6. INGESTION_ERRORS — dead-letter queue
-- ============================================================
create table if not exists public.ingestion_errors (
    id              bigint generated always as identity primary key,
    run_id          bigint references public.ingestion_runs(id),
    symbol          text,
    ts              timestamptz,
    error_type      text not null,
    raw_payload     jsonb,
    error_message   text,
    created_at      timestamptz not null default now()
);

create index if not exists ingestion_errors_run_id_idx
    on public.ingestion_errors (run_id);

create index if not exists ingestion_errors_created_at_idx
    on public.ingestion_errors (created_at desc);

-- ============================================================
-- 7. FEATURES — per-symbol, per-time features (external pipeline)
-- ============================================================
create table if not exists public.features (
    id                  bigint generated always as identity primary key,
    symbol              text not null,
    ts                  timestamptz not null,
    feature_json        jsonb not null,
    feature_set_version text not null default 'v1',
    inserted_at         timestamptz not null default now()
);

create index if not exists features_symbol_ts_idx
    on public.features (symbol, ts desc);

-- ============================================================
-- 8. SIGNALS — Agent 1 output
-- ============================================================
create table if not exists public.signals (
    id              bigint generated always as identity primary key,
    symbol          text not null,
    ts              timestamptz not null,
    direction       smallint not null check (direction in (-1, 0, 1)),
    score           numeric not null,
    confidence      numeric,
    base_signal     numeric,
    news_delta      numeric,
    horizon         text,
    strategy_id     text,
    regime_tag      text,
    meta            jsonb,
    inserted_at     timestamptz not null default now()
);

create index if not exists signals_symbol_ts_idx
    on public.signals (symbol, ts desc);

create index if not exists signals_strategy_ts_idx
    on public.signals (strategy_id, ts desc);

-- ============================================================
-- 9. RISK_CONFIG — global and per-symbol limits (Agent 2 reads)
-- ============================================================
create table if not exists public.risk_config (
    id              bigint generated always as identity primary key,
    key             text unique not null,
    value_json      jsonb not null,
    updated_at      timestamptz not null default now()
);

-- ============================================================
-- 10. PROMOTION_GATES — paper -> live -> margin progression
-- ============================================================
create table if not exists public.promotion_gates (
    id              bigint generated always as identity primary key,
    gate_name       text unique not null,
    phase           text not null check (phase in ('paper','live','margin')),
    is_enabled      boolean not null default true,
    threshold_json  jsonb not null,
    updated_at      timestamptz not null default now()
);

-- ============================================================
-- 11. PROPOSED_ORDERS — Agent 2 output, Agent 3 input
-- ============================================================
create table if not exists public.proposed_orders (
    id                  bigint generated always as identity primary key,
    symbol              text not null,
    side                text not null check (side in ('buy','sell')),
    size                numeric not null,
    order_type          text not null default 'market',
    stop_loss           numeric,
    take_profit         numeric,
    signal_id           bigint references public.signals(id),
    status              text not null default 'pending' check (status in (
                            'pending','approved','rejected','sent','filled','cancelled'
                        )),
    reject_reason       text,
    risk_checks         jsonb,
    chosen_strategy     text,
    expected_edge_bps   numeric,
    regime_tag          text,
    size_u              numeric check (size_u >= 0 and size_u <= 3),
    size_reason_code    text,
    confidence_bucket   text,
    reasoning_json      jsonb,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

create index if not exists proposed_orders_status_idx
    on public.proposed_orders (status);

create index if not exists proposed_orders_signal_id_idx
    on public.proposed_orders (signal_id);

-- ============================================================
-- 12. ORDERS — sent to broker (Agent 3)
-- ============================================================
create table if not exists public.orders (
    id                  bigint generated always as identity primary key,
    proposed_order_id   bigint references public.proposed_orders(id),
    broker_order_id     text,
    symbol              text not null,
    side                text not null,
    size                numeric not null,
    filled_size         numeric default 0,
    avg_fill_price      numeric,
    expected_price      numeric,
    slippage_bps        numeric,
    fee_amount          numeric default 0,
    fee_currency        text,
    status              text not null,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

create index if not exists orders_proposed_order_id_idx
    on public.orders (proposed_order_id);

create index if not exists orders_broker_order_id_idx
    on public.orders (broker_order_id);

-- ============================================================
-- 13. POSITIONS — open risk snapshot
-- ============================================================
create table if not exists public.positions (
    id              bigint generated always as identity primary key,
    symbol          text not null unique,
    side            text not null,
    size            numeric not null,
    entry_price     numeric,
    unrealized_pnl  numeric,
    updated_at      timestamptz not null default now()
);

-- ============================================================
-- 14. EXECUTION_EVENTS — append-only audit log
-- ============================================================
create table if not exists public.execution_events (
    id                  bigint generated always as identity primary key,
    ts                  timestamptz not null default now(),
    symbol              text,
    event_type          text not null check (event_type in (
                            'sent','rejected','filled','risk_blocked','session_blocked'
                        )),
    proposed_order_id   bigint references public.proposed_orders(id),
    order_id            bigint references public.orders(id),
    reason              text,
    meta                jsonb
);

create index if not exists execution_events_ts_idx
    on public.execution_events (ts desc);

create index if not exists execution_events_type_ts_idx
    on public.execution_events (event_type, ts desc);

create index if not exists execution_events_proposed_order_id_idx
    on public.execution_events (proposed_order_id);

create index if not exists execution_events_order_id_idx
    on public.execution_events (order_id);
