---
title: Stock Broker APIs with Paper/Sandbox (Research)
date: 2026-03-26
---

# Stock Broker APIs with Paper/Sandbox (Deep Research)

This doc focuses on brokers/platforms that support:

- **API trading**
- **paper/sandbox/sim trading**
- suitability for an agent workflow using **5-minute OHLCV** (not tick HFT)

## Summary (Best Picks for Your Use Case)

If you want the cleanest “API + paper trading” start for stocks:

- **Alpaca (paper)**: easiest paper environment; good for building Agent 3 execution + order state machine.
- **TradeStation (SIM)**: explicit SIM base URL; good if you want a broker-backed simulator environment.
- **IBKR (paper via TWS/IB Gateway)**: powerful, but operationally heavier (needs TWS/IBG running + daily restart realities).

If you want “sandbox tokens” but accept delayed quotes:

- **Tradier sandbox**: strong API surface; sandbox quotes are typically delayed (limits scalping realism).
- **tastytrade sandbox**: resets every 24h and sandbox quotes are **15-min delayed** (good for integration testing, not scalping realism).

## Broker/Platform Details

### Alpaca (Paper Trading)

- **Paper trading exists and is free to use for testing**.
- Base URL for paper:
  - `https://paper-api.alpaca.markets`
- Paper is a simulation (fills differ from live; does not model many real-world effects).

Source: `https://docs.alpaca.markets/docs/paper-trading`

Fit for your system:

- **Agent 3 execution loop**: very good
- **5m bar workflow**: good
- **Scalping realism**: medium (still a sim; slippage/queueing not fully modeled)

### Interactive Brokers (IBKR) Paper + TWS API / IB Gateway

Key realities:

- Your API client connects to a **running instance** of **TWS** or **IB Gateway**.
- Headless operation without GUI is not supported in the classic TWS API model.
- TWS/IBG were designed to be **restarted daily**; there are auto-restart features, but it’s still an ops consideration.

Source: `https://interactivebrokers.github.io/tws-api/initial_setup.html`

Fit for your system:

- **Agent 3 execution loop**: excellent
- **Ops complexity**: higher than pure REST brokers
- **Best used** once your risk+state machine is stable

### Tradier (Sandbox)

- Provides production token + sandbox token.
- Sandbox endpoint:
  - `https://sandbox.tradier.com/v1/`
- Important limitation (commonly reported/expected): sandbox market data is typically delayed, which reduces scalping realism.

Source: `https://docs.tradier.com/docs/getting-started`

Fit for your system:

- **Integration testing**: good
- **Scalping**: weaker if delayed quotes/fills matter to your edge

### TradeStation (SIM vs LIVE)

- Explicitly supports a **Simulator (SIM) API** described as identical to Live except:
  - fake accounts
  - simulated executions with instant “fills”
- SIM base URL:
  - `https://sim-api.tradestation.com/v3`

Source: `https://api.tradestation.com/docs/fundamentals/sim-vs-live/`

Fit for your system:

- **Great for end-to-end API integration**
- **Not perfect for fill realism** (instant fills are optimistic for scalping)

### tastytrade (Sandbox)

- Sandbox base URL:
  - `api.cert.tastyworks.com`
- Sandbox resets every 24 hours (positions/transactions cleared).
- Sandbox quotes are **15-min delayed**.

Source: `https://developer.tastytrade.com/sandbox/`

Fit for your system:

- **API integration testing**: good
- **Scalping realism**: poor due to delayed quotes + daily reset

## Recommendation: What to Start With (Stocks)

Given your constraints (5m bars, agentic system, focus on safety, paper first):

1. **Alpaca paper** for fastest “Agent 3 + order lifecycle + audit logs” development.
2. If you later want pro-grade broker depth: **IBKR paper**, after you’re happy with risk + state machine.

## What This Means for Your DB + Agents

- Agent 2 should produce `proposed_orders` with `risk_checks` and reasons.
- Agent 3 should:
  - enforce session/spread/slippage checks
  - be idempotent
  - write append-only execution/audit events

This matches the “semi-auto approval (Action Center)” concept you liked: proposed -> approve -> send -> fill/reject.

