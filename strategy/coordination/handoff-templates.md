# NEXUS Handoff Templates

Use these when switching agents, phases, or days so no one starts cold. Paste into chat or append to `docs/` when the handoff is a project artifact.

**Rules**

- One handoff per logical unit of work (task, phase exit, or incident).
- Attach paths, not vague memory (“see spec” → `docs/FULL_STACK_SPEC.md` §X).
- List **decisions already made**; do not re-litigate silently.
- **Evidence:** links to PRs, commits, test output, screenshots, or logs.

---

## 1. Task handoff (developer → QA)

**From:** [Agent / name]  
**To:** Evidence Collector (or API Tester)  
**Task ID:** [e.g. SPAI-42]  
**Date:**

### Summary

[1–3 sentences: what shipped]

### Acceptance criteria (verbatim)

- [ ] …
- [ ] …

### What changed

- Files / modules: [paths]
- Migrations / config: [paths]
- Env vars: [new keys, point to `.env.example` only]

### How to verify

- Commands: [e.g. `pytest …`, `npm test`, `curl …`]
- URLs / toggles: [if UI or feature flags]
- Test data: [fixtures / seed IDs]

### Known limitations

- …

### Evidence (required for PASS)

- [ ] Test output pasted or linked  
- [ ] Screenshots if UI (optional for pure API)  

---

## 2. QA feedback (QA → developer, retry)

**Task ID:**  
**Attempt:** [N] of 3  
**Verdict:** FAIL

### Failed checks

| Check | Expected | Actual | Severity |
|-------|----------|--------|----------|
| … | … | … | Blocker / Major / Minor |

### Repro steps

1. …
2. …

### Suggested fix (optional)

…

---

## 3. Phase boundary handoff (e.g. Phase N → N+1)

**Project:** [NAME]  
**Phase completed:** Phase [N] — [Name]  
**Phase entering:** Phase [N+1] — [Name]  
**Date:**

### Phase goal (original)

…

### Quality gate checklist

- [ ] Acceptance criteria for phase met  
- [ ] Tests / evidence recorded  
- [ ] Risks and open issues documented  
- [ ] Spec/design updated if behavior changed  

### Deliverables produced (paths)

- …

### Decisions locked

| Decision | Rationale | Owner |
|----------|-----------|-------|
| … | … | … |

### Carryover / risks

- …

### Next phase first tasks (ordered)

1. …
2. …

---

## 4. Pipeline status report (orchestrator)

**Project:**  
**Mode:** NEXUS-[Full/Sprint/Micro]  
**Reporting period / sprint:**  
**Date:**

| Metric | Value |
|--------|-------|
| Tasks completed | X / Y |
| First-pass QA rate | … |
| Avg retries per task | … |
| Blocked tasks | [list + reason] |
| Sprint / phase progress | …% |

### Blockers

- …

### Upcoming (next 3 priorities)

1. …
2. …
3. …

---

## 5. Incident / production handoff

**Incident ID:**  
**Severity:**  
**On-call / owner:**

### Current state

- User impact: …
- Mitigation in place: …

### Timeline (facts)

- …

### Evidence

- Logs / graphs: …
- Suspected component: …

### Next steps

- [ ] …
- [ ] …

### Do not do

- … (e.g. no schema change during incident)

---

## 6. SPA I500–specific checklist (trading stack)

Use in addition to section 1 or 3 when touching execution or risk.

- [ ] **Agent 2 / 3:** No LLM bypass of hard caps (`trading-rules`, `FULL_STACK_SPEC`).
- [ ] **Paper vs live:** Mode documented; kill switch / toggles named.
- [ ] **Audit:** `execution_events` or equivalent logging if orders touched.
- [ ] **Secrets:** No keys in repo; `.env.example` updated if new vars.

---

## File locations (this repo)

| Artifact | Source of truth |
|----------|-------------------|
| Core spec | `docs/FULL_STACK_SPEC.md` |
| Agent I/O | `docs/AGENT_SPEC_AND_IO.md` |
| Services / roadmap | `docs/SERVICES_AND_ROADMAP.md` |
| Schema | `db/schema.sql` |
| Activation prompts | `strategy/coordination/agent-activation-prompts.md` |
