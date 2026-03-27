#!/usr/bin/env python3
"""
Score news articles using FinBERT and cache results in news_sentiment_cache.

Finds all unscored articles in news_raw (via LEFT JOIN on cache),
runs ProsusAI/finbert inference on headline+summary, and writes
sentiment_score / positive_prob / negative_prob / neutral_prob.

Usage:
    .venv\\Scripts\\python.exe scripts\\score_news_sentiment.py
    .venv\\Scripts\\python.exe scripts\\score_news_sentiment.py --limit 100
    .venv\\Scripts\\python.exe scripts\\score_news_sentiment.py --batch-size 16 --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("score_sentiment")

MODEL_NAME = "ProsusAI/finbert"
DEFAULT_BATCH_SIZE = 32


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Score news_raw articles with FinBERT")
    p.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Articles per FinBERT forward pass (default: {DEFAULT_BATCH_SIZE})",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max articles to score (0 = all unscored)",
    )
    p.add_argument("--dry-run", action="store_true", help="Load model, count articles, but don't write")
    return p.parse_args()


def load_config() -> Dict[str, str]:
    env_path = Path(__file__).resolve().parent.parent / ".ENV"
    load_dotenv(env_path)
    if not os.getenv("PGPASSWORD"):
        log.error("PGPASSWORD not set in .ENV")
        sys.exit(1)
    return {
        "db_host": os.getenv("PGHOST", "localhost"),
        "db_port": os.getenv("PGPORT", "5434"),
        "db_name": os.getenv("PGDATABASE", "spai500"),
        "db_user": os.getenv("PGUSER", "postgres"),
        "db_password": os.environ["PGPASSWORD"],
    }


def get_db_conn(cfg: Dict[str, str]) -> psycopg2.extensions.connection:
    return psycopg2.connect(
        host=cfg["db_host"],
        port=cfg["db_port"],
        dbname=cfg["db_name"],
        user=cfg["db_user"],
        password=cfg["db_password"],
    )


def load_finbert_pipeline():
    """Load ProsusAI/finbert. First call downloads ~400 MB; cached afterwards."""
    from transformers import pipeline as hf_pipeline

    log.info("Loading FinBERT model (%s)...", MODEL_NAME)
    t0 = time.time()
    pipe = hf_pipeline(
        "sentiment-analysis",
        model=MODEL_NAME,
        tokenizer=MODEL_NAME,
        device=-1,
        top_k=None,
    )
    elapsed = time.time() - t0
    log.info("FinBERT loaded in %.1fs", elapsed)
    return pipe


def get_unscored_articles(
    conn: psycopg2.extensions.connection,
    limit: int = 0,
) -> List[Tuple[str, str, str, Optional[str], Optional[str]]]:
    """Return (provider, news_id, headline, summary, content) for unscored articles."""
    sql = """
        SELECT nr.provider, nr.news_id, nr.headline, nr.summary, nr.content
        FROM public.news_raw nr
        LEFT JOIN public.news_sentiment_cache nsc
            ON nr.provider = nsc.provider AND nr.news_id = nsc.news_id
        WHERE nsc.provider IS NULL
        ORDER BY nr.created_at ASC
    """
    if limit > 0:
        sql += f" LIMIT {limit}"

    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def build_input_text(headline: str, summary: Optional[str], content: Optional[str]) -> str:
    """Assemble input for FinBERT: headline + summary, fallback to content[:512]."""
    if summary and summary.strip():
        return f"{headline} {summary.strip()}"
    if content and content.strip():
        return f"{headline} {content.strip()[:512]}"
    return headline


def score_batch(pipe, texts: List[str]) -> List[Dict[str, float]]:
    """Run FinBERT on a batch of texts.

    Returns list of dicts with sentiment_score, positive_prob, negative_prob, neutral_prob.
    """
    results = pipe(texts, truncation=True, max_length=512, batch_size=len(texts))

    scored = []
    for item_scores in results:
        probs = {entry["label"]: entry["score"] for entry in item_scores}
        pos = probs.get("positive", 0.0)
        neg = probs.get("negative", 0.0)
        neu = probs.get("neutral", 0.0)
        scored.append({
            "sentiment_score": round(pos - neg, 4),
            "positive_prob": round(pos, 4),
            "negative_prob": round(neg, 4),
            "neutral_prob": round(neu, 4),
        })
    return scored


INSERT_SQL = """
    INSERT INTO public.news_sentiment_cache
        (provider, news_id, sentiment_score, positive_prob, negative_prob, neutral_prob, model_version)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (provider, news_id) DO NOTHING
"""


def insert_scores(
    conn: psycopg2.extensions.connection,
    rows: List[Tuple],
) -> int:
    """Batch-insert scores. Returns number of rows actually inserted."""
    if not rows:
        return 0

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM public.news_sentiment_cache")
        before = cur.fetchone()[0]

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, INSERT_SQL, rows, page_size=100)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM public.news_sentiment_cache")
        after = cur.fetchone()[0]

    return after - before


def create_run(conn: psycopg2.extensions.connection, meta: Dict[str, Any]) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.ingestion_runs (job_type, status, meta)
            VALUES ('sentiment_scoring', 'running', %s)
            RETURNING id
            """,
            (json.dumps(meta),),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def finish_run(
    conn: psycopg2.extensions.connection,
    run_id: int,
    status: str,
    rows_inserted: int,
    rows_skipped: int,
    error_message: Optional[str] = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.ingestion_runs
            SET status = %s, finished_at = now(), rows_inserted = %s,
                rows_skipped = %s, error_message = %s
            WHERE id = %s
            """,
            (status, rows_inserted, rows_skipped, error_message, run_id),
        )
    conn.commit()


def main() -> None:
    args = parse_args()
    cfg = load_config()
    conn = get_db_conn(cfg)

    articles = get_unscored_articles(conn, limit=args.limit)
    total = len(articles)
    log.info("Unscored articles found: %d", total)

    if total == 0:
        log.info("Nothing to score. Exiting.")
        conn.close()
        return

    if args.dry_run:
        log.info("[DRY RUN] Would score %d articles. Loading model to verify...", total)
        pipe = load_finbert_pipeline()
        sample_text = build_input_text(articles[0][2], articles[0][3], articles[0][4])
        sample_result = score_batch(pipe, [sample_text])
        log.info("[DRY RUN] Sample score for first article: %s", sample_result[0])
        log.info("[DRY RUN] Done. No rows written.")
        conn.close()
        return

    run_meta = {
        "model": MODEL_NAME,
        "batch_size": args.batch_size,
        "total_unscored": total,
        "limit": args.limit,
    }
    run_id = create_run(conn, run_meta)

    pipe = load_finbert_pipeline()

    total_inserted = 0
    total_skipped = 0
    t_start = time.time()

    try:
        for batch_idx in range(0, total, args.batch_size):
            batch = articles[batch_idx : batch_idx + args.batch_size]
            texts = [build_input_text(row[2], row[3], row[4]) for row in batch]

            scores = score_batch(pipe, texts)

            db_rows = [
                (
                    row[0],
                    row[1],
                    s["sentiment_score"],
                    s["positive_prob"],
                    s["negative_prob"],
                    s["neutral_prob"],
                    MODEL_NAME,
                )
                for row, s in zip(batch, scores)
            ]

            inserted = insert_scores(conn, db_rows)
            skipped = len(batch) - inserted
            total_inserted += inserted
            total_skipped += skipped

            elapsed = time.time() - t_start
            done = batch_idx + len(batch)
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0

            log.info(
                "  batch %d: scored=%d  inserted=%d  skipped=%d  "
                "progress=%d/%d  rate=%.1f art/s  ETA=%.0fs",
                (batch_idx // args.batch_size) + 1,
                len(batch),
                inserted,
                skipped,
                done,
                total,
                rate,
                eta,
            )

        finish_run(conn, run_id, "success", total_inserted, total_skipped)
        elapsed = time.time() - t_start

        log.info(
            "=== COMPLETE === scored=%d  inserted=%d  skipped=%d  elapsed=%.1fs  rate=%.1f art/s",
            total,
            total_inserted,
            total_skipped,
            elapsed,
            total / elapsed if elapsed > 0 else 0,
        )

    except Exception as exc:
        log.error("FAILED: %s", exc)
        finish_run(conn, run_id, "failed", total_inserted, total_skipped, str(exc))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
