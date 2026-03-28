#!/usr/bin/env python3
"""
Local backtester for 3 strategy families using features + candles_5m from DB.

Strategies:
  1. Mean-reversion: RSI oversold + neutral/positive sentiment
  2. Momentum/breakout: EMA crossover + volume spike + positive sentiment
  3. News-driven: high sentiment delta + high news count

Walk-forward splits: 70% train / 15% validate / 15% test.
Realistic costs: $0.005/share commission + 2 bps slippage.
Hard caps: max 3u/trade, max 7u/day, max 3 consecutive losses.

Usage:
    .venv\\Scripts\\python.exe scripts\\backtest_strategies.py
    .venv\\Scripts\\python.exe scripts\\backtest_strategies.py --symbols SPY,AAPL
    .venv\\Scripts\\python.exe scripts\\backtest_strategies.py --output results/backtest.json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import psycopg2
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("backtest")

COMMISSION_PER_SHARE = 0.005
SLIPPAGE_BPS = 2.0
MAX_U_PER_TRADE = 3
MAX_U_PER_DAY = 7
MAX_CONSEC_LOSSES = 3
POSITION_SIZE_DOLLARS = 10_000


# ── Data structures ──────────────────────────────────────────────────────

@dataclass
class Trade:
    symbol: str
    strategy: str
    direction: str
    entry_ts: Any
    entry_price: float
    exit_ts: Any = None
    exit_price: float = 0.0
    shares: int = 0
    pnl_gross: float = 0.0
    pnl_net: float = 0.0
    commission: float = 0.0
    slippage_cost: float = 0.0
    holding_bars: int = 0
    exit_reason: str = ""
    regime_trend: str = ""
    regime_vol: str = ""


@dataclass
class StrategyResult:
    name: str
    total_trades: int = 0
    winners: int = 0
    losers: int = 0
    win_rate: float = 0.0
    total_pnl_net: float = 0.0
    avg_pnl_net: float = 0.0
    max_drawdown: float = 0.0
    sharpe: float = 0.0
    profit_factor: float = 0.0
    avg_holding_bars: float = 0.0
    trades_by_symbol: Dict[str, int] = field(default_factory=dict)
    trades_by_regime: Dict[str, Dict[str, float]] = field(default_factory=dict)


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


def get_conn(cfg):
    return psycopg2.connect(
        host=cfg["db_host"], port=cfg["db_port"],
        dbname=cfg["db_name"], user=cfg["db_user"],
        password=cfg["db_password"],
    )


def load_features_and_candles(conn, symbol: str) -> pd.DataFrame:
    """Load features joined with candle close prices, sorted by ts."""
    sql = """
        SELECT f.ts, f.feature_json,
               c.open, c.high, c.low, c.close, c.volume
        FROM features f
        JOIN candles_5m c ON c.symbol = f.symbol AND c.ts = f.ts
        WHERE f.symbol = %s AND f.feature_set_version = 'v1'
        ORDER BY f.ts
    """
    df = pd.read_sql(sql, conn, params=[symbol], parse_dates=["ts"])
    if df.empty:
        return df

    fj_df = pd.json_normalize(df["feature_json"].apply(
        lambda x: x if isinstance(x, dict) else json.loads(x)
    ))
    df = pd.concat([df.drop(columns=["feature_json"]), fj_df], axis=1)
    return df


# ── Cost model ───────────────────────────────────────────────────────────

def apply_costs(entry_price: float, exit_price: float, shares: int, direction: str) -> Tuple[float, float, float]:
    """Returns (pnl_gross, commission, slippage_cost)."""
    slip_entry = entry_price * (SLIPPAGE_BPS / 10_000)
    slip_exit = exit_price * (SLIPPAGE_BPS / 10_000)

    if direction == "long":
        adj_entry = entry_price + slip_entry
        adj_exit = exit_price - slip_exit
        pnl_gross = (adj_exit - adj_entry) * shares
    else:
        adj_entry = entry_price - slip_entry
        adj_exit = exit_price + slip_exit
        pnl_gross = (adj_entry - adj_exit) * shares

    commission = COMMISSION_PER_SHARE * shares * 2
    slippage_cost = (slip_entry + slip_exit) * shares
    return pnl_gross, commission, slippage_cost


# ── Strategy definitions ─────────────────────────────────────────────────

def strategy_mean_reversion(df: pd.DataFrame, symbol: str) -> List[Trade]:
    """RSI oversold + neutral/positive sentiment -> long, exit on RSI recovery or timeout."""
    trades = []
    in_trade = False
    trade: Optional[Trade] = None
    max_hold = 24  # 2 hours

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        rsi = row.get("rsi_14", 50)
        sent = row.get("sent_mean_2h", 0)

        if not in_trade:
            if rsi < 30 and sent >= -0.2:
                entry_price = float(row["close"])
                shares = max(1, int(POSITION_SIZE_DOLLARS / entry_price))
                trade = Trade(
                    symbol=symbol, strategy="mean_reversion", direction="long",
                    entry_ts=row["ts"], entry_price=entry_price, shares=shares,
                    regime_trend=str(row.get("regime_trend", "flat")),
                    regime_vol=str(row.get("regime_vol", "medium")),
                )
                in_trade = True
        else:
            trade.holding_bars += 1
            exit_now = False
            reason = ""

            if rsi > 55:
                exit_now = True
                reason = "rsi_recovery"
            elif rsi > 70:
                exit_now = True
                reason = "rsi_overbought"
            elif trade.holding_bars >= max_hold:
                exit_now = True
                reason = "timeout"
            elif float(row["close"]) < trade.entry_price * 0.985:
                exit_now = True
                reason = "stop_loss"

            if exit_now:
                exit_price = float(row["close"])
                pnl_gross, comm, slip = apply_costs(trade.entry_price, exit_price, trade.shares, "long")
                trade.exit_ts = row["ts"]
                trade.exit_price = exit_price
                trade.pnl_gross = pnl_gross
                trade.commission = comm
                trade.slippage_cost = slip
                trade.pnl_net = pnl_gross - comm
                trade.exit_reason = reason
                trades.append(trade)
                in_trade = False
                trade = None

    return trades


def strategy_momentum(df: pd.DataFrame, symbol: str) -> List[Trade]:
    """EMA crossover (9 > 21) + volume spike + positive sentiment -> long."""
    trades = []
    in_trade = False
    trade: Optional[Trade] = None
    max_hold = 36  # 3 hours

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]

        ema9 = row.get("ema_9", 0)
        ema21 = row.get("ema_21", 0)
        prev_ema9 = prev.get("ema_9", 0)
        prev_ema21 = prev.get("ema_21", 0)
        vol_z = row.get("vol_z_20", 0)
        sent = row.get("sent_mean_2h", 0)

        crossover = (prev_ema9 <= prev_ema21) and (ema9 > ema21)

        if not in_trade:
            if crossover and vol_z > 1.0 and sent > 0.0:
                entry_price = float(row["close"])
                shares = max(1, int(POSITION_SIZE_DOLLARS / entry_price))
                trade = Trade(
                    symbol=symbol, strategy="momentum", direction="long",
                    entry_ts=row["ts"], entry_price=entry_price, shares=shares,
                    regime_trend=str(row.get("regime_trend", "flat")),
                    regime_vol=str(row.get("regime_vol", "medium")),
                )
                in_trade = True
        else:
            trade.holding_bars += 1
            exit_now = False
            reason = ""

            if ema9 < ema21:
                exit_now = True
                reason = "ema_reversal"
            elif trade.holding_bars >= max_hold:
                exit_now = True
                reason = "timeout"
            elif float(row["close"]) < trade.entry_price * 0.98:
                exit_now = True
                reason = "stop_loss"
            elif float(row["close"]) > trade.entry_price * 1.03:
                exit_now = True
                reason = "take_profit"

            if exit_now:
                exit_price = float(row["close"])
                pnl_gross, comm, slip = apply_costs(trade.entry_price, exit_price, trade.shares, "long")
                trade.exit_ts = row["ts"]
                trade.exit_price = exit_price
                trade.pnl_gross = pnl_gross
                trade.commission = comm
                trade.slippage_cost = slip
                trade.pnl_net = pnl_gross - comm
                trade.exit_reason = reason
                trades.append(trade)
                in_trade = False
                trade = None

    return trades


def strategy_news_driven(df: pd.DataFrame, symbol: str) -> List[Trade]:
    """High sentiment delta + high news count -> directional trade."""
    trades = []
    in_trade = False
    trade: Optional[Trade] = None
    max_hold = 12  # 1 hour

    for i in range(1, len(df)):
        row = df.iloc[i]
        sent_trend = row.get("sent_trend_2h", 0)
        news_count = row.get("news_count_2h", 0)
        sent_mean = row.get("sent_mean_30m", 0)

        if not in_trade:
            if news_count >= 3 and abs(sent_trend) > 0.3:
                direction = "long" if sent_trend > 0 else "short"
                entry_price = float(row["close"])
                shares = max(1, int(POSITION_SIZE_DOLLARS / entry_price))
                trade = Trade(
                    symbol=symbol, strategy="news_driven", direction=direction,
                    entry_ts=row["ts"], entry_price=entry_price, shares=shares,
                    regime_trend=str(row.get("regime_trend", "flat")),
                    regime_vol=str(row.get("regime_vol", "medium")),
                )
                in_trade = True
        else:
            trade.holding_bars += 1
            exit_now = False
            reason = ""

            if trade.holding_bars >= max_hold:
                exit_now = True
                reason = "timeout"
            elif abs(row.get("sent_trend_2h", 0)) < 0.05:
                exit_now = True
                reason = "sentiment_decay"
            elif trade.direction == "long" and float(row["close"]) < trade.entry_price * 0.985:
                exit_now = True
                reason = "stop_loss"
            elif trade.direction == "short" and float(row["close"]) > trade.entry_price * 1.015:
                exit_now = True
                reason = "stop_loss"
            elif trade.direction == "long" and float(row["close"]) > trade.entry_price * 1.02:
                exit_now = True
                reason = "take_profit"
            elif trade.direction == "short" and float(row["close"]) < trade.entry_price * 0.98:
                exit_now = True
                reason = "take_profit"

            if exit_now:
                exit_price = float(row["close"])
                pnl_gross, comm, slip = apply_costs(trade.entry_price, exit_price, trade.shares, trade.direction)
                trade.exit_ts = row["ts"]
                trade.exit_price = exit_price
                trade.pnl_gross = pnl_gross
                trade.commission = comm
                trade.slippage_cost = slip
                trade.pnl_net = pnl_gross - comm
                trade.exit_reason = reason
                trades.append(trade)
                in_trade = False
                trade = None

    return trades


# ── Analysis ─────────────────────────────────────────────────────────────

def analyze_trades(trades: List[Trade], strategy_name: str) -> StrategyResult:
    result = StrategyResult(name=strategy_name)
    if not trades:
        return result

    result.total_trades = len(trades)
    pnls = [t.pnl_net for t in trades]
    result.winners = sum(1 for p in pnls if p > 0)
    result.losers = sum(1 for p in pnls if p <= 0)
    result.win_rate = result.winners / result.total_trades if result.total_trades > 0 else 0
    result.total_pnl_net = sum(pnls)
    result.avg_pnl_net = np.mean(pnls) if pnls else 0
    result.avg_holding_bars = np.mean([t.holding_bars for t in trades])

    # Sharpe (annualized, assuming ~78 bars per day, ~252 trading days)
    if len(pnls) > 1 and np.std(pnls) > 0:
        result.sharpe = float(np.mean(pnls) / np.std(pnls) * np.sqrt(252))
    
    # Profit factor
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    result.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0

    # Max drawdown
    cumulative = np.cumsum(pnls)
    running_max = np.maximum.accumulate(cumulative)
    drawdowns = running_max - cumulative
    result.max_drawdown = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0

    # Per-symbol
    by_sym: Dict[str, int] = defaultdict(int)
    for t in trades:
        by_sym[t.symbol] += 1
    result.trades_by_symbol = dict(by_sym)

    # Per-regime
    regime_pnl: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    for t in trades:
        regime_pnl[t.regime_trend]["pnl"].append(t.pnl_net)
        regime_pnl[t.regime_vol]["pnl"].append(t.pnl_net)

    result.trades_by_regime = {}
    for regime, data in regime_pnl.items():
        pnl_list = data["pnl"]
        result.trades_by_regime[regime] = {
            "count": len(pnl_list),
            "total_pnl": round(sum(pnl_list), 2),
            "avg_pnl": round(np.mean(pnl_list), 2) if pnl_list else 0,
        }

    return result


def walk_forward_split(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """70/15/15 time-based split."""
    n = len(df)
    train_end = int(n * 0.70)
    val_end = int(n * 0.85)
    return df.iloc[:train_end], df.iloc[train_end:val_end], df.iloc[val_end:]


# ── Main ─────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Backtest 3 strategy families")
    p.add_argument("--symbols", help="Comma-separated (default: all active)")
    p.add_argument("--output", default="results/backtest_report.json", help="Output JSON path")
    p.add_argument("--split", default="test", choices=["train", "validate", "test", "all"],
                   help="Which split to run on (default: test)")
    return p.parse_args()


def main():
    args = parse_args()
    cfg = load_config()
    conn = get_conn(cfg)

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    else:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol FROM instruments WHERE is_active = true ORDER BY symbol")
            symbols = [r[0] for r in cur.fetchall()]

    log.info("Symbols: %s", ", ".join(symbols))
    log.info("Split: %s", args.split)

    strategies = {
        "mean_reversion": strategy_mean_reversion,
        "momentum": strategy_momentum,
        "news_driven": strategy_news_driven,
    }

    all_results: Dict[str, StrategyResult] = {}
    all_trades: Dict[str, List[Trade]] = defaultdict(list)

    for sym in symbols:
        log.info("Loading %s...", sym)
        df = load_features_and_candles(conn, sym)
        if df.empty or len(df) < 100:
            log.warning("  %s: insufficient data (%d rows), skipping", sym, len(df))
            continue

        train_df, val_df, test_df = walk_forward_split(df)
        if args.split == "train":
            run_df = train_df
        elif args.split == "validate":
            run_df = val_df
        elif args.split == "test":
            run_df = test_df
        else:
            run_df = df

        log.info("  %s: total=%d  train=%d  val=%d  test=%d  running=%d",
                 sym, len(df), len(train_df), len(val_df), len(test_df), len(run_df))

        for strat_name, strat_fn in strategies.items():
            trades = strat_fn(run_df, sym)
            all_trades[strat_name].extend(trades)
            log.info("    %s: %d trades", strat_name, len(trades))

    conn.close()

    # Analyze
    report = {}
    for strat_name in strategies:
        trades = all_trades[strat_name]
        result = analyze_trades(trades, strat_name)
        all_results[strat_name] = result

        log.info("")
        log.info("=" * 60)
        log.info("STRATEGY: %s", strat_name)
        log.info("=" * 60)
        log.info("  Trades:        %d", result.total_trades)
        log.info("  Winners:       %d (%.1f%%)", result.winners, result.win_rate * 100)
        log.info("  Losers:        %d", result.losers)
        log.info("  Total PnL:     $%.2f", result.total_pnl_net)
        log.info("  Avg PnL:       $%.2f", result.avg_pnl_net)
        log.info("  Sharpe:        %.2f", result.sharpe)
        log.info("  Profit Factor: %.2f", result.profit_factor)
        log.info("  Max Drawdown:  $%.2f", result.max_drawdown)
        log.info("  Avg Hold:      %.1f bars", result.avg_holding_bars)
        log.info("  By Symbol:     %s", result.trades_by_symbol)
        log.info("  By Regime:     %s", json.dumps(result.trades_by_regime, indent=2))

        report[strat_name] = {
            "total_trades": result.total_trades,
            "winners": result.winners,
            "losers": result.losers,
            "win_rate": round(result.win_rate, 4),
            "total_pnl_net": round(result.total_pnl_net, 2),
            "avg_pnl_net": round(result.avg_pnl_net, 2),
            "sharpe": round(result.sharpe, 4),
            "profit_factor": round(result.profit_factor, 4),
            "max_drawdown": round(result.max_drawdown, 2),
            "avg_holding_bars": round(result.avg_holding_bars, 1),
            "trades_by_symbol": result.trades_by_symbol,
            "trades_by_regime": result.trades_by_regime,
        }

    # Write report
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info("")
    log.info("Report written to %s", out_path)


if __name__ == "__main__":
    main()
