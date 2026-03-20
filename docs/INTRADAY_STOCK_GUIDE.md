# Intraday Stock Guide (Phase 1 -> Phase 2)

This guide applies the 3-agent architecture from `docs/FULL_STACK_SPEC.md` to a stock-first intraday rollout.

## 1) Target Universe and Session Rules

- Phase 1 universe (start): `SPY, QQQ, AAPL, MSFT, NVDA, AMZN`
- Expansion rule: add at most 2-4 symbols only after stability gates pass.
- Trading session (US regular hours): 09:30-16:00 America/New_York.
- Avoid low-liquidity windows in Phase 1:
  - Skip first 5 minutes after open.
  - Skip last 10 minutes before close.

## 2) Agent Cadence

- Agent 1 (Data & Signal): every 5 minutes.
- Agent 2 (Risk & Sizing): every 5 minutes, immediately after Agent 1.
- Agent 3 (Execution): every 30-60 seconds.

## 3) Broker Path

- Phase 1 and Phase 2 default: **IBKR paper first**.
- Keep OANDA for optional FX expansion later.
- Why this path:
  - Matches "real stocks intraday" requirement.
  - Keeps execution and risk controls in a stock-native broker flow.
  - Reduces connector switching when moving from paper to live.

## 4) Risk Controls to Enforce in Agent 2 and Agent 3

### Agent 2 (proposal-time controls)

- `max_risk_pct_per_trade`
- `max_daily_loss_pct`
- `max_open_positions`
- `max_symbol_notional_pct`
- `max_gross_exposure_pct`
- `max_leverage`
- `max_margin_utilization_pct`
- `min_maintenance_margin_buffer_pct`
- `auto_downsize_drawdown_trigger_pct`
- `auto_downsize_factor`

### Agent 3 (send-time controls)

- `max_slippage_bps`
- `max_spread_bps`
- hard kill switch (`live_trading_enabled`)
- session checks (market hours + no-trade windows)

## 5) Promotion Gates (Paper -> Live -> Margin)

Only promote when all gates hold over a meaningful sample window:

- Positive net expectancy after fees and slippage.
- Drawdown within configured limits.
- No repeated control breaches (kill switch, session, sizing, exposure).
- Execution quality passes:
  - reject rate below threshold,
  - slippage below threshold,
  - fee-to-gross-profit ratio acceptable.

Suggested progression:

1. Phase 1A: paper, no/near-zero leverage.
2. Phase 1B: paper with full fee/slippage accounting.
3. Phase 1C: live micro-size, minimal leverage.
4. Phase 2: gradual margin ramp after gate pass.

## 6) Data and Execution Quality Metrics

Track and store:

- proposed order count, sent count, rejected count
- average and percentile slippage (bps)
- average spread at decision time (bps)
- fees and fee ratio
- fill ratio and order latency

Use these metrics as first-class blockers for margin enablement.
