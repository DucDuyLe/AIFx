#!/usr/bin/env python3
"""
Backfill historical news from Alpaca News API into news_raw + news_symbol_map.

Only fetches news tagged with symbols in our instruments table.

Usage:
    .venv\\Scripts\\python.exe scripts\\backfill_news.py
    .venv\\Scripts\\python.exe scripts\\backfill_news.py --days 90
    .venv\\Scripts\\python.exe scripts\\backfill_news.py --symbols SPY,AAPL --days 30
    .venv\\Scripts\\python.exe scripts\\backfill_news.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("backfill_news")

ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
NEWS_PER_PAGE = 50
DEFAULT_DAYS = 180
REQUEST_DELAY_S = 0.3
MAX_RETRIES = 3
RETRY_429_WAIT_S = 10


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill news from Alpaca into news_raw")
    p.add_argument(
        "--symbols",
        help="Comma-separated symbols (default: all active in instruments table)",
    )
    p.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"Number of calendar days to look back (default: {DEFAULT_DAYS})",
    )
    p.add_argument("--dry-run", action="store_true", help="Fetch but don't insert into DB")
    return p.parse_args()


def load_config() -> Dict[str, str]:
    env_path = Path(__file__).resolve().parent.parent / ".ENV"
    load_dotenv(env_path)
    required = ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        log.error("Missing env vars: %s", ", ".join(missing))
        sys.exit(1)
    if not os.getenv("PGPASSWORD"):
        log.error("PGPASSWORD not set in .ENV")
        sys.exit(1)
    return {
        "alpaca_key": os.environ["ALPACA_API_KEY"],
        "alpaca_secret": os.environ["ALPACA_SECRET_KEY"],
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


def get_active_symbols(conn: psycopg2.extensions.connection) -> List[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol FROM public.instruments WHERE is_active = true ORDER BY symbol"
        )
        return [row[0] for row in cur.fetchall()]


def fetch_news_page(
    symbols: List[str],
    start: str,
    end: str,
    api_key: str,
    api_secret: str,
    page_token: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Fetch one page of news from Alpaca. Returns (articles, next_page_token)."""
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }
    params: Dict[str, Any] = {
        "symbols": ",".join(symbols),
        "start": start,
        "end": end,
        "limit": NEWS_PER_PAGE,
        "include_content": "true",
        "sort": "asc",
    }
    if page_token:
        params["page_token"] = page_token

    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.get(ALPACA_NEWS_URL, headers=headers, params=params, timeout=30)

        if resp.status_code == 429:
            wait = RETRY_429_WAIT_S * attempt
            log.warning("Rate limited (429). Waiting %ds (attempt %d/%d)", wait, attempt, MAX_RETRIES)
            time.sleep(wait)
            continue

        resp.raise_for_status()
        data = resp.json()
        articles = data.get("news", [])
        npt = data.get("next_page_token")
        return articles, npt

    log.error("Max retries hit for news fetch")
    return [], None


NEWS_RAW_SQL = """
    INSERT INTO public.news_raw
        (provider, news_id, headline, summary, content, source, url, author,
         created_at, updated_at, images_json)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (provider, news_id) DO NOTHING
"""

NEWS_SYMBOL_MAP_SQL = """
    INSERT INTO public.news_symbol_map (provider, news_id, symbol)
    VALUES (%s, %s, %s)
    ON CONFLICT (provider, news_id, symbol) DO NOTHING
"""


def insert_news_batch(
    conn: psycopg2.extensions.connection,
    articles: List[Dict[str, Any]],
    valid_symbols: Set[str],
) -> Tuple[int, int]:
    """Insert articles into news_raw + news_symbol_map.
    Returns (articles_inserted, symbol_maps_inserted)."""
    if not articles:
        return 0, 0

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM public.news_raw")
        before_articles = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.news_symbol_map")
        before_maps = cur.fetchone()[0]

    with conn.cursor() as cur:
        for art in articles:
            news_id = str(art.get("id", ""))
            images = art.get("images") or []

            cur.execute(NEWS_RAW_SQL, (
                "alpaca",
                news_id,
                art.get("headline", ""),
                art.get("summary"),
                art.get("content"),
                art.get("source"),
                art.get("url"),
                art.get("author"),
                art.get("created_at"),
                art.get("updated_at"),
                json.dumps(images) if images else None,
            ))

            for sym in art.get("symbols", []):
                if sym in valid_symbols:
                    cur.execute(NEWS_SYMBOL_MAP_SQL, ("alpaca", news_id, sym))

    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM public.news_raw")
        after_articles = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.news_symbol_map")
        after_maps = cur.fetchone()[0]

    return after_articles - before_articles, after_maps - before_maps


def create_ingestion_run(
    conn: psycopg2.extensions.connection,
    meta: Dict[str, Any],
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.ingestion_runs (job_type, status, meta)
            VALUES ('news_backfill', 'running', %s)
            RETURNING id
            """,
            (json.dumps(meta),),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def finish_ingestion_run(
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


def log_ingestion_error(
    conn: psycopg2.extensions.connection,
    run_id: int,
    error_type: str,
    error_message: str,
    raw_payload: Optional[Dict] = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.ingestion_errors (run_id, error_type, error_message, raw_payload)
            VALUES (%s, %s, %s, %s)
            """,
            (run_id, error_type, error_message, json.dumps(raw_payload) if raw_payload else None),
        )
    conn.commit()


def main() -> None:
    args = parse_args()
    cfg = load_config()

    end_dt = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=args.days)
    start_str = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    log.info("News backfill range: %s to %s (%d days)", start_dt.date(), end_dt.date(), args.days)
    log.info("Dry run: %s", args.dry_run)

    conn = get_db_conn(cfg)

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    else:
        symbols = get_active_symbols(conn)

    if not symbols:
        log.error("No symbols found. Check instruments table or --symbols flag.")
        sys.exit(1)

    valid_symbols: Set[str] = set(symbols)
    log.info("Symbols filter: %s", ", ".join(symbols))

    run_meta = {
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "days": args.days,
        "symbols": symbols,
        "dry_run": args.dry_run,
    }

    run_id = None
    if not args.dry_run:
        run_id = create_ingestion_run(conn, run_meta)

    total_fetched = 0
    total_articles_inserted = 0
    total_maps_inserted = 0
    page = 0
    page_token: Optional[str] = None

    try:
        while True:
            page += 1
            articles, next_token = fetch_news_page(
                symbols, start_str, end_str,
                cfg["alpaca_key"], cfg["alpaca_secret"], page_token,
            )

            if not articles:
                break

            total_fetched += len(articles)

            if not args.dry_run:
                art_ins, map_ins = insert_news_batch(conn, articles, valid_symbols)
                total_articles_inserted += art_ins
                total_maps_inserted += map_ins
            else:
                art_ins = 0
                map_ins = 0

            first_date = articles[0].get("created_at", "?")[:10]
            last_date = articles[-1].get("created_at", "?")[:10]

            log.info(
                "  page %d: fetched=%d  articles_inserted=%d  maps_inserted=%d  "
                "range=%s..%s  (total fetched=%d)",
                page, len(articles), art_ins, map_ins,
                first_date, last_date, total_fetched,
            )

            if not next_token:
                break

            page_token = next_token
            time.sleep(REQUEST_DELAY_S)

        total_skipped = total_fetched - total_articles_inserted
        if run_id is not None:
            finish_ingestion_run(conn, run_id, "success", total_articles_inserted, total_skipped)

        log.info(
            "=== COMPLETE === fetched=%d  articles_inserted=%d  symbol_maps=%d  skipped=%d",
            total_fetched, total_articles_inserted, total_maps_inserted, total_skipped,
        )

    except Exception as exc:
        log.error("FAILED: %s", exc)
        if run_id is not None:
            log_ingestion_error(conn, run_id, "api_error", str(exc))
            finish_ingestion_run(conn, run_id, "failed", total_articles_inserted, 0, str(exc))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
