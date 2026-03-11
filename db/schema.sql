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


