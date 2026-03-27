-- 001_initial_schema_down.sql (DOWN)
-- Reverses 001_initial_schema.sql — drops all v2 tables.
-- WARNING: destroys all data. Only use on dev/paper databases.

begin;

drop table if exists public.execution_events cascade;
drop table if exists public.orders cascade;
drop table if exists public.proposed_orders cascade;
drop table if exists public.positions cascade;
drop table if exists public.promotion_gates cascade;
drop table if exists public.risk_config cascade;
drop table if exists public.signals cascade;
drop table if exists public.features cascade;
drop table if exists public.ingestion_errors cascade;
drop table if exists public.ingestion_runs cascade;
drop table if exists public.news_symbol_map cascade;
drop table if exists public.news_raw cascade;
drop table if exists public.candles_5m cascade;
drop table if exists public.instruments cascade;

commit;
