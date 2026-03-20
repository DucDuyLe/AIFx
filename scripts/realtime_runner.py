#!/usr/bin/env python3
"""
Realtime runner supporting delayed or live modes with Polygon or Alpaca data.

- Data sources:
  - polygon: uses REST aggs range endpoint per symbol (requires POLYGON_API_KEY)
  - alpaca: uses Market Data v2 bars multi-symbol endpoint (requires APCA keys for some feeds)

- Execution (optional):
  - Alpaca paper REST (requires APCA_API_KEY_ID, APCA_API_SECRET_KEY, APCA_API_BASE_URL)
  - IBKR paper is the default target path for this project; if selected here, orders are logged
    as "approved-but-not-sent" placeholders until a dedicated IBKR connector is added.

Typical usage (PowerShell):
  # Delayed data (free tiers), no orders, check last closed 5m bar
  python scripts\realtime_runner.py --data-source alpaca --interval 5m --tickers AAPL,MSFT --once

  # Loop on 5m bar-close, using Polygon data, and paper place demo orders via Alpaca
  $env:POLYGON_API_KEY="your_key"
  $env:APCA_API_KEY_ID="paper-key"; $env:APCA_API_SECRET_KEY="paper-secret"; $env:APCA_API_BASE_URL="https://paper-api.alpaca.markets"
  python scripts\realtime_runner.py --data-source polygon --interval 5m --tickers-file data\tickers_top200.txt --max-tickers 50 --paper --demo-strategy
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests


# -----------------------------
# Utilities
# -----------------------------

ISO_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def floor_time(dt: datetime, seconds: int) -> datetime:
    epoch = int(dt.timestamp())
    floored = epoch - (epoch % seconds)
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def next_boundary(dt: datetime, seconds: int) -> datetime:
    floored = floor_time(dt, seconds)
    if floored == dt:
        return dt
    return datetime.fromtimestamp(floored.timestamp() + seconds, tz=timezone.utc)


# -----------------------------
# Args
# -----------------------------


INTERVAL_TO_SECONDS: Dict[str, int] = {
    "1m": 60,
    "5m": 300,
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Realtime 1m/5m runner with Polygon/Alpaca data and optional Alpaca paper orders")
    g_t = p.add_mutually_exclusive_group(required=True)
    g_t.add_argument("--tickers", help="Comma-separated tickers, e.g., AAPL,MSFT")
    g_t.add_argument("--tickers-file", help="Path to file with one ticker per line")
    p.add_argument("--max-tickers", type=int, default=50, help="Max symbols to use from the list (default: 50)")
    p.add_argument("--interval", required=True, choices=sorted(INTERVAL_TO_SECONDS.keys()))
    p.add_argument("--data-source", choices=["polygon", "alpaca"], default="alpaca")
    p.add_argument("--mode", choices=["delayed", "live"], default="delayed", help="Hints data feed selection (alpaca feed)")
    p.add_argument("--once", action="store_true", help="Run once for the latest closed bar and exit")
    p.add_argument("--paper", action="store_true", help="Enable Alpaca paper order placement")
    p.add_argument("--demo-strategy", action="store_true", help="Place sample orders: buy if close>open, sell if close<open")
    p.add_argument("--request-delay-ms", type=int, default=200, help="Delay between HTTP calls")
    p.add_argument("--broker-target", choices=["ibkr", "alpaca"], default="ibkr", help="Execution target path (default: ibkr)")
    p.add_argument("--enforce-session", action="store_true", help="Block orders outside configured market session")
    p.add_argument("--session-tz", default="America/New_York", help="Session timezone name")
    p.add_argument("--session-start", default="09:30", help="Session start HH:MM")
    p.add_argument("--session-end", default="16:00", help="Session end HH:MM")
    p.add_argument("--skip-open-minutes", type=int, default=5, help="Skip first N minutes after session open")
    p.add_argument("--skip-close-minutes", type=int, default=10, help="Skip last N minutes before session close")
    p.add_argument("--max-orders-per-cycle", type=int, default=4, help="Simple exposure cap per cycle")
    p.add_argument("--max-spread-bps", type=float, default=8.0, help="Reject if spread proxy exceeds this threshold")
    p.add_argument("--max-slippage-bps", type=float, default=12.0, help="Reject if estimated slippage exceeds this threshold")
    p.add_argument("--metrics-csv", default="data/execution_metrics.csv", help="Path to append execution metrics rows")
    return p.parse_args()


def load_tickers(args: argparse.Namespace) -> List[str]:
    if args.tickers:
        syms = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        syms: List[str] = []
        with open(args.tickers_file, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                syms.append(s.upper())
    if args.max_tickers and len(syms) > args.max_tickers:
        return syms[: args.max_tickers]
    return syms


# -----------------------------
# Data sources
# -----------------------------


def fetch_latest_bars_polygon(symbols: List[str], interval: str, request_delay_ms: int, api_key: str) -> Dict[str, Dict]:
    """
    Fetch the latest closed bar per symbol using Polygon range endpoint (1 request per symbol).
    Returns a dict: symbol -> { open, high, low, close, volume, t(open_ms) }
    """
    multiplier = 1 if interval == "1m" else 5
    timespan = "minute"
    results: Dict[str, Dict] = {}
    end = floor_time(now_utc(), INTERVAL_TO_SECONDS[interval])
    start = end
    # Query a small lookback window to ensure we catch the latest bar
    start = datetime.fromtimestamp(end.timestamp() - 8 * INTERVAL_TO_SECONDS[interval], tz=timezone.utc)

    base = "https://api.polygon.io/v2/aggs/ticker/{sym}/range/{mult}/{span}/{start}/{end}"
    for i, sym in enumerate(symbols):
        url = base.format(sym=sym, mult=multiplier, span=timespan, start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
        params = {"adjusted": "true", "sort": "desc", "limit": 1, "apiKey": api_key}
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(1.0)
                r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            items = data.get("results") or []
            if items:
                results[sym] = items[0]
        except Exception:
            continue
        if request_delay_ms > 0 and i + 1 < len(symbols):
            time.sleep(request_delay_ms / 1000.0)
    return results


def fetch_latest_bars_alpaca(symbols: List[str], interval: str, mode: str, request_delay_ms: int) -> Dict[str, Dict]:
    """
    Fetch latest closed bar for many symbols in one shot via Alpaca Market Data v2.
    Returns dict: symbol -> { t, o, h, l, c, v } (keys mirror Polygon for simplicity)
    """
    tf = "1Min" if interval == "1m" else "5Min"
    # For delayed mode, feed=iex is typically available; for live, feed=sip may require paid
    feed = "iex" if mode == "delayed" else os.environ.get("ALPACA_FEED", "sip")
    url = "https://data.alpaca.markets/v2/stocks/bars"

    headers = {
        "Apca-Api-Key-Id": os.environ.get("APCA_API_KEY_ID", ""),
        "Apca-Api-Secret-Key": os.environ.get("APCA_API_SECRET_KEY", ""),
    }
    # batch symbols to respect URL length limits
    results: Dict[str, Dict] = {}
    batch_size = 200
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i : i + batch_size]
        params = {
            "symbols": ",".join(batch),
            "timeframe": tf,
            "limit": 1,
            "feed": feed,
            "adjustment": "all",
        }
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json() or {}
            bars = data.get("bars") or {}
            for sym, lst in bars.items():
                if not lst:
                    continue
                b = lst[0]
                # Normalize to Polygon-like keys
                results[sym.upper()] = {
                    "t": int(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp() * 1000),
                    "o": b.get("o"),
                    "h": b.get("h"),
                    "l": b.get("l"),
                    "c": b.get("c"),
                    "v": b.get("v"),
                    "n": b.get("n", 0),
                }
        except Exception:
            continue
        if request_delay_ms > 0 and i + batch_size < len(symbols):
            time.sleep(request_delay_ms / 1000.0)
    return results


# -----------------------------
# Orders (Alpaca paper)
# -----------------------------


def alpaca_paper_order(symbol: str, side: str, qty: int) -> Optional[str]:
    base = os.environ.get("APCA_API_BASE_URL", "").rstrip("/")
    key = os.environ.get("APCA_API_KEY_ID", "")
    secret = os.environ.get("APCA_API_SECRET_KEY", "")
    if not base or not key or not secret:
        return None
    url = f"{base}/v2/orders"
    headers = {"Apca-Api-Key-Id": key, "Apca-Api-Secret-Key": secret, "Content-Type": "application/json"}
    payload = {
        "symbol": symbol,
        "qty": qty,
        "side": side,
        "type": "market",
        "time_in_force": "day",
    }
    r = requests.post(url, json=payload, headers=headers, timeout=30)
    if r.status_code >= 400:
        return None
    return r.json().get("id")


# -----------------------------
# Runner
# -----------------------------


@dataclass
class Bar:
    symbol: str
    open_ms: int
    open_: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class SendGateResult:
    ok: bool
    reason: str
    spread_bps: float
    est_slippage_bps: float


def normalize_bars(raw: Dict[str, Dict]) -> List[Bar]:
    bars: List[Bar] = []
    for sym, d in raw.items():
        try:
            bars.append(
                Bar(
                    symbol=sym,
                    open_ms=int(d.get("t")),
                    open_=float(d.get("o")),
                    high=float(d.get("h")),
                    low=float(d.get("l")),
                    close=float(d.get("c")),
                    volume=float(d.get("v")),
                )
            )
        except Exception:
            continue
    return bars


def demo_signal(bar: Bar) -> Optional[Tuple[str, int]]:
    # Simple placeholder: buy if close > open; sell if close < open
    if bar.close > bar.open_:
        return ("buy", 1)
    if bar.close < bar.open_:
        return ("sell", 1)
    return None


def _parse_hhmm(value: str) -> Tuple[int, int]:
    hh, mm = value.split(":")
    return int(hh), int(mm)


def in_allowed_session(
    now_dt: datetime,
    session_tz: str,
    session_start: str,
    session_end: str,
    skip_open_minutes: int,
    skip_close_minutes: int,
) -> Tuple[bool, str]:
    local = now_dt.astimezone(ZoneInfo(session_tz))
    if local.weekday() >= 5:
        return (False, "weekend")

    start_h, start_m = _parse_hhmm(session_start)
    end_h, end_m = _parse_hhmm(session_end)
    start_dt = local.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end_dt = local.replace(hour=end_h, minute=end_m, second=0, microsecond=0)

    if local < start_dt or local >= end_dt:
        return (False, "outside_session")
    if local < start_dt + timedelta(minutes=skip_open_minutes):
        return (False, "open_buffer")
    if local >= end_dt - timedelta(minutes=skip_close_minutes):
        return (False, "close_buffer")
    return (True, "ok")


def spread_proxy_bps(bar: Bar) -> float:
    if bar.close <= 0:
        return float("inf")
    return ((bar.high - bar.low) / bar.close) * 10000.0


def estimate_slippage_bps(bar: Bar) -> float:
    if bar.close <= 0:
        return float("inf")
    # Proxy estimate for demo runner: small fraction of bar body with a floor.
    body_bps = abs(bar.close - bar.open_) / bar.close * 10000.0
    return max(0.5, body_bps * 0.10)


def evaluate_send_gate(bar: Bar, args: argparse.Namespace) -> SendGateResult:
    spread_bps = spread_proxy_bps(bar)
    est_slippage_bps = estimate_slippage_bps(bar)
    if spread_bps > args.max_spread_bps:
        return SendGateResult(False, "spread_block", spread_bps, est_slippage_bps)
    if est_slippage_bps > args.max_slippage_bps:
        return SendGateResult(False, "slippage_block", spread_bps, est_slippage_bps)
    return SendGateResult(True, "ok", spread_bps, est_slippage_bps)


def append_metric_row(path: str, row: Dict[str, object]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    exists = os.path.exists(path)
    fieldnames = [
        "ts_utc",
        "symbol",
        "event_type",
        "reason",
        "spread_bps",
        "est_slippage_bps",
        "broker_target",
        "order_id",
    ]
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def run_once(
    symbols: List[str],
    interval: str,
    data_source: str,
    mode: str,
    request_delay_ms: int,
    polygon_key: Optional[str],
    paper: bool,
    demo_strategy: bool,
    args: argparse.Namespace,
) -> None:
    if data_source == "polygon":
        if not polygon_key:
            print("POLYGON_API_KEY required for polygon data")
            return
        raw = fetch_latest_bars_polygon(symbols, interval, request_delay_ms, polygon_key)
    else:
        raw = fetch_latest_bars_alpaca(symbols, interval, mode, request_delay_ms)
    bars = normalize_bars(raw)
    bars.sort(key=lambda b: b.symbol)
    ts = bars[0].open_ms if bars else int(now_utc().timestamp() * 1000)
    print(f"Fetched {len(bars)} bars @ {datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime(ISO_FORMAT)}")
    if not demo_strategy:
        return
    allowed = True
    session_reason = "ok"
    if args.enforce_session:
        allowed, session_reason = in_allowed_session(
            now_dt=now_utc(),
            session_tz=args.session_tz,
            session_start=args.session_start,
            session_end=args.session_end,
            skip_open_minutes=args.skip_open_minutes,
            skip_close_minutes=args.skip_close_minutes,
        )

    sent_count = 0
    for b in bars:
        sig = demo_signal(b)
        if not sig:
            continue
        side, qty = sig
        if sent_count >= args.max_orders_per_cycle:
            print(f"{b.symbol}: {side} x{qty} -> blocked(max_orders_per_cycle)")
            append_metric_row(
                args.metrics_csv,
                {
                    "ts_utc": now_utc().isoformat(),
                    "symbol": b.symbol,
                    "event_type": "risk_blocked",
                    "reason": "max_orders_per_cycle",
                    "spread_bps": "",
                    "est_slippage_bps": "",
                    "broker_target": args.broker_target,
                    "order_id": "",
                },
            )
            continue
        if not allowed:
            print(f"{b.symbol}: {side} x{qty} -> blocked({session_reason})")
            append_metric_row(
                args.metrics_csv,
                {
                    "ts_utc": now_utc().isoformat(),
                    "symbol": b.symbol,
                    "event_type": "session_blocked",
                    "reason": session_reason,
                    "spread_bps": "",
                    "est_slippage_bps": "",
                    "broker_target": args.broker_target,
                    "order_id": "",
                },
            )
            continue

        gate = evaluate_send_gate(b, args)
        if not gate.ok:
            print(f"{b.symbol}: {side} x{qty} -> blocked({gate.reason}) spread={gate.spread_bps:.2f}bps est_slip={gate.est_slippage_bps:.2f}bps")
            append_metric_row(
                args.metrics_csv,
                {
                    "ts_utc": now_utc().isoformat(),
                    "symbol": b.symbol,
                    "event_type": "risk_blocked",
                    "reason": gate.reason,
                    "spread_bps": f"{gate.spread_bps:.4f}",
                    "est_slippage_bps": f"{gate.est_slippage_bps:.4f}",
                    "broker_target": args.broker_target,
                    "order_id": "",
                },
            )
            continue

        order_id: Optional[str] = None
        if args.broker_target == "alpaca" and paper:
            order_id = alpaca_paper_order(b.symbol, side, qty)
        elif args.broker_target == "ibkr":
            # Placeholder for IBKR paper connector integration.
            order_id = "ibkr_paper_placeholder"

        event_type = "sent" if order_id else "rejected"
        status = order_id or "rejected"
        sent_count += 1 if order_id else 0
        print(f"{b.symbol}: {side} x{qty} -> {status} spread={gate.spread_bps:.2f}bps est_slip={gate.est_slippage_bps:.2f}bps")
        append_metric_row(
            args.metrics_csv,
            {
                "ts_utc": now_utc().isoformat(),
                "symbol": b.symbol,
                "event_type": event_type,
                "reason": "ok" if order_id else "broker_reject_or_disabled",
                "spread_bps": f"{gate.spread_bps:.4f}",
                "est_slippage_bps": f"{gate.est_slippage_bps:.4f}",
                "broker_target": args.broker_target,
                "order_id": order_id or "",
            },
        )


def main() -> None:
    args = parse_args()
    symbols = load_tickers(args)
    polygon_key = os.environ.get("POLYGON_API_KEY")
    step_seconds = INTERVAL_TO_SECONDS[args.interval]

    if args.once:
        run_once(symbols, args.interval, args.data_source, args.mode, args.request_delay_ms, polygon_key, args.paper, args.demo_strategy, args)
        return

    # Align to bar-close and loop
    while True:
        t0 = now_utc()
        close_ts = floor_time(t0, step_seconds)
        # Sleep until just after the next bar closes
        next_ts = datetime.fromtimestamp(close_ts.timestamp() + step_seconds + 1, tz=timezone.utc)
        sleep_s = max(0.0, (next_ts - now_utc()).total_seconds())
        if sleep_s > 0:
            time.sleep(sleep_s)
        try:
            run_once(symbols, args.interval, args.data_source, args.mode, args.request_delay_ms, polygon_key, args.paper, args.demo_strategy, args)
        except KeyboardInterrupt:
            print("Interrupted.")
            sys.exit(130)
        except Exception as exc:
            print(f"Error: {exc}")
            time.sleep(2.0)


if __name__ == "__main__":
    main()



