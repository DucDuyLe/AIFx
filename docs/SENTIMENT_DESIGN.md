---
title: Sentiment Design (Phase 2)
date: 2026-03-28
---

# Sentiment Design (Phase 2 — FinBERT from Day 1)

This document finalizes the sentiment approach for Phase 2.

## Decision

Use **FinBERT (`ProsusAI/finbert`) from the start** for article-level sentiment scoring.

- Local inference (CPU), zero API cost
- Better nuance than lexical scoring — handles financial language, negation, context
- One-time download (~400 MB model weights)
- Batch processing: ~50–200 articles/min on CPU (sufficient for historical backfill and 5m incremental)

## Article-Level Scoring

For each article in `news_raw`:

1. **Input text**: `headline + ' ' + summary` (fallback to `content[:512]` if summary missing)
2. **Model**: `ProsusAI/finbert` via HuggingFace `transformers` pipeline
3. **Output**: sentiment_score in `[-1, 1]`
   - FinBERT outputs 3 logits: positive, negative, neutral
   - `sentiment_score = positive_prob - negative_prob`
4. **Cache**: Store in `news_sentiment_cache` table
   - PK: `(provider, news_id)`
   - Columns: `sentiment_score`, `positive_prob`, `negative_prob`, `neutral_prob`, `model_version`, `scored_at`
   - Avoids re-scoring unchanged articles

## Relevancy Weighting

At aggregation time, weight each article's score by relevancy to the target symbol:

- **Exclusivity weight**: `1 / count(symbols tagged on article)`
  - An article tagged to 1 symbol gets weight 1.0; tagged to 5 symbols gets 0.2
- **Headline boost**: `1.5x` if target symbol appears in the headline text
- **Final relevancy weight**: `exclusivity * headline_boost`

Weighted aggregation:
- `weighted_mean = sum(score * relevancy) / sum(relevancy)`
- If no articles in window, use default (0.0)

## Window Aggregation

At each bar timestamp `ts` and symbol `s`, aggregate articles from `news_symbol_map` joined to `news_raw` and `news_sentiment_cache`:

- **W30**: `(ts - 30m, ts]`
- **W120**: `(ts - 2h, ts]`
- **W1D**: `(ts - 24h, ts]`

Features written to `feature_json`:

| Feature | Description | Default |
|---------|-------------|---------|
| `news_count_30m` | Count of tagged articles in 30m | `0` |
| `news_count_2h` | Count in 2h | `0` |
| `news_count_1d` | Count in 1d | `0` |
| `sent_mean_30m` | Relevancy-weighted mean sentiment in 30m | `0.0` |
| `sent_mean_2h` | Relevancy-weighted mean in 2h | `0.0` |
| `sent_mean_1d` | Relevancy-weighted mean in 1d | `0.0` |
| `sent_std_2h` | Stddev of sentiment scores in 2h | `0.0` |
| `sent_trend_2h` | mean(last 60m) - mean(previous 60m) | `0.0` |
| `headline_impact_max_1d` | max(abs(weighted_score)) over 1d | `0.0` |
| `time_since_last_news_min` | Minutes since latest tagged article | `99999.0` |

## Data-Quality Constraints

- Use only rows with `created_at <= ts` (no lookahead)
- If article has no mapped symbols in active universe, ignore
- Deduplicate via `news_raw` PK and `news_symbol_map` PK (already enforced)
- Articles without sentiment scores in cache are scored on-demand during pipeline run

## Schema Addition: `news_sentiment_cache`

```sql
CREATE TABLE IF NOT EXISTS public.news_sentiment_cache (
    provider       TEXT    NOT NULL,
    news_id        TEXT    NOT NULL,
    sentiment_score NUMERIC(6,4) NOT NULL,
    positive_prob  NUMERIC(6,4),
    negative_prob  NUMERIC(6,4),
    neutral_prob   NUMERIC(6,4),
    model_version  TEXT    NOT NULL DEFAULT 'ProsusAI/finbert',
    scored_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, news_id),
    FOREIGN KEY (provider, news_id) REFERENCES public.news_raw(provider, news_id)
);
```

## Pipeline Cadence

- **Historical backfill**: Score all `news_raw` articles → cache → build all feature rows
- **Incremental (5m cycle)**: Score only new/unscored articles → update cache → recompute affected windows

## Dependencies

```
transformers>=4.40
torch>=2.2
```

These are added to `requirements.txt` when implementation begins.
