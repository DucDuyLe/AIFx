#!/usr/bin/env python3
"""
Quality checks on candles_5m data.

Checks:
  1. Row counts and date coverage per symbol
  2. Trading day gaps (missing days vs NYSE calendar)
  3. Bars-per-day distribution (detect short/long days)
  4. Bad OHLCV (high < low, negative volume, zero prices, OHLC out of range)
  5. Duplicate timestamps
  6. Extreme price moves (potential bad data)
  7. Null/missing vwap and trade_count

Usage:
    .venv\\Scripts\\python.exe scripts\\quality_check_bars.py
    .venv\\Scripts\\python.exe scripts\\quality_check_bars.py --symbol AAPL
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import psycopg2
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("quality_check")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Quality checks on candles_5m data")
    p.add_argument("--symbol", help="Check a single symbol (default: all active)")
    return p.parse_args()


def load_config() -> Dict[str, str]:
    env_path = Path(__file__).resolve().parent.parent / ".ENV"
    load_dotenv(env_path)
    return {
        "db_host": os.getenv("PGHOST", "localhost"),
        "db_port": os.getenv("PGPORT", "5434"),
        "db_name": os.getenv("PGDATABASE", "spai500"),
        "db_user": os.getenv("PGUSER", "postgres"),
        "db_password": os.environ.get("PGPASSWORD", ""),
    }


def get_db_conn(cfg: Dict[str, str]) -> psycopg2.extensions.connection:
    return psycopg2.connect(
        host=cfg["db_host"],
        port=cfg["db_port"],
        dbname=cfg["db_name"],
        user=cfg["db_user"],
        password=cfg["db_password"],
    )


def get_nyse_holidays(year_start: int, year_end: int) -> Set[date]:
    """Known NYSE holidays (approximate). Good enough for gap detection."""
    holidays: Set[date] = set()
    for y in range(year_start, year_end + 1):
        holidays.update([
            date(y, 1, 1),    # New Year's
            date(y, 1, 20) if y == 2025 else date(y, 1, 16) if y == 2024 else date(y, 1, 19),  # MLK
            date(y, 7, 4),    # Independence Day
            date(y, 12, 25),  # Christmas
        ])
    return holidays


def get_expected_trading_days(start: date, end: date) -> Set[date]:
    """Generate set of weekdays minus known holidays."""
    holidays = get_nyse_holidays(start.year, end.year)
    days: Set[date] = set()
    d = start
    while d <= end:
        if d.weekday() < 5 and d not in holidays:
            days.add(d)
        d += timedelta(days=1)
    return days


def check_overview(conn: psycopg2.extensions.connection, symbols: List[str]) -> None:
    """Check 1: Row counts and date coverage."""
    log.info("=" * 70)
    log.info("CHECK 1: Row counts and date coverage")
    log.info("=" * 70)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, count(*) as rows,
                   min(ts)::date as earliest, max(ts)::date as latest,
                   count(distinct ts::date) as trading_days
            FROM candles_5m
            WHERE symbol = ANY(%s)
            GROUP BY symbol ORDER BY symbol
        """, (symbols,))
        rows = cur.fetchall()

    for sym, cnt, earliest, latest, tdays in rows:
        cal_days = (latest - earliest).days + 1
        log.info(
            "  %-6s  rows=%6d  range=%s to %s  trading_days=%d  cal_days=%d",
            sym, cnt, earliest, latest, tdays, cal_days,
        )


def check_gaps(conn: psycopg2.extensions.connection, symbols: List[str]) -> None:
    """Check 2: Missing trading days."""
    log.info("=" * 70)
    log.info("CHECK 2: Missing trading days (gaps)")
    log.info("=" * 70)
    with conn.cursor() as cur:
        for sym in symbols:
            cur.execute("""
                SELECT min(ts)::date, max(ts)::date,
                       array_agg(distinct ts::date ORDER BY ts::date)
                FROM candles_5m WHERE symbol = %s
            """, (sym,))
            row = cur.fetchone()
            if not row or not row[0]:
                log.warning("  %-6s  NO DATA", sym)
                continue
            start, end, actual_dates = row
            actual_set = set(actual_dates)
            expected = get_expected_trading_days(start, end)
            missing = sorted(expected - actual_set)
            extra = sorted(actual_set - expected)

            if missing:
                if len(missing) <= 10:
                    log.warning("  %-6s  missing %d days: %s", sym, len(missing),
                                ", ".join(str(d) for d in missing))
                else:
                    log.warning("  %-6s  missing %d days (first 10): %s ...", sym, len(missing),
                                ", ".join(str(d) for d in missing[:10]))
            else:
                log.info("  %-6s  no missing trading days", sym)

            if extra and len(extra) <= 5:
                log.info("  %-6s  %d weekend/holiday dates with data (extended hours): %s",
                         sym, len(extra), ", ".join(str(d) for d in extra))


def check_bars_per_day(conn: psycopg2.extensions.connection, symbols: List[str]) -> None:
    """Check 3: Bars-per-day distribution."""
    log.info("=" * 70)
    log.info("CHECK 3: Bars-per-day distribution")
    log.info("=" * 70)
    with conn.cursor() as cur:
        for sym in symbols:
            cur.execute("""
                SELECT ts::date as day, count(*) as bars
                FROM candles_5m WHERE symbol = %s
                GROUP BY day ORDER BY day
            """, (sym,))
            rows = cur.fetchall()
            if not rows:
                continue
            counts = [r[1] for r in rows]
            avg_bars = sum(counts) / len(counts)
            min_bars = min(counts)
            max_bars = max(counts)
            min_day = [r[0] for r in rows if r[1] == min_bars][0]
            max_day = [r[0] for r in rows if r[1] == max_bars][0]

            short_days = [(r[0], r[1]) for r in rows if r[1] < 50]
            long_days = [(r[0], r[1]) for r in rows if r[1] > 100]

            log.info(
                "  %-6s  days=%d  avg=%.1f  min=%d (%s)  max=%d (%s)",
                sym, len(rows), avg_bars, min_bars, min_day, max_bars, max_day,
            )
            if short_days:
                shown = short_days[:5]
                log.warning(
                    "  %-6s  %d short days (<50 bars): %s%s", sym, len(short_days),
                    ", ".join(f"{d}({n})" for d, n in shown),
                    " ..." if len(short_days) > 5 else "",
                )
            if long_days:
                shown = long_days[:5]
                log.info(
                    "  %-6s  %d extended days (>100 bars): %s%s", sym, len(long_days),
                    ", ".join(f"{d}({n})" for d, n in shown),
                    " ..." if len(long_days) > 5 else "",
                )


def check_bad_ohlcv(conn: psycopg2.extensions.connection, symbols: List[str]) -> None:
    """Check 4: Bad OHLCV values."""
    log.info("=" * 70)
    log.info("CHECK 4: Bad OHLCV values")
    log.info("=" * 70)
    checks = [
        ("high < low", "high < low"),
        ("high < open or high < close", "high < open OR high < close"),
        ("low > open or low > close", "low > open OR low > close"),
        ("negative volume", "volume < 0"),
        ("zero/null price", "open <= 0 OR high <= 0 OR low <= 0 OR close <= 0"),
    ]
    with conn.cursor() as cur:
        for label, condition in checks:
            cur.execute(f"""
                SELECT symbol, count(*) FROM candles_5m
                WHERE symbol = ANY(%s) AND ({condition})
                GROUP BY symbol ORDER BY symbol
            """, (symbols,))
            rows = cur.fetchall()
            if rows:
                for sym, cnt in rows:
                    log.warning("  %-6s  %s: %d bars", sym, label, cnt)
            else:
                log.info("  PASS: %s — 0 violations", label)


def check_duplicates(conn: psycopg2.extensions.connection, symbols: List[str]) -> None:
    """Check 5: Duplicate timestamps (should be 0 given PK constraint)."""
    log.info("=" * 70)
    log.info("CHECK 5: Duplicate timestamps")
    log.info("=" * 70)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, ts, feed, count(*) FROM candles_5m
            WHERE symbol = ANY(%s)
            GROUP BY symbol, ts, feed
            HAVING count(*) > 1
            ORDER BY count(*) DESC
            LIMIT 10
        """, (symbols,))
        rows = cur.fetchall()
        if rows:
            for sym, ts, feed, cnt in rows:
                log.warning("  %-6s  %s feed=%s  count=%d", sym, ts, feed, cnt)
        else:
            log.info("  PASS: 0 duplicates (PK constraint enforced)")


def check_extreme_moves(conn: psycopg2.extensions.connection, symbols: List[str]) -> None:
    """Check 6: Extreme single-bar moves (>10% in 5m — likely bad data or halt)."""
    log.info("=" * 70)
    log.info("CHECK 6: Extreme price moves (>10%% in single 5m bar)")
    log.info("=" * 70)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, ts, open, high, low, close,
                   abs(close - open) / NULLIF(open, 0) * 100 as move_pct
            FROM candles_5m
            WHERE symbol = ANY(%s)
              AND abs(close - open) / NULLIF(open, 0) > 0.10
            ORDER BY abs(close - open) / NULLIF(open, 0) DESC
            LIMIT 20
        """, (symbols,))
        rows = cur.fetchall()
        if rows:
            log.warning("  Found %d bars with >10%% move:", len(rows))
            for sym, ts, o, h, l, c, pct in rows:
                log.warning("  %-6s  %s  O=%.2f H=%.2f L=%.2f C=%.2f  move=%.1f%%",
                            sym, ts, float(o), float(h), float(l), float(c), float(pct))
        else:
            log.info("  PASS: 0 bars with >10%% single-bar move")


def check_nulls(conn: psycopg2.extensions.connection, symbols: List[str]) -> None:
    """Check 7: Null vwap or trade_count."""
    log.info("=" * 70)
    log.info("CHECK 7: Null vwap / trade_count")
    log.info("=" * 70)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol,
                   count(*) FILTER (WHERE vwap IS NULL) as null_vwap,
                   count(*) FILTER (WHERE trade_count IS NULL) as null_tc,
                   count(*) as total
            FROM candles_5m
            WHERE symbol = ANY(%s)
            GROUP BY symbol ORDER BY symbol
        """, (symbols,))
        rows = cur.fetchall()
        for sym, nv, ntc, total in rows:
            if nv > 0 or ntc > 0:
                log.warning("  %-6s  null_vwap=%d (%.1f%%)  null_trade_count=%d (%.1f%%)  total=%d",
                            sym, nv, nv / total * 100, ntc, ntc / total * 100, total)
            else:
                log.info("  %-6s  PASS: no nulls in vwap or trade_count", sym)


def main() -> None:
    args = parse_args()
    cfg = load_config()
    conn = get_db_conn(cfg)

    if args.symbol:
        symbols = [args.symbol.upper()]
    else:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol FROM instruments WHERE is_active = true ORDER BY symbol")
            symbols = [r[0] for r in cur.fetchall()]

    log.info("Quality check for: %s", ", ".join(symbols))
    log.info("")

    check_overview(conn, symbols)
    log.info("")
    check_gaps(conn, symbols)
    log.info("")
    check_bars_per_day(conn, symbols)
    log.info("")
    check_bad_ohlcv(conn, symbols)
    log.info("")
    check_duplicates(conn, symbols)
    log.info("")
    check_extreme_moves(conn, symbols)
    log.info("")
    check_nulls(conn, symbols)

    conn.close()
    log.info("")
    log.info("Quality check complete.")


if __name__ == "__main__":
    main()
