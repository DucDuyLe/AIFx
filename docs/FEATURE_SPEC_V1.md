# Feature Spec v1

This document defines the canonical Phase 2 feature contract written to `features.feature_json`.

## Row contract

- `features.symbol`: ticker
- `features.ts`: 5-minute boundary in UTC
- `features.feature_set_version`: `v1`
- `features.feature_json`: flat JSON object (no nested arrays except optional diagnostics)

## Anti-leakage rules

- For feature row at `ts`, all source data must satisfy `source_time <= ts`.
- No forward-filled values from future bars.
- News windows are right-closed at `ts`.

## Feature keys and defaults

### Price and returns

- `ret_1`: pct return over 1 bar, default `0.0`
- `ret_3`: pct return over 3 bars, default `0.0`
- `ret_6`: pct return over 6 bars, default `0.0`
- `ret_12`: pct return over 12 bars, default `0.0`
- `log_ret_1`: log return over 1 bar, default `0.0`

### Trend and momentum

- `ema_9`: EMA(9), default `close`
- `ema_21`: EMA(21), default `close`
- `ema_50`: EMA(50), default `close`
- `ema_spread_9_21`: `(ema_9 - ema_21) / ema_21`, default `0.0`
- `ema_spread_21_50`: `(ema_21 - ema_50) / ema_50`, default `0.0`
- `rsi_14`: RSI(14), default `50.0`
- `macd`: MACD(12,26), default `0.0`
- `macd_signal`: MACD signal(9), default `0.0`
- `macd_hist`: `macd - macd_signal`, default `0.0`

### Volatility and range

- `true_range`: TR, default `0.0`
- `atr_14`: ATR(14), default `0.0`
- `realized_vol_12`: stdev of `ret_1` over 12 bars, default `0.0`
- `realized_vol_24`: stdev of `ret_1` over 24 bars, default `0.0`

### Volume and liquidity

- `dollar_vol`: `close * volume`, default `0.0`
- `vol_z_20`: z-score(volume, 20 bars), default `0.0`
- `trade_count_z_20`: z-score(trade_count, 20 bars), default `0.0`
- `vwap_dev_bps`: `(close - vwap) / vwap * 10000`, default `0.0`

### Session and time

- `minute_of_day`: minutes from 00:00 ET at bar close, default `0`
- `is_opening_window`: first 30 regular-session minutes (0/1), default `0`
- `is_closing_window`: last 30 regular-session minutes (0/1), default `0`

### Cross-sectional

- `rel_strength_vs_spy_12`: symbol `ret_12 - SPY ret_12`, default `0.0`

### Regime tags

- `regime_trend`: categorical in `{up, down, flat}`, default `flat`
- `regime_vol`: categorical in `{low, medium, high}`, default `medium`

### Sentiment windows (FinBERT + relevancy weighting)

Sentiment source: `ProsusAI/finbert` (local inference), cached in `news_sentiment_cache`.
Relevancy weighting: `exclusivity = 1/symbol_count`, `headline_boost = 1.5x if symbol in headline`.
Aggregation: `weighted_mean = sum(score * relevancy) / sum(relevancy)`.

- `news_count_30m`: count of tagged articles in last 30m, default `0`
- `news_count_2h`: count in last 2h, default `0`
- `news_count_1d`: count in last 1d, default `0`
- `sent_mean_30m`: relevancy-weighted mean FinBERT sentiment in 30m, default `0.0`
- `sent_mean_2h`: relevancy-weighted mean in 2h, default `0.0`
- `sent_mean_1d`: relevancy-weighted mean in 1d, default `0.0`
- `sent_std_2h`: stddev of FinBERT scores in 2h, default `0.0`
- `sent_trend_2h`: recent-vs-earlier mean difference in 2h, default `0.0`
- `headline_impact_max_1d`: max absolute relevancy-weighted sentiment in 1d, default `0.0`
- `time_since_last_news_min`: minutes since latest tagged article, default `99999.0`

## Example `feature_json` (minimal)

```json
{
  "ret_1": 0.0012,
  "ema_9": 213.45,
  "ema_21": 213.08,
  "rsi_14": 56.2,
  "atr_14": 0.84,
  "vol_z_20": 1.1,
  "vwap_dev_bps": 4.7,
  "minute_of_day": 605,
  "regime_trend": "up",
  "regime_vol": "medium",
  "news_count_30m": 2,
  "sent_mean_30m": 0.38,
  "time_since_last_news_min": 7.0
}
```
