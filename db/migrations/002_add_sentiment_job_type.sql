-- Migration 002: add sentiment_scoring to ingestion_runs job_type CHECK
-- and create news_sentiment_cache table (if not exists, for idempotency).

BEGIN;

ALTER TABLE public.ingestion_runs
    DROP CONSTRAINT IF EXISTS ingestion_runs_job_type_check;
ALTER TABLE public.ingestion_runs
    ADD CONSTRAINT ingestion_runs_job_type_check
    CHECK (job_type IN (
        'bars_backfill','bars_realtime','news_backfill','news_stream','sentiment_scoring'
    ));

CREATE TABLE IF NOT EXISTS public.news_sentiment_cache (
    provider        TEXT        NOT NULL,
    news_id         TEXT        NOT NULL,
    sentiment_score NUMERIC(6,4) NOT NULL,
    positive_prob   NUMERIC(6,4),
    negative_prob   NUMERIC(6,4),
    neutral_prob    NUMERIC(6,4),
    model_version   TEXT        NOT NULL DEFAULT 'ProsusAI/finbert',
    scored_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, news_id),
    FOREIGN KEY (provider, news_id) REFERENCES public.news_raw(provider, news_id)
);

COMMIT;
