#!/usr/bin/env python3
"""
Ingest CSV from binance_downloader into Postgres (e.g., Supabase).

Usage (PowerShell):
  $env:DATABASE_URL="postgresql://user:pass@host:5432/db?sslmode=require"
  python scripts\ingest_csv_to_postgres.py --csv data\AXSUSDT_1m_2024-10-16_2025-10-16.csv

Notes:
- Expects CSV header from binance_downloader.py
- Upserts into public.candles using (symbol, interval, open_time) as key
"""

from __future__ import annotations

import argparse
import csv
import os
from datetime import datetime, timezone
from typing import List, Tuple

import psycopg2
from psycopg2.extras import execute_values


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest Binance CSV into Postgres")
    parser.add_argument("--csv", required=True, help="Path to CSV produced by binance_downloader.py")
    parser.add_argument(
        "--table",
        default="public.candles",
        help="Target table (default: public.candles)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5000,
        help="Rows per upsert batch (default: 5000)",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Override source column value (e.g., polygon, binance). If omitted, uses CSV/default",
    )
    return parser.parse_args()


def get_db_url() -> str:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL env var is required")
    return db_url


def parse_iso_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def read_rows(csv_path: str, source_override: str | None) -> List[Tuple]:
    rows: List[Tuple] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {
            "symbol",
            "interval",
            "open_time_iso",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time_iso",
            "quote_asset_volume",
            "number_of_trades",
            "taker_buy_base_volume",
            "taker_buy_quote_volume",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(f"CSV is missing columns: {sorted(missing)}")

        for r in reader:
            rows.append(
                (
                    r["symbol"],
                    r["interval"],
                    parse_iso_utc(r["open_time_iso"]),
                    r["open"],
                    r["high"],
                    r["low"],
                    r["close"],
                    r["volume"],
                    parse_iso_utc(r["close_time_iso"]),
                    r["quote_asset_volume"],
                    int(r["number_of_trades"]),
                    r["taker_buy_base_volume"],
                    r["taker_buy_quote_volume"],
                    source_override or "binance",
                )
            )
    return rows


def upsert_rows(conn, table: str, rows: List[Tuple], batch_size: int) -> int:
    if not rows:
        return 0
    cols = (
        "symbol",
        "interval",
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_asset_volume",
        "number_of_trades",
        "taker_buy_base_volume",
        "taker_buy_quote_volume",
        "source",
    )
    insert_sql = f"""
        insert into {table} ({','.join(cols)})
        values %s
        on conflict (symbol, interval, open_time) do update set
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            volume = excluded.volume,
            close_time = excluded.close_time,
            quote_asset_volume = excluded.quote_asset_volume,
            number_of_trades = excluded.number_of_trades,
            taker_buy_base_volume = excluded.taker_buy_base_volume,
            taker_buy_quote_volume = excluded.taker_buy_quote_volume,
            source = excluded.source
    """

    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), batch_size):
            chunk = rows[i : i + batch_size]
            execute_values(cur, insert_sql, chunk)
            total += len(chunk)
        conn.commit()
    return total


def main() -> None:
    args = parse_args()
    db_url = get_db_url()
    rows = read_rows(args.csv, args.source)
    with psycopg2.connect(db_url) as conn:
        inserted = upsert_rows(conn, args.table, rows, args.batch_size)
    print(f"Upserted {inserted} rows into {args.table}")


if __name__ == "__main__":
    main()


