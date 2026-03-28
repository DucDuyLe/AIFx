---
title: Agent Spec and I/O
date: 2026-03-29
---

# Agent Spec and I/O (v4 — Hybrid Architecture)

This file defines the system layers: deterministic pipeline, LLM agents, execution, hard-cap enforcement, and future ML upgrades (TFT, DRL, meta-classifier).

## Architecture Overview

The system has 3 core layers, 2 async agents, and 3 future ML upgrades:

| Layer | Type | LLM? | Purpose |
|-------|------|------|---------|
| **Feature Pipeline** | Deterministic code | No | Compute 42 technical/sentiment features from candles_5m + news_raw + FinBERT |
| **22 Strategy Engine** | Deterministic code | No | Evaluate 22 strategies per bar, produce trigger signals + confluence scores |
| **Signal Agent** | LLM agent | Yes | Read features + strategy triggers + raw headlines, reason about trade candidates |
| **Risk Agent** | LLM agent | Yes | Read signals + account state, size positions (0u-3u), produce proposed_orders |
| **Execution Layer** | Mostly deterministic | Minimal | Send-time checks + broker API. Optional light LLM safety review |
| **Hard-Cap Layer** | Deterministic gate | No | Final enforcement: 3u/trade, 7u/day, consecutive-loss cooldown |
| **Post-Loss Reviewer** | Async LLM agent | Yes | Analyze losses, propose improvements (triggers after daily halt or EOD) |
| **Strategy Researcher** | Async LLM agent (optional) | Yes | Rolling performance analysis, strategy ranking updates |

### Future ML upgrades (not active yet)

| Component | Type | Phase | Purpose |
|-----------|------|-------|---------|
| **Meta-Classifier** | XGBoost/LightGBM | B1 | Learns which strategy combinations + regime = profitable; outputs meta_confidence score |
| **TFT Sidecar** | Temporal Fusion Transformer | B2 | Multi-horizon price forecasts + feature importance; outputs added to feature pipeline |
| **DRL Shadow** | SAC (Stable-Baselines3) | C | Shadow sizer that trains alongside; observes but does NOT control real trades |
| **DRL Active** | SAC | D | Manual promotion of DRL to active sizing after sustained shadow outperformance |

## Core Rules

- **FinBERT handles bulk sentiment** — Tier 1, pipeline tool, not an agent.
- **LLM audits sentiment context** — Tier 2, Signal Agent reads top raw headlines alongside FinBERT scores.
- **Feature computation is deterministic Python** — no LLM tokens spent on features.
- **22 strategies are deterministic Python** — "pre-digested logic" for the LLM, not raw numbers.
- **Signal Agent** receives strategy triggers + confluence + features and reasons about direction/score.
- **Risk Agent** is separate from Signal Agent — clean separation of "what to trade" vs "how much".
- **Hard-cap layer** runs AFTER execution layer, before final broker submission. Not inside any agent.
- **DRL (when active) still sits UNDER hard caps** — it can never bypass risk enforcement.
- Trading cadence baseline: **every 5 minutes**.

---

## Feature Pipeline (deterministic, no LLM)

### Purpose

- Compute 42 technical indicators and sentiment aggregates from `candles_5m`
- Score news sentiment using FinBERT (local model, no API cost)
- Aggregate sentiment into time windows with relevancy weighting
- Compute daily levels (day_high, day_low, prev_day_high, prev_day_low)
- Write versioned feature rows to `features` table

### Inputs

- `candles_5m` (5m OHLCV bars from Alpaca)
- `news_raw` + `news_symbol_map` (Alpaca news, backfilled)
- `news_sentiment_cache` (FinBERT per-article scores)

### Outputs

- `features` rows:
  - `symbol`, `ts`, `feature_set_version = 'v1'`
  - `feature_json` containing all 42 v1 keys (see `docs/FEATURE_SPEC_V1.md`)

### Two-tiered sentiment

- **Tier 1 (FinBERT, bulk):** Scores every article, produces numeric aggregates in `feature_json` (sent_mean_30m, sent_std_2h, etc.). Fast, consistent, cheap.
- **Tier 2 (LLM, contextual audit):** Signal Agent receives top 3-5 most recent/relevant raw headlines as text. Catches nuance FinBERT misses (e.g. "beat earnings but lowered guidance").

### Anti-leakage

- At bar `ts`, only use source data with timestamps `<= ts`
- No forward-fill from future bars
- Sentiment windows are right-closed at `ts`

### Cadence

- Historical backfill: full range, one-time
- Incremental: runs shortly after each 5m bar close

---

## 22 Strategy Engine (deterministic, no LLM)

### Purpose

- Evaluate 22 deterministic strategies per bar per symbol
- Each strategy checks entry conditions against `feature_json`
- Output: per-bar trigger signals (1 = long, -1 = short, 0 = no trigger) per strategy
- Confluence scoring: `long_count`, `short_count`, `confluence_score = long_count - short_count`

### Strategy list (7 categories)

**Category 1: EMA Cross**
1. `ema_cross_long` — 9/21 bullish cross + RSI < 65; long
2. `ema_cross_short` — 9/21 bearish cross + RSI > 35; short
3. `ema_cross_vol_surge` — 9/21 cross (either) + vol_z_20 > 1.5; high-conviction
4. `ema_ribbon_expansion` — ema_spread_9_21 > 0 AND ema_spread_21_50 > 0 + trade_count_z > 1

**Category 2: Mean Reversion**
5. `rsi_vol_exhaustion` — RSI < 25 + vol_z_20 > 2.5 + sent_mean_30m > -0.2; long
6. `rsi_overbought_fade` — RSI > 70 + regime_trend != "up"; short
7. `vwap_reversion_long` — vwap_dev_bps < -50; long snap-back
8. `institutional_vwap_fade` — vwap_dev_bps > 100 + trade_count_z > 3 + RSI > 80; short

**Category 3: Momentum / Breakout**
9. `macd_regime_alignment` — regime_trend = "up" + macd crosses macd_signal + price > ema_50
10. `multi_ret_momentum` — ret_3 > 0 AND ret_6 > 0 AND ret_12 > 0; all aligned, long
11. `volume_breakout` — vol_z_20 > 2.0 + ret_1 > 0.3%; volume explosion with direction
12. `relative_strength_long` — rel_strength_vs_spy_12 > 0.005 + regime_trend = "up"
13. `volatility_squeeze` — realized_vol_12 at historical low + ema_spread_9_21 flips direction

**Category 4: News / Sentiment**
14. `headline_front_runner` — headline_impact_max_1d > 0.8 + sent_mean_30m > 0.5 + ret_1 > 0
15. `sell_the_news` — headline_impact_max_1d > 0.9 + RSI > 75 + sent_mean_30m declining; short
16. `sentiment_gap_fill` — ret_12 negative but sent_mean_30m > 0.5; price-sentiment divergence, long
17. `news_volume_breakout` — news_count_30m spike + ema_spread_9_21 > 0 + vol_z_20 > 2
18. `quiet_accumulation` — sent_mean_2h rising while ret_12 flat + ema_9 > ema_21

**Category 5: Session-Based**
19. `opening_range_breakout` — is_opening_window + ret_1 > 0.2% + vol_z_20 > 1.0; extra conviction if breaks prev_day_high
20. `last_hour_fade` — is_closing_window + RSI > 70 + ret_12 > 2%; short mean-reversion

**Category 6: Regime-Adaptive**
21. `regime_switch_flip` — regime_trend changes from "down" to "up" + sent_mean_2h > 0
22. `range_fade_flat_regime` — regime_trend = "flat" + RSI extremes; fade between prev_day_high/prev_day_low as S/R

### Daily level filters (used across strategies, NOT standalone)

`prev_day_high`, `prev_day_low`, `day_high`, `day_low` enhance existing strategies:
- `ema_cross_long` — only enter if price above prev_day_low
- `opening_range_breakout` — extra conviction if price breaks prev_day_high
- `rsi_vol_exhaustion` — stronger if price near prev_day_low
- `volume_breakout` — confirm if price crosses prev_day_high or prev_day_low
- `range_fade_flat_regime` — use prev_day_high/low as natural S/R boundaries

### Theoretical trigger tracking

Every bar where a strategy's entry conditions are met, the system logs the hypothetical outcome regardless of whether a trade was opened:
- `theoretical_triggers`: count of bars where conditions were satisfied
- `theoretical_win_rate`: of those triggers, how many would have hit TP vs SL vs timeout
- `theoretical_avg_pnl`: average hypothetical PnL per trigger

Purpose: detect alpha decay, measure LLM selection quality (theoretical win rate vs actual).

### Confluence scoring

Per bar per symbol:
- `long_count`: how many of the 22 strategies say LONG
- `short_count`: how many say SHORT
- `confluence_score`: `long_count - short_count`

Confluence is the primary interconnection mechanism. Individual strategies stay simple (2-3 conditions); complex combinations are handled by confluence + meta-classifier (Phase B1).

---

## Signal Agent (LLM-powered)

### Purpose

- Read pre-computed features (42 keys) + 22 strategy trigger signals + confluence score
- Read top 3-5 most recent/relevant raw headlines (Tier 2 sentiment audit)
- Reason about trade candidates: synthesize strategy triggers, news context, regime
- Produce directional signals with confidence and strategy attribution

### Inputs

- Latest `features` rows (feature_json) for all active symbols
- 22 strategy trigger signals + confluence scores per symbol
- Per-strategy theoretical win rates and per-symbol performance stats
- Top 3-5 raw headlines from `news_raw` (queried at prompt time)
- Recent `signals` history (for context continuity)

### Outputs

- `signals` rows:
  - `symbol`, `ts`, `direction`, `score`, `confidence`, `horizon`
  - `strategy_id`, `base_signal`, `news_delta`, `regime_tag`
  - `meta` (explanation, sentiment summary, model diagnostics)

### Design principle

The LLM **synthesizes**, it does not calculate. Strategies provide "pre-digested logic" (deterministic). The LLM decides whether the logic makes sense in context.

If FinBERT score and LLM's headline reading disagree, the LLM must note the conflict in `meta` and default to reduced confidence or flat.

### Model

- Primary: `openai/gpt-4.1` (~$2/$8 per 1M tokens)
- Budget alternative: `deepseek/deepseek-v3.2` (~$0.26/$0.38 per 1M tokens, reasoning mode)
- Fallback: `google/gemini-2.5-pro`, `anthropic/claude-sonnet-4.5`

### Cadence

- Runs every 5m bar close, after feature pipeline + strategy engine complete

### Token expectation

- ~2,000-8,000 input / 200-1,200 output per 5m cycle

---

## Risk Agent (LLM-powered, DRL shadow alongside)

### Purpose

- Analyze signals table plus account/risk context
- Select best actionable candidate(s)
- Determine position sizing (0u-3u) with explicit reasoning
- Produce executable proposals

### Inputs

- Latest `signals`
- `risk_config`
- Account equity, open positions, daily PnL
- Rolling model/strategy performance stats per symbol/regime
- Execution quality stats (spread/slippage/reject-rate)
- Price-action and regime context for current bar

### Outputs

- `proposed_orders` rows:
  - side, size, order_type, SL/TP
  - `chosen_strategy`, `expected_edge_bps`, `regime_tag`
  - `size_u`, `size_reason_code`, `confidence_bucket`
  - `status = pending`
  - `risk_checks`, `reject_reason`
  - `reasoning_json` for audit

### DRL shadow mode (Phase C)

When DRL shadow is active:
- DRL receives same inputs as Risk Agent (signal direction/confidence, account state, regime, vol)
- DRL produces shadow size recommendation (0u-3u), logged but NOT executed
- After each trade resolves, actual outcome is fed back as reward signal to DRL
- Every cycle logs: `{ drl_suggested_size, llm_actual_size, outcome }`
- DRL is promoted to active sizing ONLY after sustained shadow outperformance (500+ trades), reviewed manually

### Model

- Primary: `openai/gpt-4.1`
- Fallback: `anthropic/claude-sonnet-4.5`, `google/gemini-2.5-pro`

### Token expectation

- ~1,000-6,000 input / 200-1,200 output per 5m cycle

---

## Execution Layer (mostly deterministic)

### Purpose

- Send approved/proposed orders to broker
- Run send-time safety checks
- Track fills/rejections and audit events

### Inputs

- `proposed_orders` (approved or auto-approved)
- Alpaca Trading API (paper first, live later)
- Session and execution risk state

### Send-time checks (deterministic)

- Kill switch (`live_trading_enabled`)
- Session filter (skip open/close buffers)
- Spread/slippage thresholds
- Daily halt state
- Idempotency (no duplicate send)
- Account/tradability checks

### Optional LLM safety review

- Light review for anomaly detection before send
- Model: `openai/gpt-4.1` or `google/gemini-2.5-flash`
- Can be disabled for full-auto mode

### Outputs

- `orders` updates (sent, filled, cancelled, rejected)
- `positions` updates
- Append-only `execution_events`

---

## Hard-Cap Layer (deterministic, NOT an agent)

Runs AFTER execution layer, before final broker submission:

- **Max 3u risk per trade**
- **Max 7u daily loss** -> halt_for_day (no new entries)
- **Max consecutive losses** (default: 3) -> cooldown
- Pure code — no LLM, no override possible
- Even if DRL is promoted to active, hard caps remain the final gate

---

## Post-Loss Reviewer (async, LLM-powered)

### Purpose

- Trigger after daily loss halt or end-of-day
- Explain what happened and propose improvements

### Inputs

- `signals`, `proposed_orders`, `orders`, `execution_events`, news snapshots
- Per-strategy theoretical vs actual win rates (detect alpha decay)

### Outputs

- Structured review artifact:
  - root causes
  - rule/model/data changes
  - next-session guard adjustments
  - strategy-specific alpha decay flags

### Model

- Primary: `anthropic/claude-sonnet-4.5`
- Fallback: `openai/gpt-4.1`

### Token expectation

- ~5,000-20,000 input / 800-3,000 output (batch report)

---

## Strategy Researcher (optional, async, LLM-powered)

### Purpose

- Analyze rolling performance by symbol, regime, and strategy variant
- Recommend weight/rule updates (never auto-executes trades)

### Inputs

- Historical `signals`, `proposed_orders`, `orders`, `execution_events`
- Feature snapshots and regime labels
- Theoretical trigger stats and alpha decay metrics

### Outputs

- Strategy ranking updates
- De-prioritize underperforming setups
- Candidate threshold changes for human approval

### Guardrail

- Can suggest parameter changes, but cannot bypass hard constraints.

### Token expectation

- Medium batch workloads (2,000-10,000 input, variable output)

---

## Future ML Components

### Phase B1: Meta-Classifier (XGBoost/LightGBM)

- **When:** After Phase 2 backtester produces sufficient trigger/outcome data
- **Input:** 22 strategy signals (1/0/-1) + regime + vol + session flags + per-strategy theoretical win rates
- **Output:** Single `meta_confidence` score (0.0-1.0) passed to Signal Agent LLM
- **Purpose:** Statistically learns which strategy combinations + regime = profitable; removes noise for the LLM
- Sits between strategy engine and Signal Agent in the pipeline

### Phase B2: TFT Sidecar (Temporal Fusion Transformer)

- **When:** After backtester results reviewed and architecture stable
- **What:** Neural network trained on historical `feature_json` sequences per symbol (with symbol as static covariate)
- **Output:** Multi-horizon quantile forecasts (e.g. predicted return at +3/+6/+12 bars) + attention-based feature importance
- **Integration:** TFT outputs become additional columns in feature pipeline; Signal Agent reads them alongside existing features
- **TFT is advisory** — if TFT and confluence disagree, Signal Agent defaults to reduced confidence
- **Evaluation:** Walk-forward validation, costs in metric, calibration check, paper shadow before trust

### Phase C: DRL Shadow (SAC via Stable-Baselines3)

- **When:** From day 1 of paper/live trading
- **Algorithm:** SAC (Soft Actor-Critic) — off-policy, handles non-stationary markets
- **State:** Signal direction + confidence, daily PnL, consecutive losses, regime, volatility, time of day
- **Action:** Position size (0-3u continuous, rounded)
- **Reward:** Realized PnL minus commission/slippage minus over-trading penalty
- **Key rule:** Observes but NEVER controls real trades until manually promoted
- Logs `{ drl_suggested_size, llm_actual_size, outcome }` every cycle

### Phase D: DRL Active Promotion

- **When:** Manual decision after 500+ shadow trades with sustained outperformance
- **Change:** DRL replaces or augments LLM Risk Agent for sizing
- **Safety:** LLM optionally kept as "explainer" for audit; Hard-Cap Layer unchanged
