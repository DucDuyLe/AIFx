#!/usr/bin/env python3
"""
Download equities 5-minute (or 1-minute) bars from Polygon.io aggregates API.

Requirements:
  - Set POLYGON_API_KEY env var or pass --api-key

Examples (PowerShell):
  $env:POLYGON_API_KEY="your_key"
  python scripts/polygon_downloader.py --tickers AAPL,MSFT --interval 5m --days 365

  python scripts/polygon_downloader.py --tickers-file data/tickers_top200.txt --interval 5m --start 2024-01-01 --end 2025-01-01

Outputs CSV per ticker in data/ by default, with the same columns as the Binance CSV so it can be ingested by the existing ingestor script.
"""

from __future__ import annotations

import argparse
import csv
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

import requests


POLYGON_BASE_URL = "https://api.polygon.io"

INTERVAL_TO_MULTIPLIER_TIMESPAN: Dict[str, Tuple[int, str]] = {
    "5m": (5, "minute"),
    "1m": (1, "minute"),
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Polygon equities intraday downloader")
    g_tickers = p.add_mutually_exclusive_group(required=True)
    g_tickers.add_argument("--tickers", help="Comma-separated tickers, e.g., AAPL,MSFT,GOOGL")
    g_tickers.add_argument("--tickers-file", help="Path to a text file with one ticker per line")
    p.add_argument("--interval", required=True, choices=sorted(INTERVAL_TO_MULTIPLIER_TIMESPAN.keys()))
    g_time = p.add_mutually_exclusive_group()
    g_time.add_argument("--days", type=int, help="Days back from --end (default 365)")
    g_time.add_argument("--start", help="Start date (UTC) YYYY-MM-DD or ISO 8601")
    p.add_argument("--end", help="End date (UTC) YYYY-MM-DD or ISO 8601 (default now)")
    p.add_argument("--out-dir", default="data", help="Output directory (default: data)")
    p.add_argument("--api-key", help="Polygon API key (or set POLYGON_API_KEY)")
    p.add_argument("--adjusted", action="store_true", help="Request adjusted aggregates")
    p.add_argument("--request-delay-ms", type=int, default=200, help="Delay between requests")
    p.add_argument("--limit", type=int, default=50000, help="Aggs per page (Polygon max 50000)")
    return p.parse_args()


def parse_datetime_utc(dt_str: str) -> datetime:
    if len(dt_str) == 10 and dt_str[4] == "-" and dt_str[7] == "-":
        return datetime.strptime(dt_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ensure_dir(d: str) -> None:
    os.makedirs(d, exist_ok=True)


def load_tickers(args: argparse.Namespace) -> List[str]:
    if args.tickers:
        return [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    tickers: List[str] = []
    with open(args.tickers_file, encoding="utf-8") as f:
        for line in f:
            t = line.strip()
            if not t or t.startswith("#"):
                continue
            tickers.append(t.upper())
    return tickers


def to_millis(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def from_millis(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def write_csv_header(writer: csv.writer) -> None:
    writer.writerow([
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
    ])


def polygon_aggs_url(ticker: str, multiplier: int, timespan: str, start_dt: datetime, end_dt: datetime) -> str:
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str = end_dt.strftime("%Y-%m-%d")
    return f"{POLYGON_BASE_URL}/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{start_str}/{end_str}"


def fetch_aggs(session: requests.Session, url: str, api_key: str, adjusted: bool, limit: int) -> Iterable[dict]:
    params = {
        "adjusted": "true" if adjusted else "false",
        "sort": "asc",
        "limit": min(limit, 50000),
        "apiKey": api_key,
    }
    next_url: Optional[str] = url
    delay = 0.5
    while next_url:
        try:
            resp = session.get(next_url, params=None if next_url != url else params, timeout=30)
            if resp.status_code == 429:
                time.sleep(delay)
                delay = min(delay * 2, 8.0)
                continue
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            for item in results:
                yield item
            # Polygon provides next_url for pagination when more data is available
            next_url = data.get("next_url")
            if next_url:
                # Ensure apiKey is present on next_url as well
                connector = "&" if "?" in next_url else "?"
                next_url = f"{next_url}{connector}apiKey={api_key}"
        except (requests.RequestException, ValueError):
            time.sleep(delay)
            delay = min(delay * 2, 8.0)
            continue


def aggs_to_rows(symbol: str, interval: str, multiplier: int, item: dict) -> List[str]:
    # Polygon fields: t (open ms), o,h,l,c, v (volume), n (#trades)
    open_ms = int(item.get("t"))
    o = item.get("o")
    h = item.get("h")
    l = item.get("l")
    c = item.get("c")
    v = item.get("v")
    n = item.get("n", 0)
    close_ms = open_ms + (multiplier * 60_000) - 1
    return [
        symbol,
        interval,
        str(open_ms),
        from_millis(open_ms).isoformat(),
        f"{o}",
        f"{h}",
        f"{l}",
        f"{c}",
        f"{v}",
        str(close_ms),
        from_millis(close_ms).isoformat(),
        "",              # quote_asset_volume (not applicable)
        str(n),           # number_of_trades
        "",              # taker_buy_base_volume (not provided)
        "",              # taker_buy_quote_volume (not provided)
    ]


def download_for_ticker(
    ticker: str,
    interval: str,
    start_dt: datetime,
    end_dt: datetime,
    out_dir: str,
    api_key: str,
    adjusted: bool,
    request_delay_ms: int,
    limit: int,
) -> str:
    multiplier, timespan = INTERVAL_TO_MULTIPLIER_TIMESPAN[interval]
    url = polygon_aggs_url(ticker, multiplier, timespan, start_dt, end_dt)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{ticker}_{interval}_{start_dt.strftime('%Y-%m-%d')}_{end_dt.strftime('%Y-%m-%d')}.csv")
    is_new = not os.path.exists(out_path)
    rows_written = 0
    with requests.Session() as session, open(out_path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            write_csv_header(w)
        for item in fetch_aggs(session, url, api_key, adjusted, limit):
            w.writerow(aggs_to_rows(ticker, interval, multiplier, item))
            rows_written += 1
        if request_delay_ms:
            time.sleep(request_delay_ms / 1000.0)
    print(f"{ticker}: wrote {rows_written} rows -> {out_path}")
    return out_path


def main() -> None:
    args = parse_args()
    api_key = args.api_key or os.environ.get("POLYGON_API_KEY")
    if not api_key:
        raise SystemExit("Polygon API key required. Set POLYGON_API_KEY or pass --api-key.")

    now_utc = datetime.now(tz=timezone.utc)
    end_dt = parse_datetime_utc(args.end) if args.end else now_utc
    if args.start:
        start_dt = parse_datetime_utc(args.start)
    else:
        days = args.days if args.days is not None else 365
        start_dt = end_dt - timedelta(days=days)

    ensure_dir(args.out_dir)
    tickers = load_tickers(args)
    print(f"Downloading {args.interval} bars for {len(tickers)} tickers from {start_dt.date()} to {end_dt.date()}")
    for t in tickers:
        try:
            download_for_ticker(
                ticker=t,
                interval=args.interval,
                start_dt=start_dt,
                end_dt=end_dt,
                out_dir=args.out_dir,
                api_key=api_key,
                adjusted=args.adjusted,
                request_delay_ms=args.request_delay_ms,
                limit=args.limit,
            )
        except Exception as exc:
            print(f"{t}: error {exc}")
            continue


if __name__ == "__main__":
    main()


