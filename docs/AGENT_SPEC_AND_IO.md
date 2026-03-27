---
title: Agent Spec and I/O
date: 2026-03-28
---

# Agent Spec and I/O (v3 — Revised Architecture)

This file defines the system layers: deterministic pipeline, LLM agents, execution, and hard-cap enforcement.

## Architecture Overview

The system has 3 distinct layers plus 2 async agents:

| Layer | Type | LLM? | Purpose |
|-------|------|------|---------|
| **Feature Pipeline** | Deterministic code | No | Compute technical features + FinBERT sentiment from candles_5m + news_raw |
| **Signal Agent** | LLM agent | Yes | Read features, reason about trade candidates, produce signals |
| **Risk Agent** | LLM agent | Yes | Read signals + account state, size positions (0u–3u), produce proposed_orders |
| **Execution Layer** | Mostly deterministic | Minimal | Send-time checks + broker API. Optional light LLM safety review |
| **Hard-Cap Layer** | Deterministic gate | No | Final enforcement: 3u/trade, 7u/day, consecutive-loss cooldown |
| **Post-Loss Reviewer** | Async LLM agent | Yes | Analyze losses, propose improvements (triggers after daily halt or EOD) |
| **Strategy Researcher** | Async LLM agent (optional) | Yes | Rolling performance analysis, strategy ranking updates |

## Core Rules

- **FinBERT handles sentiment** — it is a pipeline tool, not an agent.
- **Feature computation is deterministic Python** — no LLM tokens spent on features.
- **Signal Agent** receives pre-computed features and reasons about direction/score/strategy.
- **Risk Agent** is separate from Signal Agent — clean separation of "what to trade" vs "how much".
- **Hard-cap layer** runs AFTER execution layer, before final broker submission. Not inside any agent.
- Trading cadence baseline: **every 5 minutes**.

---

## Feature Pipeline (deterministic, no LLM)

### Purpose

- Compute technical indicators from `candles_5m`
- Score news sentiment using FinBERT (local model, no API cost)
- Aggregate sentiment into time windows with relevancy weighting
- Write versioned feature rows to `features` table

### Inputs

- `candles_5m` (5m OHLCV bars from Alpaca)
- `news_raw` + `news_symbol_map` (Alpaca news, backfilled)
- `news_sentiment_cache` (FinBERT per-article scores)

### Outputs

- `features` rows:
  - `symbol`, `ts`, `feature_set_version = 'v1'`
  - `feature_json` containing all v1 keys (see `docs/FEATURE_SPEC_V1.md`)

### Sentiment scoring

- Model: `ProsusAI/finbert` (local inference, CPU)
- Input: `headline + summary` per article
- Output: sentiment_score in `[-1, 1]`, cached in `news_sentiment_cache`
- Relevancy weighting at aggregation time:
  - exclusivity: `1 / count(symbols tagged on article)`
  - headline boost: `1.5x` if symbol appears in headline
  - weighted mean: `sum(score * relevancy) / sum(relevancy)`

### Anti-leakage

- At bar `ts`, only use source data with timestamps `<= ts`
- No forward-fill from future bars
- Sentiment windows are right-closed at `ts`

### Cadence

- Historical backfill: full range, one-time
- Incremental: runs shortly after each 5m bar close

---

## Signal Agent (LLM-powered)

### Purpose

- Read pre-computed features (technical + sentiment) for active symbols
- Reason about trade candidates across multiple strategies
- Produce directional signals with confidence and strategy attribution

### Inputs

- Latest `features` rows (feature_json) for all active symbols
- Recent `signals` history (for context continuity)
- Optional regime flags and macro context

### Outputs

- `signals` rows:
  - `symbol`, `ts`, `direction`, `score`, `confidence`, `horizon`
  - `strategy_id`, `base_signal`, `news_delta`, `regime_tag`
  - `meta` (explanation, sentiment summary, model diagnostics)

### Model

- Primary: `openai/gpt-4.1` (~$2/$8 per 1M tokens)
- Fallback: `google/gemini-2.5-pro`, `anthropic/claude-sonnet-4.5`
- Budget fallback: `google/gemini-2.5-flash`

### Cadence

- Runs every 5m bar close, after feature pipeline completes

### Token expectation

- ~1,000–6,000 input / 200–1,200 output per 5m cycle

---

## Risk Agent (LLM-powered)

### Purpose

- Analyze signals table plus account/risk context
- Select best actionable candidate(s)
- Determine position sizing (0u–3u) with explicit reasoning
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

### Decision behavior

- Evaluate candidate edge from multi-strategy signals + historical hit-rate context
- Compare alternatives and choose best trade
- Map confidence and expected edge to size bucket (0u, 1u, 2u, 3u)
- Produce explicit "why this trade / why not alternatives" reasoning
- "Why 2u instead of 1u" must be auditable via `size_reason_code` + `reasoning_json`

### Hard constraints

- Respects configured limits and emits `risk_checks`
- Final hard-cap enforcement is validated by the external cap layer

### Model

- Primary: `openai/gpt-4.1`
- Fallback: `anthropic/claude-sonnet-4.5`, `google/gemini-2.5-pro`

### Token expectation

- ~1,000–6,000 input / 200–1,200 output per 5m cycle

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
- Token expectation: ~500–2,500 input / 120–700 output per batch
- Can be disabled for full-auto mode

### Outputs

- `orders` updates (sent, filled, cancelled, rejected)
- `positions` updates
- Append-only `execution_events`

---

## Hard-Cap Layer (deterministic, NOT an agent)

Runs AFTER execution layer, before final broker submission:

- **Max 3u risk per trade**
- **Max 7u daily loss** → halt_for_day (no new entries)
- **Max consecutive losses** (default: 3) → cooldown
- Pure code — no LLM, no override possible

---

## Post-Loss Reviewer (async, LLM-powered)

### Purpose

- Trigger after daily loss halt or end-of-day
- Explain what happened and propose improvements

### Inputs

- `signals`, `proposed_orders`, `orders`, `execution_events`, news snapshots

### Outputs

- Structured review artifact:
  - root causes
  - rule/model/data changes
  - next-session guard adjustments

### Model

- Primary: `anthropic/claude-sonnet-4.5`
- Fallback: `openai/gpt-4.1`

### Token expectation

- ~5,000–20,000 input / 800–3,000 output (batch report)

---

## Strategy Researcher (optional, async, LLM-powered)

### Purpose

- Analyze rolling performance by symbol, regime, and strategy variant
- Recommend weight/rule updates (never auto-executes trades)

### Inputs

- Historical `signals`, `proposed_orders`, `orders`, `execution_events`
- Feature snapshots and regime labels

### Outputs

- Strategy ranking updates
- De-prioritize underperforming setups
- Candidate threshold changes for human approval

### Guardrail

- Can suggest parameter changes, but cannot bypass hard constraints.

### Token expectation

- Medium batch workloads (2,000–10,000 input, variable output)
