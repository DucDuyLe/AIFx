#!/usr/bin/env python3
"""
Binance klines downloader for 1m/5m intervals (no API key required).

Examples (PowerShell):
  python scripts/binance_downloader.py --symbol BTCUSDT --interval 1m --days 365
  python scripts/binance_downloader.py --symbol ETHUSDT --interval 5m --start 2024-01-01 --end 2025-01-01

Output: CSV file with OHLCV and metadata in the specified or auto-generated path.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

import requests


BINANCE_SPOT_BASE_URL = "https://api.binance.com"
KLINES_ENDPOINT = "/api/v3/klines"

INTERVAL_TO_MS: Dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download Binance klines for 1m/5m intervals")
    parser.add_argument("--symbol", required=True, help="Trading pair symbol, e.g., BTCUSDT")
    parser.add_argument(
        "--interval",
        required=True,
        choices=sorted(INTERVAL_TO_MS.keys()),
        help="Kline interval",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, help="Number of days back from --end (default: 365)")
    group.add_argument("--start", help="Start date (UTC) in YYYY-MM-DD or ISO 8601")
    parser.add_argument("--end", help="End date (UTC) in YYYY-MM-DD or ISO 8601 (default: now)")
    parser.add_argument(
        "--out",
        help="Output CSV path. If omitted, a file is created in ./data/",
    )
    parser.add_argument(
        "--request-delay-ms",
        type=int,
        default=200,
        help="Sleep between API requests in milliseconds (default: 200)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Max klines per request (Binance max 1000)",
    )
    return parser.parse_args()


def ensure_output_path(path: Optional[str], symbol: str, interval: str, start_dt: datetime, end_dt: datetime) -> str:
    if path:
        out_dir = os.path.dirname(path) or "."
        os.makedirs(out_dir, exist_ok=True)
        return path
    out_dir = os.path.join("data")
    os.makedirs(out_dir, exist_ok=True)
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str = end_dt.strftime("%Y-%m-%d")
    filename = f"{symbol.upper()}_{interval}_{start_str}_{end_str}.csv"
    return os.path.join(out_dir, filename)


def parse_datetime_utc(dt_str: str) -> datetime:
    # Accept YYYY-MM-DD or ISO 8601; assume naive timestamps are UTC
    try:
        if len(dt_str) == 10 and dt_str[4] == "-" and dt_str[7] == "-":
            return datetime.strptime(dt_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        # Fallback to fromisoformat for simple ISO strings
        parsed = datetime.fromisoformat(dt_str)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception as exc:
        raise ValueError(f"Could not parse datetime '{dt_str}': {exc}") from exc


def to_millis(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def from_millis(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def fetch_klines(
    session: requests.Session,
    symbol: str,
    interval: str,
    start_time_ms: int,
    end_time_ms: int,
    limit: int,
) -> List[List[str]]:
    params = {
        "symbol": symbol.upper(),
        "interval": interval,
        "startTime": start_time_ms,
        "endTime": end_time_ms,
        "limit": min(limit, 1000),
    }
    url = BINANCE_SPOT_BASE_URL + KLINES_ENDPOINT

    # Retry with simple exponential backoff
    delay = 0.5
    for attempt in range(6):
        try:
            resp = session.get(url, params=params, timeout=30)
            if resp.status_code == 429 or resp.status_code == 418:
                # Rate limited or banned; respect Retry-After if present
                retry_after = float(resp.headers.get("Retry-After", "1"))
                time.sleep(max(retry_after, delay))
                delay = min(delay * 2, 8.0)
                continue
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise RuntimeError(f"Unexpected response: {data}")
            return data  # List of klines
        except (requests.RequestException, ValueError) as exc:
            if attempt == 5:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 8.0)
    return []


def write_csv_header(writer: csv.writer) -> None:
    writer.writerow(
        [
            "symbol",
            "interval",
            "open_time_ms",
            "open_time_iso",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time_ms",
            "close_time_iso",
            "quote_asset_volume",
            "number_of_trades",
            "taker_buy_base_volume",
            "taker_buy_quote_volume",
        ]
    )


def kline_rows(symbol: str, interval: str, klines: Iterable[List]) -> Iterable[List[str]]:
    for k in klines:
        open_time_ms = int(k[0])
        close_time_ms = int(k[6])
        yield [
            symbol.upper(),
            interval,
            str(open_time_ms),
            from_millis(open_time_ms).isoformat(),
            k[1],  # open
            k[2],  # high
            k[3],  # low
            k[4],  # close
            k[5],  # volume
            str(close_time_ms),
            from_millis(close_time_ms).isoformat(),
            k[7],  # quote asset volume
            str(k[8]),  # number of trades
            k[9],  # taker buy base volume
            k[10],  # taker buy quote volume
        ]


def download_klines(
    symbol: str,
    interval: str,
    start_dt: datetime,
    end_dt: datetime,
    out_path: str,
    request_delay_ms: int,
    limit: int,
) -> None:
    if interval not in INTERVAL_TO_MS:
        raise ValueError(f"Unsupported interval: {interval}. Supported: {sorted(INTERVAL_TO_MS.keys())}")
    if start_dt >= end_dt:
        raise ValueError("start must be earlier than end")

    interval_ms = INTERVAL_TO_MS[interval]
    start_ms = to_millis(start_dt)
    end_ms = to_millis(end_dt)

    # Prepare CSV
    is_new_file = not os.path.exists(out_path)
    out_dir = os.path.dirname(out_path) or "."
    os.makedirs(out_dir, exist_ok=True)

    with requests.Session() as session, open(out_path, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new_file:
            write_csv_header(writer)

        next_start = start_ms
        total_rows = 0
        while next_start < end_ms:
            klines = fetch_klines(
                session=session,
                symbol=symbol,
                interval=interval,
                start_time_ms=next_start,
                end_time_ms=end_ms,
                limit=limit,
            )
            if not klines:
                break

            # Write rows
            for row in kline_rows(symbol, interval, klines):
                writer.writerow(row)
                total_rows += 1

            last_open_time_ms = int(klines[-1][0])
            next_start = last_open_time_ms + interval_ms

            # Throttle slightly to be polite
            if request_delay_ms > 0:
                time.sleep(request_delay_ms / 1000.0)

        print(f"Done. Wrote {total_rows} rows to {out_path}")


def main() -> None:
    args = parse_args()

    now_utc = datetime.now(tz=timezone.utc)
    end_dt = parse_datetime_utc(args.end) if args.end else now_utc
    if args.start:
        start_dt = parse_datetime_utc(args.start)
    else:
        days = args.days if args.days is not None else 365
        start_dt = end_dt - timedelta(days=days)

    out_path = ensure_output_path(args.out, args.symbol, args.interval, start_dt, end_dt)

    print(
        f"Downloading {args.symbol.upper()} {args.interval} klines from {start_dt.isoformat()} to {end_dt.isoformat()}\n"
        f"Output: {out_path}"
    )

    try:
        download_klines(
            symbol=args.symbol,
            interval=args.interval,
            start_dt=start_dt,
            end_dt=end_dt,
            out_path=out_path,
            request_delay_ms=args.request_delay_ms,
            limit=args.limit,
        )
    except KeyboardInterrupt:
        print("Interrupted by user.")
        sys.exit(130)
    except Exception as exc:
        print(f"Error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()


