#!/usr/bin/env python3
"""
Train a baseline classifier on the Parquet features produced by compute_features.py.

Model: LogisticRegression (liblinear) on standardized features.
Splits: time-based (sorted by open_time, 80/20 split by time).

Usage (PowerShell):
  python scripts\train_model.py --features features_5m.parquet --out models\baseline.joblib
"""

from __future__ import annotations

import argparse
import os
from typing import List

import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, roc_auc_score
from sklearn.preprocessing import StandardScaler


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train baseline classifier on features")
    p.add_argument("--features", required=True, help="Input Parquet path from compute_features.py with label_5d")
    p.add_argument("--out", required=True, help="Output model path (joblib)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    df = pd.read_parquet(args.features)
    required = ["symbol","interval","open_time","close","ret_1","vol_20","rsi_14","label_5d"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise SystemExit(f"features file missing columns: {missing}")

    df = df.sort_values("open_time").reset_index(drop=True)
    # Time split 80/20
    n = len(df)
    cut = int(n * 0.8)
    train = df.iloc[:cut]
    test = df.iloc[cut:]

    feat_cols = ["ret_1","vol_20","rsi_14"]
    X_train = train[feat_cols].to_numpy()
    X_test = test[feat_cols].to_numpy()
    y_train = train["label_5d"].to_numpy()
    y_test = test["label_5d"].to_numpy()

    scaler = StandardScaler()
    X_train_sc = scaler.fit_transform(X_train)
    X_test_sc = scaler.transform(X_test)

    clf = LogisticRegression(max_iter=1000, solver="liblinear")
    clf.fit(X_train_sc, y_train)

    proba = clf.predict_proba(X_test_sc)[:, 1]
    pred = (proba >= 0.5).astype(int)
    try:
        auc = roc_auc_score(y_test, proba)
    except Exception:
        auc = float("nan")
    print(f"AUC: {auc:.4f}")
    print(classification_report(y_test, pred, digits=4))

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    joblib.dump({"scaler": scaler, "model": clf, "features": feat_cols}, args.out)
    print(f"Saved model -> {args.out}")


if __name__ == "__main__":
    main()


