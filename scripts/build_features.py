#!/usr/bin/env python3
"""
Build Feature Spec v1 rows for all active symbols.

Reads candles_5m + news_sentiment_cache + news_symbol_map + news_raw,
computes technical indicators and sentiment window aggregates,
writes versioned rows to the features table.

Usage:
    .venv\\Scripts\\python.exe scripts\\build_features.py
    .venv\\Scripts\\python.exe scripts\\build_features.py --symbols SPY,AAPL
    .venv\\Scripts\\python.exe scripts\\build_features.py --start 2025-01-01 --end 2025-06-01
    .venv\\Scripts\\python.exe scripts\\build_features.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("build_features")

FEATURE_SET_VERSION = "v1"
BATCH_INSERT_SIZE = 1000


# ── CLI ──────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build v1 feature rows")
    p.add_argument("--symbols", help="Comma-separated symbols (default: all active)")
    p.add_argument("--start", help="Start date YYYY-MM-DD (default: earliest bar)")
    p.add_argument("--end", help="End date YYYY-MM-DD (default: latest bar)")
    p.add_argument("--batch-size", type=int, default=BATCH_INSERT_SIZE)
    p.add_argument("--dry-run", action="store_true", help="Compute but don't write")
    return p.parse_args()


# ── DB helpers ───────────────────────────────────────────────────────────

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


def get_db_conn(cfg: Dict[str, str]):
    return psycopg2.connect(
        host=cfg["db_host"], port=cfg["db_port"],
        dbname=cfg["db_name"], user=cfg["db_user"],
        password=cfg["db_password"],
    )


def get_active_symbols(conn) -> List[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM public.instruments WHERE is_active = true ORDER BY symbol")
        return [r[0] for r in cur.fetchall()]


def load_candles(conn, symbol: str, start: Optional[str], end: Optional[str]) -> pd.DataFrame:
    sql = "SELECT ts, open, high, low, close, volume, vwap, trade_count FROM public.candles_5m WHERE symbol = %s"
    params: list = [symbol]
    if start:
        sql += " AND ts >= %s"
        params.append(start)
    if end:
        sql += " AND ts <= %s"
        params.append(end)
    sql += " ORDER BY ts"
    return pd.read_sql(sql, conn, params=params, parse_dates=["ts"])


def load_sentiment_for_symbol(conn, symbol: str) -> pd.DataFrame:
    """Load all sentiment data relevant to a symbol, pre-joined."""
    sql = """
        SELECT nr.created_at, nsc.sentiment_score, nr.headline,
               (SELECT count(*) FROM public.news_symbol_map nsm2
                WHERE nsm2.provider = nsm.provider AND nsm2.news_id = nsm.news_id) AS symbol_count
        FROM public.news_symbol_map nsm
        JOIN public.news_raw nr ON nr.provider = nsm.provider AND nr.news_id = nsm.news_id
        JOIN public.news_sentiment_cache nsc ON nsc.provider = nsm.provider AND nsc.news_id = nsm.news_id
        WHERE nsm.symbol = %s
        ORDER BY nr.created_at
    """
    df = pd.read_sql(sql, conn, params=[symbol], parse_dates=["created_at"])
    return df


# ── Technical indicators ────────────────────────────────────────────────

def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def compute_technical(df: pd.DataFrame) -> pd.DataFrame:
    """Add all technical indicator columns to candles DataFrame."""
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    lo = df["low"].astype(float)
    v = df["volume"].astype(float)
    vwap = df["vwap"].astype(float)
    tc = df["trade_count"].astype(float)
    prev_c = c.shift(1)

    # Returns
    df["ret_1"] = c.pct_change(1)
    df["ret_3"] = c.pct_change(3)
    df["ret_6"] = c.pct_change(6)
    df["ret_12"] = c.pct_change(12)
    df["log_ret_1"] = np.log(c / prev_c)

    # EMAs
    df["ema_9"] = c.ewm(span=9, adjust=False).mean()
    df["ema_21"] = c.ewm(span=21, adjust=False).mean()
    df["ema_50"] = c.ewm(span=50, adjust=False).mean()
    df["ema_spread_9_21"] = (df["ema_9"] - df["ema_21"]) / df["ema_21"]
    df["ema_spread_21_50"] = (df["ema_21"] - df["ema_50"]) / df["ema_50"]

    # RSI
    df["rsi_14"] = compute_rsi(c, 14)

    # MACD
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # Volatility
    df["true_range"] = pd.concat([
        h - lo,
        (h - prev_c).abs(),
        (lo - prev_c).abs(),
    ], axis=1).max(axis=1)
    df["atr_14"] = df["true_range"].ewm(span=14, adjust=False).mean()
    df["realized_vol_12"] = df["ret_1"].rolling(12).std()
    df["realized_vol_24"] = df["ret_1"].rolling(24).std()

    # Volume / liquidity
    df["dollar_vol"] = c * v
    vol_mean_20 = v.rolling(20).mean()
    vol_std_20 = v.rolling(20).std().replace(0, np.nan)
    df["vol_z_20"] = (v - vol_mean_20) / vol_std_20

    tc_mean_20 = tc.rolling(20).mean()
    tc_std_20 = tc.rolling(20).std().replace(0, np.nan)
    df["trade_count_z_20"] = (tc - tc_mean_20) / tc_std_20

    vwap_safe = vwap.replace(0, np.nan)
    df["vwap_dev_bps"] = (c - vwap_safe) / vwap_safe * 10000.0

    # Session / time (convert ts to ET)
    ts_et = df["ts"].dt.tz_convert("America/New_York")
    df["minute_of_day"] = ts_et.dt.hour * 60 + ts_et.dt.minute
    df["is_opening_window"] = ((df["minute_of_day"] >= 570) & (df["minute_of_day"] < 600)).astype(int)
    df["is_closing_window"] = ((df["minute_of_day"] >= 930) & (df["minute_of_day"] < 960)).astype(int)

    # Regime tags
    spread_pct = (df["ema_9"] - df["ema_50"]) / df["ema_50"]
    df["regime_trend"] = np.where(spread_pct > 0.002, "up", np.where(spread_pct < -0.002, "down", "flat"))

    vol_rank = df["realized_vol_24"].rolling(200, min_periods=50).rank(pct=True)
    df["regime_vol"] = np.where(vol_rank > 0.67, "high", np.where(vol_rank < 0.33, "low", "medium"))

    return df


# ── Sentiment window aggregation ────────────────────────────────────────

def compute_sentiment_features(
    bar_ts: pd.Timestamp,
    symbol: str,
    sent_df: pd.DataFrame,
) -> Dict[str, Any]:
    """Compute sentiment window features for a single bar timestamp."""
    defaults = {
        "news_count_30m": 0, "news_count_2h": 0, "news_count_1d": 0,
        "sent_mean_30m": 0.0, "sent_mean_2h": 0.0, "sent_mean_1d": 0.0,
        "sent_std_2h": 0.0, "sent_trend_2h": 0.0,
        "headline_impact_max_1d": 0.0, "time_since_last_news_min": 99999.0,
    }
    if sent_df.empty:
        return defaults

    ts = bar_ts
    w30 = sent_df[(sent_df["created_at"] > ts - pd.Timedelta(minutes=30)) & (sent_df["created_at"] <= ts)]
    w2h = sent_df[(sent_df["created_at"] > ts - pd.Timedelta(hours=2)) & (sent_df["created_at"] <= ts)]
    w1d = sent_df[(sent_df["created_at"] > ts - pd.Timedelta(hours=24)) & (sent_df["created_at"] <= ts)]

    sym_upper = symbol.upper()

    def weighted_mean(window_df: pd.DataFrame) -> float:
        if window_df.empty:
            return 0.0
        excl = 1.0 / window_df["symbol_count"].clip(lower=1)
        h_boost = window_df["headline"].str.upper().str.contains(sym_upper, na=False).astype(float) * 0.5 + 1.0
        w = excl * h_boost
        return float((window_df["sentiment_score"] * w).sum() / w.sum())

    result = dict(defaults)
    result["news_count_30m"] = len(w30)
    result["news_count_2h"] = len(w2h)
    result["news_count_1d"] = len(w1d)
    result["sent_mean_30m"] = weighted_mean(w30)
    result["sent_mean_2h"] = weighted_mean(w2h)
    result["sent_mean_1d"] = weighted_mean(w1d)

    if len(w2h) >= 2:
        result["sent_std_2h"] = float(w2h["sentiment_score"].std())

    if len(w2h) >= 2:
        mid = ts - pd.Timedelta(hours=1)
        recent = w2h[w2h["created_at"] > mid]["sentiment_score"]
        earlier = w2h[w2h["created_at"] <= mid]["sentiment_score"]
        r_mean = recent.mean() if len(recent) > 0 else 0.0
        e_mean = earlier.mean() if len(earlier) > 0 else 0.0
        result["sent_trend_2h"] = float(r_mean - e_mean)

    if not w1d.empty:
        excl = 1.0 / w1d["symbol_count"].clip(lower=1)
        h_boost = w1d["headline"].str.upper().str.contains(sym_upper, na=False).astype(float) * 0.5 + 1.0
        weighted_scores = (w1d["sentiment_score"] * excl * h_boost).abs()
        result["headline_impact_max_1d"] = float(weighted_scores.max())

    if not w1d.empty:
        latest = w1d["created_at"].max()
        result["time_since_last_news_min"] = float((ts - latest).total_seconds() / 60.0)

    return result


# ── Cross-sectional features ────────────────────────────────────────────

def add_cross_sectional(features_by_symbol: Dict[str, pd.DataFrame]) -> None:
    """Add rel_strength_vs_spy_12 using SPY ret_12 as benchmark."""
    spy_df = features_by_symbol.get("SPY")
    if spy_df is None:
        for sym, df in features_by_symbol.items():
            df["rel_strength_vs_spy_12"] = 0.0
        return

    spy_ret = spy_df.set_index("ts")["ret_12"].rename("spy_ret_12")
    for sym, df in features_by_symbol.items():
        merged = df.set_index("ts").join(spy_ret, how="left")
        df["rel_strength_vs_spy_12"] = (merged["ret_12"] - merged["spy_ret_12"].fillna(0)).values


# ── Assembly ─────────────────────────────────────────────────────────────

TECH_KEYS = [
    "ret_1", "ret_3", "ret_6", "ret_12", "log_ret_1",
    "ema_9", "ema_21", "ema_50", "ema_spread_9_21", "ema_spread_21_50",
    "rsi_14", "macd", "macd_signal", "macd_hist",
    "true_range", "atr_14", "realized_vol_12", "realized_vol_24",
    "dollar_vol", "vol_z_20", "trade_count_z_20", "vwap_dev_bps",
    "minute_of_day", "is_opening_window", "is_closing_window",
    "rel_strength_vs_spy_12",
    "regime_trend", "regime_vol",
]

SENT_KEYS = [
    "news_count_30m", "news_count_2h", "news_count_1d",
    "sent_mean_30m", "sent_mean_2h", "sent_mean_1d",
    "sent_std_2h", "sent_trend_2h",
    "headline_impact_max_1d", "time_since_last_news_min",
]

DEFAULTS = {
    "ret_1": 0.0, "ret_3": 0.0, "ret_6": 0.0, "ret_12": 0.0, "log_ret_1": 0.0,
    "ema_9": 0.0, "ema_21": 0.0, "ema_50": 0.0,
    "ema_spread_9_21": 0.0, "ema_spread_21_50": 0.0,
    "rsi_14": 50.0, "macd": 0.0, "macd_signal": 0.0, "macd_hist": 0.0,
    "true_range": 0.0, "atr_14": 0.0, "realized_vol_12": 0.0, "realized_vol_24": 0.0,
    "dollar_vol": 0.0, "vol_z_20": 0.0, "trade_count_z_20": 0.0, "vwap_dev_bps": 0.0,
    "minute_of_day": 0, "is_opening_window": 0, "is_closing_window": 0,
    "rel_strength_vs_spy_12": 0.0,
    "regime_trend": "flat", "regime_vol": "medium",
    "news_count_30m": 0, "news_count_2h": 0, "news_count_1d": 0,
    "sent_mean_30m": 0.0, "sent_mean_2h": 0.0, "sent_mean_1d": 0.0,
    "sent_std_2h": 0.0, "sent_trend_2h": 0.0,
    "headline_impact_max_1d": 0.0, "time_since_last_news_min": 99999.0,
}


def safe_val(v, default):
    """Replace NaN/Inf/None with the feature default."""
    if v is None:
        return default
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return default
    if isinstance(v, np.floating) and (np.isnan(v) or np.isinf(v)):
        return float(default) if isinstance(default, (int, float)) else default
    return v


def build_feature_json(row: pd.Series, sent_feats: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble the complete v1 feature_json from a candle row + sentiment dict."""
    fj: Dict[str, Any] = {}
    for k in TECH_KEYS:
        raw = row.get(k)
        fj[k] = safe_val(raw, DEFAULTS[k])
        if isinstance(fj[k], (np.integer, np.int64)):
            fj[k] = int(fj[k])
        elif isinstance(fj[k], (np.floating, np.float64)):
            fj[k] = round(float(fj[k]), 6)
    for k in SENT_KEYS:
        raw = sent_feats.get(k, DEFAULTS[k])
        fj[k] = safe_val(raw, DEFAULTS[k])
        if isinstance(fj[k], (np.floating, np.float64)):
            fj[k] = round(float(fj[k]), 6)
    return fj


# ── DB write ─────────────────────────────────────────────────────────────

INSERT_SQL = """
    INSERT INTO public.features (symbol, ts, feature_json, feature_set_version)
    VALUES (%s, %s, %s, %s)
"""


def insert_feature_batch(conn, rows: List[Tuple]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM public.features")
        before = cur.fetchone()[0]
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, INSERT_SQL, rows, page_size=500)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM public.features")
        after = cur.fetchone()[0]
    return after - before


def create_run(conn, meta: Dict[str, Any]) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.ingestion_runs (job_type, status, meta) VALUES ('feature_build', 'running', %s) RETURNING id",
            (json.dumps(meta),),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def finish_run(conn, run_id: int, status: str, rows_inserted: int, error_message: Optional[str] = None):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE public.ingestion_runs SET status=%s, finished_at=now(), rows_inserted=%s, error_message=%s WHERE id=%s",
            (status, rows_inserted, error_message, run_id),
        )
    conn.commit()


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    cfg = load_config()
    conn = get_db_conn(cfg)

    symbols = (
        [s.strip().upper() for s in args.symbols.split(",")]
        if args.symbols
        else get_active_symbols(conn)
    )
    log.info("Symbols: %s", ", ".join(symbols))
    log.info("Date range: %s to %s", args.start or "earliest", args.end or "latest")
    log.info("Dry run: %s", args.dry_run)

    run_meta = {"symbols": symbols, "start": args.start, "end": args.end, "dry_run": args.dry_run}
    run_id = None
    if not args.dry_run:
        run_id = create_run(conn, run_meta)

    t_start = time.time()
    total_inserted = 0
    features_by_symbol: Dict[str, pd.DataFrame] = {}

    # Phase 1: load candles + compute technical indicators per symbol
    log.info("Phase 1: loading candles and computing technical indicators...")
    for sym in symbols:
        df = load_candles(conn, sym, args.start, args.end)
        if df.empty:
            log.warning("  %s: no candles found, skipping", sym)
            continue
        df = compute_technical(df)
        df["symbol"] = sym
        features_by_symbol[sym] = df
        log.info("  %s: %d bars, technical indicators computed", sym, len(df))

    # Phase 2: cross-sectional features
    log.info("Phase 2: computing cross-sectional features...")
    add_cross_sectional(features_by_symbol)

    # Phase 3: sentiment + assembly + write
    log.info("Phase 3: sentiment aggregation + feature assembly + DB write...")
    for sym, df in features_by_symbol.items():
        sent_df = load_sentiment_for_symbol(conn, sym)
        log.info("  %s: %d sentiment articles loaded", sym, len(sent_df))

        rows_to_insert: List[Tuple] = []
        bar_count = len(df)

        for idx, (_, row) in enumerate(df.iterrows()):
            bar_ts = row["ts"]
            sent_feats = compute_sentiment_features(bar_ts, sym, sent_df)
            fj = build_feature_json(row, sent_feats)
            rows_to_insert.append((sym, bar_ts, json.dumps(fj), FEATURE_SET_VERSION))

            if len(rows_to_insert) >= args.batch_size:
                if not args.dry_run:
                    inserted = insert_feature_batch(conn, rows_to_insert)
                    total_inserted += inserted
                rows_to_insert = []

                elapsed = time.time() - t_start
                log.info(
                    "    %s: %d/%d bars  total_inserted=%d  elapsed=%.0fs",
                    sym, idx + 1, bar_count, total_inserted, elapsed,
                )

        if rows_to_insert and not args.dry_run:
            inserted = insert_feature_batch(conn, rows_to_insert)
            total_inserted += inserted

        log.info("  %s: done (%d bars processed)", sym, bar_count)

    elapsed = time.time() - t_start

    if run_id is not None:
        finish_run(conn, run_id, "success", total_inserted)

    log.info(
        "=== COMPLETE === symbols=%d  total_inserted=%d  elapsed=%.1fs",
        len(features_by_symbol), total_inserted, elapsed,
    )

    conn.close()


if __name__ == "__main__":
    main()
