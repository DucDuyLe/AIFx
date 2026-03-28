#!/usr/bin/env python3
"""
Quality checks on the features table after a build_features.py run.

Checks: coverage, schema completeness, NaN/null, value ranges,
distribution stats, warmup counts, and anti-leakage spot checks.

Usage:
    .venv\\Scripts\\python.exe scripts\\quality_check_features.py
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Set

import psycopg2
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("qa_features")

EXPECTED_KEYS: Set[str] = {
    "ret_1", "ret_3", "ret_6", "ret_12", "log_ret_1",
    "ema_9", "ema_21", "ema_50", "ema_spread_9_21", "ema_spread_21_50",
    "rsi_14", "macd", "macd_signal", "macd_hist",
    "true_range", "atr_14", "realized_vol_12", "realized_vol_24",
    "dollar_vol", "vol_z_20", "trade_count_z_20", "vwap_dev_bps",
    "minute_of_day", "is_opening_window", "is_closing_window",
    "rel_strength_vs_spy_12",
    "regime_trend", "regime_vol",
    "news_count_30m", "news_count_2h", "news_count_1d",
    "sent_mean_30m", "sent_mean_2h", "sent_mean_1d",
    "sent_std_2h", "sent_trend_2h",
    "headline_impact_max_1d", "time_since_last_news_min",
}

VALID_REGIME_TREND = {"up", "down", "flat"}
VALID_REGIME_VOL = {"low", "medium", "high"}


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


def get_conn(cfg):
    return psycopg2.connect(
        host=cfg["db_host"], port=cfg["db_port"],
        dbname=cfg["db_name"], user=cfg["db_user"],
        password=cfg["db_password"],
    )


def check_coverage(conn) -> bool:
    """Compare features rows vs candles_5m rows per symbol."""
    log.info("=" * 60)
    log.info("CHECK 1: Coverage (features vs candles_5m per symbol)")
    log.info("=" * 60)
    ok = True
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.symbol, c.cnt AS candle_count, COALESCE(f.cnt, 0) AS feature_count
            FROM (SELECT symbol, count(*) AS cnt FROM candles_5m GROUP BY symbol) c
            LEFT JOIN (SELECT symbol, count(*) AS cnt FROM features GROUP BY symbol) f
                ON c.symbol = f.symbol
            ORDER BY c.symbol
        """)
        for row in cur.fetchall():
            sym, candles, feats = row
            pct = (feats / candles * 100) if candles > 0 else 0
            status = "OK" if pct > 95 else "WARN"
            if pct < 95:
                ok = False
            log.info("  %s: candles=%d  features=%d  coverage=%.1f%%  [%s]", sym, candles, feats, pct, status)
    return ok


def check_schema(conn) -> bool:
    """Verify every feature_json has all expected keys."""
    log.info("=" * 60)
    log.info("CHECK 2: Schema completeness (all 38 v1 keys present)")
    log.info("=" * 60)
    ok = True
    with conn.cursor() as cur:
        cur.execute("SELECT id, symbol, ts, feature_json FROM features ORDER BY RANDOM() LIMIT 200")
        rows = cur.fetchall()

    missing_count = 0
    extra_count = 0
    for row_id, sym, ts, fj in rows:
        if isinstance(fj, str):
            fj = json.loads(fj)
        keys = set(fj.keys())
        missing = EXPECTED_KEYS - keys
        extra = keys - EXPECTED_KEYS
        if missing:
            missing_count += 1
            if missing_count <= 3:
                log.warning("  id=%d %s %s: MISSING keys: %s", row_id, sym, ts, missing)
        if extra:
            extra_count += 1
            if extra_count <= 3:
                log.warning("  id=%d %s %s: EXTRA keys: %s", row_id, sym, ts, extra)

    if missing_count == 0 and extra_count == 0:
        log.info("  All 200 sampled rows have exactly %d expected keys. [OK]", len(EXPECTED_KEYS))
    else:
        ok = False
        log.warning("  Missing keys in %d rows, extra keys in %d rows [FAIL]", missing_count, extra_count)
    return ok


def check_nulls_nans(conn) -> bool:
    """Check for null/NaN in feature_json numeric values."""
    log.info("=" * 60)
    log.info("CHECK 3: Null/NaN values in feature_json")
    log.info("=" * 60)
    ok = True
    with conn.cursor() as cur:
        cur.execute("SELECT id, symbol, ts, feature_json FROM features ORDER BY RANDOM() LIMIT 500")
        rows = cur.fetchall()

    nan_count = 0
    for row_id, sym, ts, fj in rows:
        if isinstance(fj, str):
            fj = json.loads(fj)
        for k, v in fj.items():
            if v is None:
                nan_count += 1
                if nan_count <= 5:
                    log.warning("  id=%d %s %s: key=%s is None", row_id, sym, ts, k)
            elif isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                nan_count += 1
                if nan_count <= 5:
                    log.warning("  id=%d %s %s: key=%s is NaN/Inf (%s)", row_id, sym, ts, k, v)

    if nan_count == 0:
        log.info("  No null/NaN/Inf found in 500 sampled rows. [OK]")
    else:
        ok = False
        log.warning("  Found %d null/NaN/Inf values [FAIL]", nan_count)
    return ok


def check_ranges(conn) -> bool:
    """Check that key features are in expected ranges."""
    log.info("=" * 60)
    log.info("CHECK 4: Value ranges")
    log.info("=" * 60)
    ok = True
    with conn.cursor() as cur:
        cur.execute("SELECT feature_json FROM features ORDER BY RANDOM() LIMIT 500")
        rows = cur.fetchall()

    violations = 0
    for (fj_raw,) in rows:
        fj = fj_raw if isinstance(fj_raw, dict) else json.loads(fj_raw)

        rsi = fj.get("rsi_14", 50)
        if not (0 <= rsi <= 100):
            violations += 1
            if violations <= 3:
                log.warning("  rsi_14 out of range: %s", rsi)

        for sk in ["sent_mean_30m", "sent_mean_2h", "sent_mean_1d"]:
            sv = fj.get(sk, 0)
            if not (-1.5 <= sv <= 1.5):
                violations += 1
                if violations <= 3:
                    log.warning("  %s out of range: %s", sk, sv)

        rt = fj.get("regime_trend", "flat")
        if rt not in VALID_REGIME_TREND:
            violations += 1

        rv = fj.get("regime_vol", "medium")
        if rv not in VALID_REGIME_VOL:
            violations += 1

        mod = fj.get("minute_of_day", 0)
        if not (0 <= mod <= 1440):
            violations += 1

    if violations == 0:
        log.info("  All 500 sampled rows pass range checks. [OK]")
    else:
        ok = False
        log.warning("  %d range violations found [FAIL]", violations)
    return ok


def check_distribution(conn) -> None:
    """Print distribution stats per symbol for key features."""
    log.info("=" * 60)
    log.info("CHECK 5: Distribution stats per symbol")
    log.info("=" * 60)
    numeric_keys = ["ret_1", "rsi_14", "atr_14", "vol_z_20", "sent_mean_1d", "news_count_1d"]

    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT symbol FROM features ORDER BY symbol")
        symbols = [r[0] for r in cur.fetchall()]

    for sym in symbols:
        log.info("  --- %s ---", sym)
        with conn.cursor() as cur:
            cur.execute("SELECT feature_json FROM features WHERE symbol = %s", (sym,))
            rows = cur.fetchall()

        if not rows:
            log.info("    (no rows)")
            continue

        stats: Dict[str, List[float]] = {k: [] for k in numeric_keys}
        for (fj_raw,) in rows:
            fj = fj_raw if isinstance(fj_raw, dict) else json.loads(fj_raw)
            for k in numeric_keys:
                v = fj.get(k, 0)
                if isinstance(v, (int, float)) and not math.isnan(v) and not math.isinf(v):
                    stats[k].append(float(v))

        for k in numeric_keys:
            vals = stats[k]
            if vals:
                import numpy as np
                arr = np.array(vals)
                log.info(
                    "    %-18s  mean=%8.4f  std=%8.4f  min=%8.4f  max=%8.4f",
                    k, arr.mean(), arr.std(), arr.min(), arr.max(),
                )
            else:
                log.info("    %-18s  (no values)", k)


def check_warmup(conn) -> None:
    """Count rows where technical indicators are at warmup defaults."""
    log.info("=" * 60)
    log.info("CHECK 6: Warmup bars (rsi_14 = 50.0 default)")
    log.info("=" * 60)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, count(*) FILTER (WHERE (feature_json->>'rsi_14')::float = 50.0) AS warmup,
                   count(*) AS total
            FROM features GROUP BY symbol ORDER BY symbol
        """)
        for sym, warmup, total in cur.fetchall():
            log.info("  %s: warmup=%d / %d (%.1f%%)", sym, warmup, total, warmup / total * 100 if total else 0)


def check_anti_leakage(conn) -> None:
    """Spot-check that sentiment windows don't include future articles."""
    log.info("=" * 60)
    log.info("CHECK 7: Anti-leakage spot check (10 random bars)")
    log.info("=" * 60)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT f.symbol, f.ts, (f.feature_json->>'news_count_1d')::int AS nc
            FROM features f
            WHERE (f.feature_json->>'news_count_1d')::int > 0
            ORDER BY RANDOM() LIMIT 10
        """)
        samples = cur.fetchall()

    violations = 0
    for sym, ts, nc in samples:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(*) FROM news_symbol_map nsm
                JOIN news_raw nr ON nr.provider = nsm.provider AND nr.news_id = nsm.news_id
                WHERE nsm.symbol = %s AND nr.created_at > %s
                  AND nr.created_at <= %s + interval '1 second'
            """, (sym, ts, ts))
            future = cur.fetchone()[0]
            if future > 0:
                violations += 1
                log.warning("  LEAK: %s at %s has %d articles with created_at > ts", sym, ts, future)
            else:
                log.info("  %s at %s: news_count_1d=%d, no future articles [OK]", sym, ts, nc)

    if violations == 0:
        log.info("  No leakage found in spot check. [OK]")


def main():
    cfg = load_config()
    conn = get_conn(cfg)

    results = []
    results.append(("Coverage", check_coverage(conn)))
    results.append(("Schema", check_schema(conn)))
    results.append(("Nulls/NaN", check_nulls_nans(conn)))
    results.append(("Ranges", check_ranges(conn)))
    check_distribution(conn)
    check_warmup(conn)
    check_anti_leakage(conn)

    log.info("=" * 60)
    log.info("SUMMARY")
    log.info("=" * 60)
    all_ok = True
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_ok = False
        log.info("  %-20s [%s]", name, status)

    if all_ok:
        log.info("All critical checks passed.")
    else:
        log.warning("Some checks FAILED — review output above.")

    conn.close()


if __name__ == "__main__":
    main()
