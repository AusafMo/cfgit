# cfgit-agent: Agent Coordination Spec

> Status: design spec for an optional package in this repository. This is not a
> replacement for cfgit core. It describes an agent-first coordination layer on
> top of cfgit's existing non-custodial database version-control engine.

---

## 0. Implementation status (spec vs runtime)

This spec is the design; the runtime implements most of it. A real-usage evaluation against live
data (see **`docs/AGENT_COORDINATION_EVAL.md`**) validated the core contract and drove two fixes.
Where this document and the runtime differ, the list below is authoritative:

**Implemented and validated:** sessions, claims/leases (with overlap detection + structured
conflicts), declared intent with scope + planned-path enforcement, base-move detection
(`base_moved` — now shape-tolerant), live-drift detection with `allow_live_drift`, RFC-6902 patch
validate/apply through cfgit core, review-required routing to cfgit branch/PR (fails closed when
branches are disabled), role-scoped policy (`can_claim`/`deny_paths`/`review_paths`), idempotency,
and the MCP tool surface. In-memory + Mongo + Postgres state adapters.

**Recently added (post-eval):** `require_intent` and `allow_path_expansion` policy knobs
(§13/§16); `cfg_agent_renew` public lease renewal (§10).

**Deferred — described here but NOT in the current cut** (do not assume these work yet):
- **Session expiration & takeover** (§7.1, §19, §23) — sessions do not auto-expire; heartbeat is
  recorded but not enforced; there is no `takeover` action.
- **Human-review approval tools** (`cfg_agent_request_review` / `approve_review` / `reject_review`,
  §10) — review happens by routing the patch to a cfgit **PR**; approval is the cfgit PR merge, not
  an agent-layer review object.
- **Conflict `resolution` semantics** (§7.5) — the `resolution` field is stored but no action is
  taken on it (no auto wait/rebase/abandon).
- **Idempotency key scope** (§17) — the runtime keys on the idempotency_key (a reused key with a
  *different* payload is correctly blocked with `idempotency_conflict` regardless of session; it is
  NOT scoped strictly to `env + session_id + key` as written below).

---

## 1. Definition

`cfgit-agent` is an optional coordination and safety layer for multiple agents
working against the same live database records.

It gives agents first-class primitives for:

- sessions
- claims / leases
- declared intent
- safe patches
- idempotency
- conflict reporting
- event feeds
- human operator review

The core sentence:

> cfgit-agent lets multiple agents safely coordinate changes to live database
> records with claims, intents, patches, conflicts, audit, and rollback.

The package must stay optional:

```bash
pip install cfgit-agent
```

and must not make `cfgit` core an agent runtime.

---

## 2. Product Boundary

cfgit core remains:

> Non-custodial version control for live database records.

cfgit-agent becomes:

> Agent-first coordination for shared live database state.

This distinction matters. Agent frameworks already handle planning, tool
calling, durable execution, retries, memory, and model orchestration. cfgit-agent
does not compete with them. It coordinates the risky part they often leave to a
generic database tool call: several autonomous or semi-autonomous workers reading
and mutating the same live operational records.

### Non-goals

- No agent planner.
- No task scheduler.
- No model router.
- No workflow runtime.
- No hosted queue.
- No replacement for LangGraph, Temporal, CrewAI, AutoGen, or MCP servers.
- No mandatory write proxy for the application.
- No changes to the application's runtime read path.

### Hard boundary

`src/cfg/core/` must not import `cfgit-agent`.

The package lives outside core, likely:

```text
plugins/cfg_agent/
  pyproject.toml
  cfg_agent/
    __init__.py
    actions.py
    config.py
    state.py
    policies.py
    mcp.py
    ui.py
    patches.py
```

The package depends on `cfgit>=0.1.x,<0.2.0` and uses public engine/action
interfaces. Core can expose small generic extension seams if needed, but agent
logic stays outside core.

---

## 3. The Problem

In a multi-agent environment, "database access" is too coarse.

An agent that can update a record can accidentally:

- overwrite another agent's work
- retry the same mutation twice
- edit stale state after a human or agent moved the base
- mutate a field outside its responsibility
- make a high-risk runtime behavior change without review
- leave no clear task-level trace tying reads, plans, writes, and rollback

cfgit core already gives history, drift detection, rollback, branch/PR review,
and impact. cfgit-agent adds the missing operational protocol agents should
follow before reaching for a write:

```text
announce -> claim -> inspect -> declare intent -> propose patch -> validate
-> apply/commit -> release -> close session
```

The goal is not to block speed. The goal is to make the safe path faster than an
ad-hoc raw write.

---

## 4. First-Class Actors

### Agent

An agent is the primary user of cfgit-agent. It is expected to call MCP tools or
CLI/JSON actions repeatedly during a task.

Agent needs:

- know whether it can work on a record
- avoid conflicting with other active agents
- declare what it is about to change
- get precise blockers when unsafe
- retry safely
- leave a trace a human can understand

### Human Operator

The human is the manager, reviewer, and recovery owner.

Human needs:

- see active sessions
- see who claims what
- understand open intents
- approve or reject high-risk changes
- take over expired work
- inspect conflicts
- restore/adopt using cfgit core when needed

The human should manage flow, not micromanage every tool call.

---

## 5. Opt-In Configuration

Agent coordination is disabled by default.

```toml
[agent]
enabled = true
state_backend = "auto" # memory, auto, mongo, or postgres
state_collection = "cfgit_agent_state"
events_collection = "cfgit_agent_events"
default_lease_ttl_seconds = 900

[agent.policies]
deny_paths = ["/provider_config*", "/secrets*"]
review_paths = ["/rollout*", "/pricing*"]
require_claims = true

[[agent.role]]
name = "routing-agent"
can_claim = ["modelgarden_models:*", "routing_rules:*"]

[[agent.role]]
name = "prompt-agent"
can_claim = ["agent_configs:*"]
deny_paths = ["/provider_config*"]
review_paths = ["/instructions*"]
```

The same config should work for Mongo and Postgres. Adapter-specific storage
details are hidden behind an agent-state adapter. `state_backend = "auto"` uses
the active cfgit env database type; `memory` is for local single-process testing.

---

## 6. Sidecar State Model

V1 should minimize new collections/tables.

Preferred V1 storage:

```text
cfgit_agent_state
cfgit_agent_events
```

`cfgit_agent_state` stores current mutable coordination objects with a `kind`
field:

```json
{ "kind": "session", ... }
{ "kind": "lease", ... }
{ "kind": "intent", ... }
{ "kind": "conflict", ... }
{ "kind": "idempotency", ... }
```

`cfgit_agent_events` is append-only:

```json
{ "event": "session.started", ... }
{ "event": "lease.acquired", ... }
{ "event": "intent.opened", ... }
{ "event": "patch.validated", ... }
{ "event": "patch.applied", ... }
{ "event": "conflict.detected", ... }
{ "event": "session.completed", ... }
```

Later, if query load or indexing demands it, split state into separate tables.
Do not start there.

---

## 7. Object Specs

### 7.1 Session

A session is one agent run or task.

```json
{
  "kind": "session",
  "session_id": "ses_01h...",
  "agent_id": "agent.refund-writer",
  "agent_kind": "codex|claude-code|custom|service",
  "task": "Update refund policy copy",
  "actor": "bot@runtime",
  "status": "running",
  "started_at": "2026-07-03T10:00:00Z",
  "heartbeat_at": "2026-07-03T10:02:00Z",
  "ended_at": null,
  "tool_client": "mcp",
  "metadata": {}
}
```

Statuses:

```text
running | blocked | completed | failed | abandoned
```

Rules:

- Every lease, intent, patch, and conflict should link to a session.
- A session must heartbeat while it owns active leases.
- Expired sessions do not automatically release leases until a cleanup action or
  takeover marks the transition. This avoids silent ownership changes.

### 7.2 Lease / Claim

A lease is a time-bounded claim on a resource.

Resources:

```text
collection:id
collection:id:/json/path
collection:*
```

Example:

```json
{
  "kind": "lease",
  "lease_id": "lea_01h...",
  "session_id": "ses_01h...",
  "resource": "agent_configs:refund_resolution:/instructions",
  "collection": "agent_configs",
  "record_id": "refund_resolution",
  "path": "/instructions",
  "scope": "field",
  "status": "active",
  "reason": "Edit refund copy",
  "created_at": "2026-07-03T10:01:00Z",
  "expires_at": "2026-07-03T10:16:00Z",
  "released_at": null
}
```

Statuses:

```text
active | released | expired | stolen
```

Conflict rules:

- record lease conflicts with any field lease on the same record
- collection lease conflicts with any record or field lease in that collection
- field leases conflict only when paths overlap
- `/a` overlaps `/a/b`; `/a/b` does not overlap `/a/c`

### 7.3 Intent

An intent declares planned work before mutation.

```json
{
  "kind": "intent",
  "intent_id": "int_01h...",
  "session_id": "ses_01h...",
  "resources": ["agent_configs:refund_resolution"],
  "summary": "Lower refund automation threshold and update enterprise fallback wording.",
  "planned_paths": ["/automation_threshold", "/instructions"],
  "risk_level": "medium",
  "expected_base": {
    "agent_configs:refund_resolution": {
      "head_seq": 7,
      "head_oid": "sha256:..."
    }
  },
  "idempotency_key": "task-123:refund-policy-edit",
  "status": "open",
  "created_at": "2026-07-03T10:03:00Z",
  "closed_at": null
}
```

Statuses:

```text
open | committed | superseded | rejected | abandoned
```

Rules:

- Mutating agent actions should require an open intent when
  `require_intent_for_write = true`.
- Intent base must be checked again at patch validation/apply time.
- Intent does not reserve a resource by itself. Lease owns coordination; intent
  owns purpose.

### 7.4 Patch

V1 patches should use RFC 6902 JSON Patch for precision.

```json
{
  "record": "agent_configs:refund_resolution",
  "base": {
    "head_seq": 7,
    "head_oid": "sha256:..."
  },
  "patch": [
    { "op": "replace", "path": "/automation_threshold", "value": 0.76 },
    { "op": "replace", "path": "/instructions", "value": "..." }
  ],
  "idempotency_key": "task-123:patch-1",
  "intent_id": "int_01h..."
}
```

Rules:

- Patches are preferred over whole-document writes.
- Patch paths must be covered by active leases unless policy allows claimless
  writes.
- Patch base must match cfgit HEAD and live drift state according to policy.
- If live has drifted from HEAD, validation must fail with a drift conflict
  unless the intent explicitly says it is adopting drift.
- Idempotency key prevents duplicate retries.

### 7.5 Conflict

Conflict is a first-class object, not just an error string.

```json
{
  "kind": "conflict",
  "conflict_id": "con_01h...",
  "session_id": "ses_01h...",
  "resource": "agent_configs:refund_resolution:/instructions",
  "type": "path_overlap",
  "severity": "blocking",
  "sessions": ["ses_a", "ses_b"],
  "paths": ["/instructions"],
  "message": "Another active lease overlaps /instructions.",
  "resolution": "wait|rebase|abandon|human_override",
  "status": "open",
  "created_at": "2026-07-03T10:04:00Z",
  "resolved_at": null
}
```

Types:

```text
lease_conflict
base_moved
path_overlap
policy_block
live_drift
idempotency_replay
session_expired
human_review_required
```

---

## 8. Adapter Requirements

cfgit-agent needs an `AgentStateAdapter` separate from cfgit core's
`StorageAdapter`.

```python
class AgentStateAdapter(Protocol):
    def init_agent_state(self) -> None: ...

    def create_session(self, session: dict) -> dict: ...
    def heartbeat_session(self, session_id: str, now: datetime) -> dict: ...
    def close_session(self, session_id: str, status: str, now: datetime) -> dict: ...
    def get_session(self, session_id: str) -> dict | None: ...
    def list_sessions(self, status: str | None = None) -> list[dict]: ...

    def acquire_lease(self, lease: dict, *, now: datetime) -> dict: ...
    def renew_lease(self, lease_id: str, *, ttl_seconds: int, now: datetime) -> dict: ...
    def release_lease(self, lease_id: str, *, now: datetime) -> dict: ...
    def list_leases(self, *, active_only: bool = True) -> list[dict]: ...

    def open_intent(self, intent: dict) -> dict: ...
    def close_intent(self, intent_id: str, status: str, now: datetime) -> dict: ...
    def get_intent(self, intent_id: str) -> dict | None: ...
    def list_intents(self, status: str | None = None) -> list[dict]: ...

    def remember_idempotency(self, key: str, result: dict, now: datetime) -> dict | None: ...
    def get_idempotency(self, key: str) -> dict | None: ...

    def open_conflict(self, conflict: dict) -> dict: ...
    def resolve_conflict(self, conflict_id: str, resolution: str, now: datetime) -> dict: ...
    def list_conflicts(self, status: str | None = None) -> list[dict]: ...

    def append_event(self, event: dict) -> None: ...
    def list_events(self, *, since: datetime | None = None, limit: int = 100) -> list[dict]: ...
```

Atomicity requirements:

- `acquire_lease` must be atomic against overlapping active leases.
- `remember_idempotency` must atomically return the previous result if the key
  already exists.
- `append_event` should be best-effort but must never break the state mutation
  it describes. If a store cannot atomically write state + event, state wins and
  a later `fsck` can report missing events.

Mongo implementation:

- store state in `cfgit_agent_state`
- unique indexes:
  - `kind + session_id` for sessions
  - `kind + lease_id` for leases
  - `kind + intent_id` for intents
  - `kind + conflict_id` for conflicts
  - `kind + idempotency_key` for idempotency
- lease overlap requires query + transaction or compare-and-set inside a
  transaction

Postgres implementation:

- one `cfgit_agent_state` table with `kind text`, `id text`, `doc jsonb`
- one `cfgit_agent_events` table with append-only rows
- use transaction + row locks / exclusion strategy for lease conflict detection

---

## 9. Safety Invariants

These are non-negotiable.

1. **No hidden runtime mutation.** Any agent action that changes live records must
   go through cfgit core mutation paths.
2. **No stale base writes.** A patch prepared against base X must fail if HEAD or
   live drift moved in a way policy says is unsafe.
3. **No claim bypass when enabled.** If `require_claim_for_write` is true,
   mutating actions without an active lease fail.
4. **No intent bypass when enabled.** If `require_intent_for_write` is true,
   mutating actions without an open matching intent fail.
5. **No duplicate retry writes.** Idempotency keys return the previous result.
6. **No silent conflict resolution.** Conflicts are recorded and returned as
   structured data.
7. **No agent self-approval for human-review-required actions.** Agent can request
   review; it cannot grant it.
8. **No LLM dependency in core or cfgit-agent required path.** LLM narration stays
   optional and can be delegated to `cfgit-impact`.
9. **No cross-project DB writes by default.** The configured environment is the
   only target. Agents should see and report the env/db they are mutating.

---

## 10. Agent MCP Tools

V1 MCP tools should be the primary surface. CLI can mirror them for humans and
scripts, but design the protocol around agents.

### Session

```text
cfg_agent_start_session(task, agent_id?, agent_kind?, metadata?)
cfg_agent_heartbeat(session_id)
cfg_agent_end_session(session_id, status, summary?)
cfg_agent_status(session_id?)
```

### Claims

```text
cfg_agent_claim(session_id, resource, ttl_seconds?, reason?)
cfg_agent_release(session_id, lease_id)
cfg_agent_renew(session_id, lease_id, ttl_seconds?)
cfg_agent_claims(active_only?)
```

### Intents

```text
cfg_agent_open_intent(session_id, resources, summary, planned_paths,
                      risk_level?, idempotency_key?)
cfg_agent_close_intent(session_id, intent_id, status)
cfg_agent_intents(status?)
```

### Patch Flow

```text
cfg_agent_prepare_patch(session_id, record, patch, intent_id, idempotency_key?)
cfg_agent_validate_patch(session_id, prepared_patch_id | patch_payload)
cfg_agent_apply_patch(session_id, prepared_patch_id | patch_payload, message)
cfg_agent_commit(session_id, record, doc_or_patch, message, intent_id,
                 idempotency_key?)
```

Naming note: if V1 does not persist prepared patches, combine prepare/validate
into `cfg_agent_validate_patch` and accept the full patch payload.

### Conflicts and Events

```text
cfg_agent_conflicts(status?)
cfg_agent_explain_blocker(conflict_id | last_error)
cfg_agent_resolve_conflict(conflict_id, resolution, note?)
cfg_agent_watch(since?, limit?)
```

### Human Review

```text
cfg_agent_request_review(session_id, intent_id, reason)
cfg_agent_reviews(status?)
cfg_agent_approve_review(review_id, note)
cfg_agent_reject_review(review_id, note)
```

V1 may defer review tools if core approval primitives are not ready, but the
spec should reserve the shape.

---

## 11. CLI Shape

CLI mirrors MCP for humans and scripts:

```bash
cfg agent start-session -m "Update refund policy" --json
cfg agent claim agent_configs:refund_resolution --session ses_... --json
cfg agent intent open --session ses_... --record agent_configs:refund_resolution \
  --path /instructions --path /automation_threshold \
  -m "Lower automation threshold and clarify enterprise refund language" --json
cfg agent patch validate --session ses_... --patch patch.json --json
cfg agent patch apply --session ses_... --patch patch.json -m "update refund policy" --json
cfg agent end-session ses_... --status completed --json
```

All commands return the same envelope style as cfgit actions.

---

## 12. Human Operator UI

The existing local UI should grow an optional Agent tab when `[agent].enabled`.

Views:

### Active Sessions

Rows:

- agent id
- task
- status
- heartbeat age
- claims count
- open intents count
- blocked/conflict count

Actions:

- inspect
- mark abandoned
- release expired leases
- request summary

### Claims

Rows:

- resource
- owner session
- scope
- expires in
- reason

Actions:

- release own claim
- takeover expired claim
- inspect conflicting claims

### Intents

Rows:

- summary
- resources
- planned paths
- risk
- status
- linked impact

Actions:

- approve/reject if review is enabled
- inspect diff/impact
- open related record

### Conflicts

Rows:

- type
- resource
- sessions involved
- blocker message
- suggested resolution

Actions:

- wait
- release stale lease
- request rebase
- human override

### Event Feed

Timeline:

- session started
- claim acquired
- intent opened
- patch validated
- commit applied
- conflict detected
- restore/adopt from core

The UI is observational and operational. It should not become a hosted control
plane in V1.

---

## 13. Patch Validation Algorithm

Input:

- session id
- target record
- base seq/oid
- JSON Patch
- intent id
- idempotency key

Steps:

1. Load session. It must be `running` or `blocked` with resumable status.
2. Check idempotency key. If present and already completed, return previous
   result.
3. Load active leases for session and target.
4. Check each patch path is covered by a compatible lease.
5. Load intent. It must be open and include the target resource.
6. Check patch paths are a subset of `planned_paths` unless policy allows
   expansion.
7. Load cfgit HEAD and live status.
8. If HEAD moved from expected base, fail with `base_moved`.
9. If live drift exists and policy disallows drifted base, fail with
   `live_drift`.
10. Apply JSON Patch to the HEAD document in memory.
11. Run cfgit diff against base.
12. Run policy hooks:
    - denied paths
    - allowed role paths
    - secret-shaped values
    - required impact
    - required human review
13. Return:
    - `ok`
    - `needs_review`
    - `conflict`
    - `policy_block`

No live write happens in validate.

---

## 14. Apply Algorithm

Input:

- validated patch payload or prepared patch id
- message
- session id
- intent id
- idempotency key

Steps:

1. Re-run validation. Never trust an older validation result.
2. If validation result is not `ok`, return it and create/update conflict.
3. Build the patched full document.
4. Call cfgit core commit/apply path with base checks.
5. Store idempotency result.
6. Mark intent `committed` if all resources are done.
7. Append events:
   - `patch.applied`
   - `commit.created`
8. Return cfgit commit result plus agent metadata.

Open question for V1: whether `apply_patch` should commit directly or create a
cfgit branch draft commit. Recommendation: V1 direct-commits to runtime only in
dev/open environments and uses branch/PR flow in restricted/high-risk
environments.

---

## 15. Relationship To cfgit Branches / PRs

Agent coordination and cfgit branches solve different problems.

Claims/intents answer:

> Who is working on what right now, and are they allowed to touch it?

Branches/PRs answer:

> What proposed change should be reviewed before runtime mutation?

Recommended mapping:

- low-risk dev edit: claim + intent + validate + direct commit
- medium-risk edit: claim + intent + validate + branch draft commit
- high-risk/prod edit: claim + intent + validate + branch draft commit + PR +
  human approval + merge

Agent plugin should not invent a second PR system. It should call cfgit branch
and PR actions when review is needed.

---

## 16. Policy Hooks

Policy hooks are local deterministic checks.

V1 built-ins:

- `require_session`
- `require_claim`
- `require_intent`
- `path_allowlist`
- `path_denylist`
- `secret_value_block`
- `base_must_match`
- `no_live_drift`
- `require_impact`
- `require_human_review`

Plugin hook shape:

```python
class AgentPolicyHook(Protocol):
    name: str

    def evaluate(self, ctx: AgentPolicyContext) -> AgentPolicyResult: ...
```

Result:

```json
{
  "status": "ok|warn|block|needs_review",
  "code": "path_denied",
  "message": "routing-agent cannot edit /provider_config",
  "details": {}
}
```

Policy hooks must be deterministic. If an LLM is used to explain a policy result,
that explanation is non-authoritative.

---

## 17. Idempotency

Agents retry. The system must assume duplicate tool calls.

Idempotency key scope:

```text
env + session_id + idempotency_key
```

Rules:

- If a key is seen with the same normalized payload, return the previous result.
- If a key is seen with a different payload, block with `idempotency_conflict`.
- Store enough result metadata for agents to resume without guessing.
- Expire idempotency entries after configured window.

---

## 18. Event Feed

Events are for both humans and agents.

Event shape:

```json
{
  "event_id": "evt_01h...",
  "event": "lease.acquired",
  "session_id": "ses_01h...",
  "actor": "bot@runtime",
  "resource": "agent_configs:refund_resolution",
  "recorded_at": "2026-07-03T10:00:00Z",
  "details": {}
}
```

V1 watch is polling:

```text
cfg_agent_watch(since_event_id?, limit?)
```

Future watch can be SSE/websocket in the local UI or MCP resource subscription
if the client supports it.

---

## 19. Failure Modes

### Agent dies with active lease

Lease expires. UI shows stale owner. Another session can request takeover.
Takeover records:

- old session
- new session
- reason
- whether old lease was expired

### Agent retries after successful apply

Idempotency returns previous commit result.

### Agent validates, then another writer changes live DB

Apply re-runs validation and fails with `live_drift` or `base_moved`.

### Two agents claim sibling fields

Allowed if paths do not overlap and policy allows field claims.

### Two agents claim same record, different fields, but commit whole docs

Block. Whole-doc writes require record lease. Field leases require patch-based
apply.

### Human edits DB directly

cfgit core drift detection catches it. cfgit-agent reports open sessions whose
base is now stale.

### Agent attempts prohibited path

Policy block with exact path and role reason. No mutation.

---

## 20. Package / Release Shape

Same repository, separate PyPI package:

```text
cfgit
cfgit-impact
cfgit-agent
```

Why:

- cfgit core stays lightweight
- agent coordination can iterate quickly
- users do not install agent surfaces unless they need them
- sidecar state is explicit and opt-in
- release workflow can publish all packages from one repo

V1 package metadata:

```toml
[project]
name = "cfgit-agent"
version = "0.1.0"
dependencies = [
  "cfgit>=0.1.2,<0.2.0"
]
```

Optional extras if needed:

```toml
[project.optional-dependencies]
mcp = ["mcp>=1.0"]
dev = ["pytest>=8.0", "ruff", "mypy"]
```

---

## 21. V1 Build Order

### Stage 0: Spec and package scaffold

- Add `plugins/cfg_agent/`
- Add package metadata
- Add README
- Add import boundary tests
- Add docs index links

Acceptance:

- `python -m build` works for `cfgit-agent`
- core purity tests prove no import from core to agent package

### Stage 1: State adapter and models

- Agent config parser
- In-memory test adapter
- Mongo state adapter
- Postgres state adapter if cheap; otherwise after Mongo
- session CRUD
- event append/list

Acceptance:

- start/end/heartbeat session tests
- event feed tests
- Mongo local integration tests if available

### Stage 2: Leases

- resource parser
- path overlap logic
- acquire/renew/release/list
- expiration and takeover semantics
- conflict object for lease conflict

Acceptance:

- record vs field conflict tests
- non-overlapping field lease tests
- expired lease takeover tests
- idempotent release tests

### Stage 3: Intents and idempotency

- open/close/list intents
- expected base capture
- idempotency storage
- idempotency replay/conflict checks

Acceptance:

- duplicate retry returns previous result
- duplicate key with different payload blocks
- intent path expansion policy tested

### Stage 4: Patch validation

- JSON Patch apply in memory
- claim coverage check
- intent coverage check
- base/head/live checks via cfgit core
- built-in policies

Acceptance:

- safe patch validates
- stale base fails
- live drift fails
- denied path fails
- missing claim/intent fails when required

Current implementation status: validation is implemented for sessions, claims,
intents, base movement, live drift, and JSON Patch application.

### Stage 5: Patch apply / commit

- Re-run validation at apply time
- Call cfgit core commit path
- Link commit result to session/intent/event
- Release optional auto-release behavior behind config

Acceptance:

- successful patch creates cfgit history
- retry returns same result
- race between validate/apply fails closed

Current implementation status: safe apply is implemented against cfgit core's
commit path with validation re-run, intent closure, idempotent replay, and
events. In-memory, Mongo, and Postgres agent-state adapters are implemented, as
are deterministic policy hooks for claims, denied paths, and configured secret
fields.

### Stage 6: MCP tools

- Expose session tools
- Expose claim tools
- Expose intent tools
- Expose validate/apply tools
- Expose conflicts/watch tools

Acceptance:

- MCP tests assert payload forwarding and action envelopes
- skill docs include agent-first workflow

### Stage 7: UI agent tab

- Active sessions
- Claims
- Intents
- Conflicts
- Event feed
- Buttons for safe human actions

Acceptance:

- UI server tests for rendered controls
- browser smoke when connector is available

Current implementation status: the localhost UI includes an optional agent
coordination manager. It reports whether `[agent]` is enabled, shows sessions,
claims, intents, conflicts, and events, and exposes safe human actions for
ending sessions, releasing leases, and abandoning open intents.

### Stage 8: Branch/PR integration

- Policy can route patch to direct commit or branch draft
- PR review can link back to agent intent/session
- Human merge remains the only high-risk runtime mutation

Acceptance:

- high-risk policy creates branch/PR instead of direct runtime commit
- merge checks stale base and drift

Current implementation status: review-required paths are supported through
`[agent.policies].review_paths` or per-role `review_paths`. `apply_patch`
validates the patch, creates a cfgit draft branch commit, opens a cfgit PR,
links the PR to the agent session/intent, closes the intent as
`review_requested`, and does not mutate runtime. Runtime mutation remains cfgit
PR merge.

---

## 22. V1 Cut Line

Build V1:

- package scaffold
- config
- sessions
- leases
- intents
- idempotency
- JSON Patch validation
- JSON Patch apply through cfgit core
- structured conflicts
- MCP tools
- basic UI tab

Do not build in V1:

- scheduler
- autonomous planner
- hosted service
- queue workers
- LLM judge policies
- distributed lock service beyond DB-backed leases
- websocket/SSE watch
- complex automatic merge
- cross-database transaction coordination

---

## 23. Acceptance Scenario

Simulate three agents and one human operator.

Initial state:

- `agent_a` edits `/instructions` on `agent_configs:refund_resolution`
- `agent_b` edits `/automation_threshold` on the same record
- `agent_c` attempts `/provider_config/api_key`
- human watches UI

Expected:

1. `agent_a` starts session and claims `/instructions`.
2. `agent_b` starts session and claims `/automation_threshold`.
3. Both claims succeed because paths do not overlap.
4. `agent_c` tries to claim or edit `/provider_config/api_key`.
5. Policy blocks `agent_c`.
6. `agent_a` opens intent, validates patch, applies patch.
7. `agent_b` validates against stale base after `agent_a` commits.
8. `agent_b` receives `base_moved` with rebase guidance.
9. Human sees:
   - two active/completed sessions
   - one policy block conflict
   - one stale-base conflict
   - one applied cfgit commit
10. Rollback remains available through cfgit core.

This scenario is the minimum proof that cfgit-agent is not just logging. It
actively coordinates non-overlapping work, blocks unsafe paths, detects stale
bases, and keeps human-visible audit.

---

## 24. Open Questions

1. Should V1 require JSON Patch only, or also support merge patch?
   - Recommendation: JSON Patch only. It maps cleanly to field claims.

2. Should field claims be enabled by default?
   - Recommendation: yes for agent mode, because whole-record claims are too
     coarse for parallel agents.

3. Should direct apply be allowed in production?
   - Recommendation: configurable, default false for restricted/prod envs. Route
     through cfgit branch/PR instead.

4. Should sessions auto-close when all leases released and intents committed?
   - Recommendation: no. Agents should close explicitly; cleanup can mark stale
     sessions abandoned.

5. Should human review live in cfgit-agent or cfgit core?
   - Recommendation: cfgit core owns generic approval primitives; cfgit-agent
     owns agent-specific review requests and links them to sessions/intents.

6. Should agent state be versioned by cfgit itself?
   - Recommendation: no. Agent state is operational metadata. Event feed is the
     audit. cfgit history is for the user-configured live records.

---

## 25. Design Principle

Agent mode should make the correct thing obvious:

```text
I am agent X.
I am working on Y.
I intend to change Z.
Here is the patch.
Here is the validation.
Here is the commit or blocker.
Here is how a human can recover.
```

That protocol is the product.
