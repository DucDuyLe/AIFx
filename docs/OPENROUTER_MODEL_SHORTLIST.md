---
title: OpenRouter Model Shortlist
date: 2026-03-28
---

# OpenRouter Model Shortlist (Layer-by-Layer)

This file maps each SPAI500 system layer to candidate OpenRouter models with specs, cost, and reasoning.

## Model selection principles

- **Feature Pipeline and FinBERT sentiment** do NOT use OpenRouter — they are deterministic code + local model.
- LLM tokens are spent only on reasoning tasks: Signal Agent, Risk Agent, Execution safety review, Post-Loss Reviewer, Strategy Researcher.
- Keep Signal Agent cost manageable since it runs every 5m.
- Use stronger models for Risk Agent reasoning and Post-Loss reports.
- Always keep hard-cap and execution gate logic independent of model output.
- Prefer structured JSON output for machine-readability and audit.

References:
- Models: `https://openrouter.ai/docs/guides/overview/models`
- API: `https://openrouter.ai/docs/api-reference/overview`
- Pricing: `https://openrouter.ai/pricing`

---

## Global top 5 models (recommended baseline pool)

### 1) `google/gemini-2.5-flash`
- **Best use:** fallback for Signal Agent, Execution safety review
- **Context:** ~1M tokens
- **Cost:** ~$0.30 / 1M input, ~$2.50 / 1M output
- **Why:** low-cost, fast, good enough reasoning for frequent 5m loops

### 2) `openai/gpt-4.1`
- **Best use:** Signal Agent and Risk Agent primary
- **Context:** ~1M tokens
- **Cost:** ~$2.00 / 1M input, ~$8.00 / 1M output
- **Why:** consistent structured outputs, strong tool-oriented behavior, good financial reasoning

### 3) `anthropic/claude-sonnet-4.5`
- **Best use:** Post-Loss Reviewer primary, Risk Agent premium fallback
- **Context:** ~1M tokens
- **Cost:** starts around ~$3.00 / 1M input and ~$15.00 / 1M output
- **Why:** strong long-form reasoning and clearer, actionable explanations

### 4) `deepseek/deepseek-chat-v3.1` (or latest stable DeepSeek chat)
- **Best use:** budget fallback for Signal Agent
- **Context:** provider-dependent, up to long-context variants
- **Cost:** often in low-cost tier (~$0.15 / 1M input, ~$0.75 / 1M output)
- **Why:** very strong cost-efficiency for classification/summarization workloads

### 5) `google/gemini-2.5-pro`
- **Best use:** Risk Agent / Strategy Researcher escalation model
- **Context:** long-context class
- **Cost:** commonly around ~$1.25 / 1M input, ~$10.00 / 1M output
- **Why:** stronger reasoning depth than flash-tier with manageable cost

---

## Layer-by-layer model matrix (5 best each)

### Signal Agent — reads features, produces signals

1. `openai/gpt-4.1`
2. `google/gemini-2.5-flash`
3. `deepseek/deepseek-chat-v3.1`
4. `google/gemini-2.5-pro`
5. `anthropic/claude-sonnet-4.5`

**Reasoning:** Signal Agent runs every 5m, so cost matters but accuracy is critical for trade quality. GPT-4.1 balances both well; flash/deepseek are budget fallbacks.

### Risk Agent — reads signals, sizes trades, produces proposed_orders

1. `openai/gpt-4.1`
2. `anthropic/claude-sonnet-4.5`
3. `google/gemini-2.5-pro`
4. `deepseek/deepseek-chat-v3.1`
5. `google/gemini-2.5-flash`

**Reasoning:** Risk Agent must compare alternatives, justify size choices (0u–3u), and produce auditable reasoning. Prioritize reliability and coherent structured output.

### Execution Layer — optional LLM safety review before send

1. `openai/gpt-4.1`
2. `google/gemini-2.5-flash`
3. `anthropic/claude-sonnet-4.5`
4. `google/gemini-2.5-pro`
5. `deepseek/deepseek-chat-v3.1`

**Reasoning:** Light review for anomaly detection. Can use cheaper model since deterministic checks handle most safety. Can be disabled for full-auto mode.

### Post-Loss Reviewer — async, analyzes losses

1. `anthropic/claude-sonnet-4.5`
2. `openai/gpt-4.1`
3. `google/gemini-2.5-pro`
4. `deepseek/deepseek-chat-v3.1`
5. `google/gemini-2.5-flash`

**Reasoning:** Long-context analysis, coherent reporting, and root-cause quality matter most; latency is less important.

### Strategy Researcher — async, optional

1. `openai/gpt-4.1`
2. `anthropic/claude-sonnet-4.5`
3. `google/gemini-2.5-pro`
4. `deepseek/deepseek-chat-v3.1`
5. `google/gemini-2.5-flash`

**Reasoning:** Strategy-level optimization benefits from strong reasoning, but runs async so latency is flexible.

---

## Per-model spec and cost quick cards

### `google/gemini-2.5-flash`
- **Strengths:** speed, low cost, high throughput, long context
- **Context:** ~1M
- **Indicative cost:** ~$0.30 in / ~$2.50 out per 1M tokens
- **Best fit:** Signal Agent fallback, Execution safety review

### `openai/gpt-4.1`
- **Strengths:** reliable structured reasoning, stable tool behavior
- **Context:** ~1M
- **Indicative cost:** ~$2.00 in / ~$8.00 out per 1M tokens
- **Best fit:** Signal Agent primary, Risk Agent primary

### `anthropic/claude-sonnet-4.5`
- **Strengths:** high-quality synthesis, detailed rationale, strong long-form output
- **Context:** ~1M
- **Indicative cost:** ~$3.00+ in / ~$15.00+ out per 1M tokens
- **Best fit:** Post-Loss Reviewer primary, Risk Agent premium fallback

### `deepseek/deepseek-chat-v3.1`
- **Strengths:** cost-efficiency, strong budget reasoning/summarization
- **Context:** provider-dependent long context variants
- **Indicative cost:** low-cost tier (~$0.15 in / ~$0.75 out per 1M tokens)
- **Best fit:** Signal Agent budget fallback

### `google/gemini-2.5-pro`
- **Strengths:** stronger depth than flash for difficult reasoning
- **Context:** long-context class
- **Indicative cost:** mid/high tier (~$1.25 in / ~$10.00 out per 1M tokens)
- **Best fit:** Risk Agent / Strategy Researcher escalation model

---

## Cost control checklist

- enforce per-agent token budgets
- cap headlines per cycle and batch by ticker/time window
- FinBERT handles all sentiment scoring locally — zero OpenRouter cost for sentiment
- only enrich high-impact or low-confidence items via LLM
- store `usage` and `cost` from OpenRouter responses for audit
- pin model IDs in config and review monthly
- track fallback rate (high fallback rate usually means prompt/model mismatch)

---

## Notes on pricing accuracy

- OpenRouter pricing and model availability can change.
- Treat costs above as **indicative** and validate on model pages before locking monthly budgets.
- For budget planning, use a 10-20% safety buffer over estimated monthly usage.
