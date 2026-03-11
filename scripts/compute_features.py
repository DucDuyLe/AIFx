#!/usr/bin/env python3
"""
Compute simple features and labels from Polygon-format CSVs.

Inputs: one or more CSVs in data/ with columns matching polygon_downloader/binance_downloader outputs.
Output: Parquet dataset with columns: symbol, interval, open_time, close, ret_1, rsi_14, vol_20, label_5d (if make_labels).

Usage (PowerShell):
  python scripts\compute_features.py --glob "data/*_5m_*.csv" --out features_5m.parquet --interval 5m --make-labels
  python scripts\compute_features.py --glob "data/*_5m_*.csv" --out features_5m.parquet --interval 5m --symbols AAPL,MSFT --limit 50
"""

from __future__ import annotations

import argparse
import glob
import os
from datetime import datetime
from typing import Iterable, List, Optional

import numpy as np
import pandas as pd


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build features from CSV OHLCV files")
    p.add_argument("--glob", required=True, help="Glob for input CSVs, e.g., data/*_5m_*.csv")
    p.add_argument("--out", required=True, help="Output Parquet path")
    p.add_argument("--interval", required=True, choices=["1m", "5m"], help="Interval of data")
    p.add_argument("--symbols", help="Comma-separated symbols to include (optional)")
    p.add_argument("--limit", type=int, default=0, help="Max number of files to process (0=all)")
    p.add_argument("--make-labels", action="store_true", help="Create forward 5-bar return label")
    return p.parse_args()


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = np.maximum(delta, 0.0)
    down = np.maximum(-delta, 0.0)
    roll_up = pd.Series(up).ewm(alpha=1/period, adjust=False).mean()
    roll_down = pd.Series(down).ewm(alpha=1/period, adjust=False).mean()
    rs = roll_up / (roll_down + 1e-12)
    rsi = 100 - (100 / (1 + rs))
    return pd.Series(rsi, index=series.index)


def load_one_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = [
        "symbol","interval","open_time_iso","open","high","low","close","volume"
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"{path} missing columns: {missing}")
    df = df[required].copy()
    df["open_time"] = pd.to_datetime(df["open_time_iso"], utc=True)
    df["open"] = pd.to_numeric(df["open"], errors="coerce")
    df["high"] = pd.to_numeric(df["high"], errors="coerce")
    df["low"] = pd.to_numeric(df["low"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df = df.dropna(subset=["open","high","low","close"]).reset_index(drop=True)
    return df


def build_features(df: pd.DataFrame, make_labels: bool) -> pd.DataFrame:
    df = df.sort_values("open_time").copy()
    df["ret_1"] = df["close"].pct_change().fillna(0.0)
    df["vol_20"] = df["ret_1"].rolling(20).std().fillna(method="bfill").fillna(0.0)
    df["rsi_14"] = compute_rsi(df["close"], 14).fillna(method="bfill").fillna(50.0)
    if make_labels:
        df["fwd_ret_5"] = df["close"].pct_change(periods=5).shift(-5)
        df["label_5d"] = (df["fwd_ret_5"] > 0).astype(int)
    return df


def main() -> None:
    args = parse_args()
    symbols_filter = set([s.strip().upper() for s in args.symbols.split(",")]) if args.symbols else None
    files = sorted(glob.glob(args.glob))
    if args.limit and len(files) > args.limit:
        files = files[: args.limit]

    out_rows: List[pd.DataFrame] = []
    for path in files:
        try:
            df = load_one_csv(path)
        except Exception as exc:
            print(f"skip {path}: {exc}")
            continue
        if symbols_filter is not None:
            sym = str(df["symbol"].iloc[0]).upper()
            if sym not in symbols_filter:
                continue
        feat = build_features(df, args.make_labels)
        keep_cols = [
            "symbol","interval","open_time","close","ret_1","vol_20","rsi_14"
        ] + (["label_5d"] if args.make_labels else [])
        out_rows.append(feat[keep_cols])

    if not out_rows:
        print("No data")
        return

    out_df = pd.concat(out_rows, ignore_index=True)
    out_df.to_parquet(args.out, index=False)
    print(f"Wrote {len(out_df):,} rows -> {args.out}")


if __name__ == "__main__":
    main()


