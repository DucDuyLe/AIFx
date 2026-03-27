-- Seed defaults for stock-first intraday rollout (Phase 1 -> Phase 2)
-- Run after db/schema.sql (or db/migrations/001_initial_schema.sql)

-- Initial symbol universe
insert into public.instruments (symbol, asset_type, exchange, name) values
    ('SPY',  'etf',       'ARCA',   'SPDR S&P 500 ETF Trust'),
    ('AMD',  'us_equity', 'NASDAQ', 'Advanced Micro Devices Inc.'),
    ('AAPL', 'us_equity', 'NASDAQ', 'Apple Inc.'),
    ('MSFT', 'us_equity', 'NASDAQ', 'Microsoft Corporation'),
    ('NVDA', 'us_equity', 'NASDAQ', 'NVIDIA Corporation'),
    ('AMZN', 'us_equity', 'NASDAQ', 'Amazon.com Inc.')
on conflict (symbol) do update set
    asset_type = excluded.asset_type,
    exchange = excluded.exchange,
    name = excluded.name;

-- Risk config defaults
insert into public.risk_config (key, value_json) values
    ('trading_mode', '{"phase":"paper"}'::jsonb),
    ('live_trading_enabled', '{"enabled":false}'::jsonb),
    ('symbol_universe', '{"symbols":["SPY","AMD","AAPL","MSFT","NVDA","AMZN"]}'::jsonb),
    ('session_filter', '{"timezone":"America/New_York","start":"09:30","end":"16:00","skip_open_minutes":5,"skip_close_minutes":10}'::jsonb),
    ('max_risk_pct_per_trade', '{"value":0.35}'::jsonb),
    ('max_daily_loss_pct', '{"value":1.50}'::jsonb),
    ('max_open_positions', '{"value":4}'::jsonb),
    ('max_symbol_notional_pct', '{"value":20.0}'::jsonb),
    ('max_gross_exposure_pct', '{"value":100.0}'::jsonb),
    ('max_leverage', '{"value":1.0}'::jsonb),
    ('max_margin_utilization_pct', '{"value":25.0}'::jsonb),
    ('min_maintenance_margin_buffer_pct', '{"value":30.0}'::jsonb),
    ('auto_downsize_drawdown_trigger_pct', '{"value":2.0}'::jsonb),
    ('auto_downsize_factor', '{"value":0.5}'::jsonb),
    ('max_spread_bps', '{"value":8.0}'::jsonb),
    ('max_slippage_bps', '{"value":12.0}'::jsonb)
on conflict (key) do update set
    value_json = excluded.value_json,
    updated_at = now();

insert into public.promotion_gates (gate_name, phase, is_enabled, threshold_json) values
    (
        'paper_to_live',
        'paper',
        true,
        '{
            "min_trades": 150,
            "min_days": 20,
            "min_net_expectancy_r": 0.05,
            "max_drawdown_pct": 4.0,
            "max_reject_rate_pct": 5.0,
            "max_avg_slippage_bps": 10.0
        }'::jsonb
    ),
    (
        'live_to_margin',
        'live',
        true,
        '{
            "min_trades": 120,
            "min_days": 20,
            "min_net_expectancy_r": 0.07,
            "max_drawdown_pct": 3.5,
            "max_reject_rate_pct": 4.0,
            "max_avg_slippage_bps": 8.0,
            "max_fee_to_gross_profit_ratio": 0.35
        }'::jsonb
    )
on conflict (gate_name) do update set
    phase = excluded.phase,
    is_enabled = excluded.is_enabled,
    threshold_json = excluded.threshold_json,
    updated_at = now();
