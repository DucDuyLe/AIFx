#!/usr/bin/env python3
"""
Backfill historical 5-minute bars from Alpaca Market Data API into candles_5m.

Usage:
    .venv\\Scripts\\python.exe scripts\\backfill_bars.py
    .venv\\Scripts\\python.exe scripts\\backfill_bars.py --symbols SPY,AAPL --days 365
    .venv\\Scripts\\python.exe scripts\\backfill_bars.py --dry-run
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
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("backfill_bars")

ALPACA_DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"
BARS_PER_PAGE = 10_000
DEFAULT_DAYS = 730
DEFAULT_FEED = "iex"
REQUEST_DELAY_S = 0.3
MAX_RETRIES = 3
RETRY_429_WAIT_S = 10


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill 5m bars from Alpaca into candles_5m")
    p.add_argument(
        "--symbols",
        help="Comma-separated symbols to backfill (default: all active in instruments table)",
    )
    p.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"Number of calendar days to look back (default: {DEFAULT_DAYS})",
    )
    p.add_argument(
        "--feed",
        default=DEFAULT_FEED,
        choices=["iex", "sip"],
        help=f"Alpaca data feed (default: {DEFAULT_FEED})",
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
        log.error("PGPASSWORD not set in .ENV — add your postgres password there")
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


def fetch_bars_page(
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    feed: str,
    api_key: str,
    api_secret: str,
    page_token: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Fetch one page of bars from Alpaca. Returns (bars_list, next_page_token)."""
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }
    params: Dict[str, Any] = {
        "symbols": symbol,
        "timeframe": timeframe,
        "start": start,
        "end": end,
        "limit": BARS_PER_PAGE,
        "feed": feed,
        "adjustment": "all",
        "sort": "asc",
    }
    if page_token:
        params["page_token"] = page_token

    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.get(ALPACA_DATA_URL, headers=headers, params=params, timeout=30)

        if resp.status_code == 429:
            wait = RETRY_429_WAIT_S * attempt
            log.warning("Rate limited (429). Waiting %ds (attempt %d/%d)", wait, attempt, MAX_RETRIES)
            time.sleep(wait)
            continue

        resp.raise_for_status()
        data = resp.json()
        bars = data.get("bars", {}).get(symbol, [])
        npt = data.get("next_page_token")
        return bars, npt

    log.error("Max retries hit for %s", symbol)
    return [], None


INSERT_SQL = """
    INSERT INTO public.candles_5m (symbol, ts, feed, open, high, low, close, volume, vwap, trade_count)
    VALUES %s
    ON CONFLICT (symbol, ts, feed) DO NOTHING
"""

COUNT_SQL = "SELECT count(*) FROM public.candles_5m WHERE symbol = %s AND feed = %s"


def insert_bars_batch(
    conn: psycopg2.extensions.connection,
    symbol: str,
    feed: str,
    bars: List[Dict[str, Any]],
) -> int:
    """Batch insert bars into candles_5m. Returns number of rows actually inserted."""
    if not bars:
        return 0

    rows = []
    for b in bars:
        rows.append((
            symbol,
            b["t"],
            feed,
            b["o"],
            b["h"],
            b["l"],
            b["c"],
            b["v"],
            b.get("vw"),
            b.get("n"),
        ))

    with conn.cursor() as cur:
        cur.execute(COUNT_SQL, (symbol, feed))
        before = cur.fetchone()[0]
        psycopg2.extras.execute_values(cur, INSERT_SQL, rows, page_size=2000)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(COUNT_SQL, (symbol, feed))
        after = cur.fetchone()[0]

    return after - before


def create_ingestion_run(
    conn: psycopg2.extensions.connection,
    symbol: str,
    meta: Dict[str, Any],
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.ingestion_runs (job_type, symbol, status, meta)
            VALUES ('bars_backfill', %s, 'running', %s)
            RETURNING id
            """,
            (symbol, json.dumps(meta)),
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
    symbol: str,
    error_type: str,
    error_message: str,
    raw_payload: Optional[Dict] = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.ingestion_errors (run_id, symbol, error_type, error_message, raw_payload)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (run_id, symbol, error_type, error_message, json.dumps(raw_payload) if raw_payload else None),
        )
    conn.commit()


def backfill_symbol(
    conn: psycopg2.extensions.connection,
    symbol: str,
    start_dt: datetime,
    end_dt: datetime,
    feed: str,
    api_key: str,
    api_secret: str,
    dry_run: bool,
) -> Tuple[int, int]:
    """Backfill all pages for one symbol. Returns (total_inserted, total_skipped)."""
    start_str = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    total_fetched = 0
    total_inserted = 0
    page = 0
    page_token: Optional[str] = None

    while True:
        page += 1
        bars, next_token = fetch_bars_page(
            symbol, "5Min", start_str, end_str, feed, api_key, api_secret, page_token
        )

        if not bars:
            break

        total_fetched += len(bars)

        if not dry_run:
            inserted = insert_bars_batch(conn, symbol, feed, bars)
            total_inserted += inserted
            skipped = len(bars) - inserted
        else:
            inserted = 0
            skipped = 0

        log.info(
            "  %s page %d: fetched=%d inserted=%d (total fetched=%d)",
            symbol, page, len(bars), inserted, total_fetched,
        )

        if not next_token:
            break

        page_token = next_token
        time.sleep(REQUEST_DELAY_S)

    total_skipped = total_fetched - total_inserted
    return total_inserted, total_skipped


def main() -> None:
    args = parse_args()
    cfg = load_config()

    end_dt = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=args.days)

    log.info("Backfill range: %s to %s (%d days)", start_dt.date(), end_dt.date(), args.days)
    log.info("Feed: %s | Dry run: %s", args.feed, args.dry_run)

    conn = get_db_conn(cfg)

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    else:
        symbols = get_active_symbols(conn)

    if not symbols:
        log.error("No symbols to backfill. Check instruments table or --symbols flag.")
        sys.exit(1)

    log.info("Symbols: %s", ", ".join(symbols))

    grand_inserted = 0
    grand_skipped = 0

    for sym in symbols:
        log.info("--- Starting %s ---", sym)
        run_meta = {
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "feed": args.feed,
            "days": args.days,
            "dry_run": args.dry_run,
        }

        run_id = None
        if not args.dry_run:
            run_id = create_ingestion_run(conn, sym, run_meta)

        try:
            inserted, skipped = backfill_symbol(
                conn, sym, start_dt, end_dt, args.feed,
                cfg["alpaca_key"], cfg["alpaca_secret"], args.dry_run,
            )
            grand_inserted += inserted
            grand_skipped += skipped

            if run_id is not None:
                finish_ingestion_run(conn, run_id, "success", inserted, skipped)

            log.info("  %s done: inserted=%d skipped=%d", sym, inserted, skipped)

        except Exception as exc:
            log.error("  %s FAILED: %s", sym, exc)
            if run_id is not None:
                log_ingestion_error(conn, run_id, sym, "api_error", str(exc))
                finish_ingestion_run(conn, run_id, "failed", 0, 0, str(exc))

    conn.close()

    log.info("=== COMPLETE === inserted=%d skipped=%d symbols=%d", grand_inserted, grand_skipped, len(symbols))


if __name__ == "__main__":
    main()
