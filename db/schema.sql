-- candles schema for intraday OHLCV
-- Uses timestamptz for UTC timestamps and a composite primary key

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

create index if not exists candles_symbol_interval_time_idx
    on public.candles(symbol, interval, open_time desc);

-- -------- FX 3-Agent full stack (see docs/FULL_STACK_SPEC.md) --------

-- Optional: per-symbol, per-time features (Agent 1)
create table if not exists public.features (
    id bigint generated always as identity primary key,
    symbol text not null,
    ts timestamptz not null,
    feature_json jsonb not null,
    inserted_at timestamptz not null default now()
);
create index if not exists features_symbol_ts_idx on public.features(symbol, ts desc);

-- Signal table: output of Agent 1 (base + delta)
create table if not exists public.signals (
    id bigint generated always as identity primary key,
    symbol text not null,
    ts timestamptz not null,
    direction smallint not null check (direction in (-1, 0, 1)),
    score numeric not null,
    confidence numeric,
    base_signal numeric,
    news_delta numeric,
    meta jsonb,
    inserted_at timestamptz not null default now()
);
create index if not exists signals_symbol_ts_idx on public.signals(symbol, ts desc);

-- Risk limits (Agent 2 reads)
create table if not exists public.risk_config (
    id bigint generated always as identity primary key,
    key text unique not null,
    value_json jsonb not null,
    updated_at timestamptz not null default now()
);

-- Optional promotion gates for paper -> live -> margin progression
create table if not exists public.promotion_gates (
    id bigint generated always as identity primary key,
    gate_name text unique not null,
    phase text not null check (phase in ('paper','live','margin')),
    is_enabled boolean not null default true,
    threshold_json jsonb not null,
    updated_at timestamptz not null default now()
);

-- Proposed orders: Agent 2 output, Agent 3 input
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
    reject_reason text,
    risk_checks jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists proposed_orders_status_idx on public.proposed_orders(status);

-- Orders sent to broker (Agent 3)
create table if not exists public.orders (
    id bigint generated always as identity primary key,
    proposed_order_id bigint references public.proposed_orders(id),
    broker_order_id text,
    symbol text not null,
    side text not null,
    size numeric not null,
    filled_size numeric default 0,
    avg_fill_price numeric,
    expected_price numeric,
    slippage_bps numeric,
    fee_amount numeric default 0,
    fee_currency text,
    status text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Open positions (from broker / Agent 3)
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

-- Agent 3 execution quality events (used by margin promotion gates)
create table if not exists public.execution_events (
    id bigint generated always as identity primary key,
    ts timestamptz not null default now(),
    symbol text,
    event_type text not null check (event_type in ('sent','rejected','filled','risk_blocked','session_blocked')),
    proposed_order_id bigint references public.proposed_orders(id),
    order_id bigint references public.orders(id),
    reason text,
    meta jsonb
);
create index if not exists execution_events_ts_idx on public.execution_events(ts desc);
create index if not exists execution_events_type_ts_idx on public.execution_events(event_type, ts desc);

