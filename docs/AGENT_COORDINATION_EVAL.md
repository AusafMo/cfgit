# cfgit-agent — real-usage evaluation

This is the result of driving the coordination plugin end-to-end **as a real human+agent would**,
against a live Mongo (localhost:27018) seeded with **real AI-Studio control-plane data** —
`modelgarden_models` (242 docs), `agent_configs` (78), `foundry_agents` (9), copied from the local
`ai-tools-dev` mirror. The harness lives at `eval/run_eval.py` (gitignored); each scenario maps a
spec claim to observed runtime behavior and reports PASS / GAP.

**Verdict: 10 PASS · 2 GAP · 0 ERR (initial run).** The core safety contract holds against real
data. Two real gaps surfaced that unit tests (which always pass inputs in the exact expected shape)
missed — **both are now fixed** (see below); the eval re-runs at **12 PASS · 0 GAP · 0 ERR**.

## What holds (PASS)

| Spec claim | Scenario | Observed |
|---|---|---|
| Agent mutation lands through cfgit (no hidden runtime write) | happy path | commit / review-routed, intent closed |
| Overlapping claims blocked, conflict structured + queryable | lease conflict | `lease_conflict`, listed by `conflicts()` |
| Patch outside declared intent paths rejected | intent scope | `intent_scope` |
| Patch on unclaimed path rejected | claim guard | `claim_required` |
| Review-required patch → cfgit branch+PR, **runtime unchanged** | review routing | `review_requested`, PR created, live doc untouched |
| Stale write caught **when base given in the right shape** | base_moved (flat) | `base_moved` |
| Role can't claim outside its scope | role scope | `policy_blocked` |
| Out-of-band live drift blocks validation | live drift | `live_drift` |
| `allow_live_drift=True` lets the agent proceed | drift adopt | `state=ok` |
| Idempotency key reuse with a **different** payload is blocked, even cross-session | idempotency | `idempotency_conflict` (NOT a silent replay) |

Note the last row: a static code read suspected idempotency keys weren't scoped to env+session and
could silently replay across sessions. **Real usage disproves the dangerous case** — reuse with a
changed payload is blocked with `idempotency_conflict` regardless of session. The scoping is looser
than the spec's wording but the corruption hole does not exist. (Down-scoped from a bug to a
doc-accuracy item.)

## Gaps (fix targets)

### GAP 1 — `base_moved` fails open on shape mismatch (the important one)

The anti-stale-write guarantee — an agent must not overwrite a record that moved since it read it —
is **opt-in AND shape-sensitive AND fails silently open.**

`coordinator.validate_patch` resolves the expected base as:
```python
expected = base or (intent.get("expected_base") or {}).get(record_resource) or {}
expected_seq = expected.get("head_seq")
if expected_seq is not None and int(expected_seq) != int(head.get("seq")): raise base_moved
```
- The `base=` param wants a **flat** `{head_seq, head_oid}`.
- `intent.expected_base` wants a **nested** `{record: {head_seq, head_oid}}`.
- Give either the *other* (equally natural) shape → `expected` resolves empty → `expected_seq is None`
  → the check is **skipped with no error**. The agent gets zero stale-write protection and no signal.

Observed live (same stale base, two shapes): **flat `base=` → `base_moved` (PASS); nested `base=` →
validates cleanly (GAP).** A real agent author will get the shape wrong and silently lose the
guarantee — this is exactly the class of thing the unit tests miss because they always pass the
canonical shape.

**FIXED:** `validate_patch` now resolves the base via `_resolve_expected_base`, which accepts BOTH
the flat `{head_seq, head_oid}` and the nested `{record: {...}}` shape, from either `base=` or the
intent — so the guarantee never silently fails open on shape alone. Regression:
`test_validate_patch_base_moved_accepts_both_shapes` (4 shapes). *(The "fail closed when no base at
all" hardening is noted as a follow-up — today an agent that supplies no base still gets no
check; that's a separate, documented item.)*

### GAP 2 — no public lease renewal

Spec §10 lists `cfg_agent_renew(session_id, lease_id, ttl_seconds?)`. The adapter implemented
`renew_lease`, but the coordinator and MCP layer exposed no renewal — a long-running agent could
only re-claim, not extend. **FIXED:** added `coordinator.renew()` + `AgentActions.renew` +
`cfg_agent_renew` MCP tool, with ownership check. Regression:
`test_coordinator_renew_extends_owned_lease`.

## Deferred (documented, not fixed this pass)

Confirmed thin vs the spec, but out of scope for this cut — the spec doc will mark them explicitly:
- **Session expiration / takeover** — sessions never go stale; heartbeat is cosmetic (spec §7.1/§19).
- **Human-review approval tools** (`request_review`/`approve`/`reject`, spec §10) — approval happens
  only via cfgit's PR merge; no agent-layer review objects.
- **Conflict `resolution` field** — stored, no semantics (spec §7.5).
- **`require_intent` / `allow_path_expansion` policy knobs** — intent always required, `planned_paths`
  strictly enforced; no opt-out (spec §13 implies configurable). *(Small knobs — building these.)*
- **Postgres adapter** — contract-tested but under-exercised vs Mongo.

## Reproduce

```bash
# data already copied into 27018:cfgit_agent_eval (see eval/.cfg.toml)
EVAL_MONGODB_URI='mongodb://localhost:27018/?replicaSet=rs0&directConnection=true' \
  python eval/run_eval.py
```
The harness clears agent state each run and re-adopts the touched records, so it's repeatable.
