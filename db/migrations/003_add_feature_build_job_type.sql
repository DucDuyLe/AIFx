-- Migration 003: add feature_build to ingestion_runs job_type CHECK

BEGIN;

ALTER TABLE public.ingestion_runs
    DROP CONSTRAINT IF EXISTS ingestion_runs_job_type_check;
ALTER TABLE public.ingestion_runs
    ADD CONSTRAINT ingestion_runs_job_type_check
    CHECK (job_type IN (
        'bars_backfill','bars_realtime','news_backfill','news_stream',
        'sentiment_scoring','feature_build'
    ));

COMMIT;
