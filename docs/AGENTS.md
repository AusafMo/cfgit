# Agents, MCP, and Impact

cfgit is designed for humans and AI coding agents to use the same operation
surface.

## Agent rule

Do not write directly to the database when cfgit can perform the mutation. Use
cfgit so drift, history, and rollback remain coherent.

Recommended agent flow:

1. `cfg_status`
2. `cfg_diff` if there is drift or a planned edit
3. `cfg_impact` before committing behavior-affecting records
4. `cfg_commit`, `cfg_bulk_commit`, or `cfg_adopt`
5. `cfg_status` again

If status is `changed_outside_cfgit`, do not commit over it. Diff it, explain it,
then ask whether to adopt it or merge manually.

For coupled multi-record edits, prefer one `cfg_bulk_commit` call over several
independent `cfg_commit` calls. It preflights every target first, so a known drift
or secret-policy failure blocks the whole batch before any record is written.
If it returns `blocked`, nothing was applied. If it returns `partial`, inspect
`results`, `failed`, and `pending` before continuing.

## Agent Identity

In `authenticated` or `enforced` environments, run the agent process with its own
private token:

```bash
export CFGIT_IDENTITY_TOKEN='agent-private-string'
cfg whoami
```

Configure only the token hash in `.cfg.toml`, mapped to an agent author such as
`codex-agent@example.com`. The MCP `author` argument is only a hint in verified
modes; cfgit refuses it if it does not match the token or DB principal identity.

For real human or agent setup secrets, prefer local hashing:

```bash
printf '%s' 'agent-private-string' | cfg identity-hash --stdin
```

The MCP server also exposes `cfg_identity_hash` for setup convenience, but tool
inputs may be visible to the MCP client. Do not send real production identity
tokens through MCP unless that client/session is trusted for secrets.

## MCP server

Install the MCP extra:

```bash
pip install 'cfgit[mcp]'
```

Run:

```bash
cfg-mcp
```

The MCP tools return a uniform envelope:

```json
{
  "status": "ok",
  "code": 0,
  "message": "",
  "data": {}
}
```

Tool list:

- `cfg_whoami`
- `cfg_init`
- `cfg_status`
- `cfg_import`
- `cfg_diff`
- `cfg_impact`
- `cfg_commit`
- `cfg_bulk_commit`
- `cfg_log`
- `cfg_show`
- `cfg_adopt`
- `cfg_restore`
- `cfg_tag`
- `cfg_fsck`
- `cfg_identity_hash`

`cfg_impact` accepts `against` as either a list of `collection:id` strings or a
comma/space-separated string when narration should be scoped to selected records.
`cfg_bulk_commit` accepts `items` as either a structured list
(`[{record, doc}]`), a mapping (`{"collection:id": doc}`), or a JSON string in
either shape.

If `cfg_log`, `cfg_show`, or a diff/impact path returns `bad_config` saying
history exists under another env, the same history store has been opened under a
different env name. Switch to the env named in the message or fix `.cfg.toml`
before mutating anything.

## Portable skill

The skill file is:

```text
skills/cfgit/SKILL.md
```

It is intentionally plain. It tells a coding agent how to inspect first, branch on
drift, avoid raw database writes, use JSON output, and treat verified identity
as token/DB-principal based rather than author-string based.

## Impact plugin

Install:

```bash
pip install cfgit-impact
```

Deterministic impact:

```bash
cfg impact agent_configs:agent_planner --json
```

LLM narration:

```bash
cfg impact agent_configs:agent_planner --llm --json
```

Scoped narration:

```bash
cfg impact agent_configs:agent_planner --against agent_configs:critic --llm --json
```

Provider config:

```toml
[connections]
ai_provider = "claude"
```

Supported providers:

- `claude`, using `ANTHROPIC_API_KEY`
- `openai`, using `OPENAI_API_KEY`
- `gemini`, using `GEMINI_API_KEY` or `GOOGLE_API_KEY`

The impact engine calls a provider-agnostic `narrate()` or `complete()` method.
Provider selection is done by `cfg_impact.providers.factory.ImpactProviderFactory`.
No vendor provider code lives in `src/cfg/core`.

## Agent coordination plugin

`cfgit-agent` is an optional package for multi-agent coordination over shared
live database records. It keeps cfgit core lightweight while adding agent-first
sessions, leases, intents, idempotency, conflicts, and event feeds.

It is currently in-repo development, not published to PyPI yet:

```bash
pip install -e plugins/cfg_agent
```

Current implementation slice:

- package scaffold
- in-memory state adapter
- Mongo and Postgres state adapters
- plugin-local `[agent]` config loader
- resource/path overlap checks
- sessions
- field/record/collection leases
- intents
- idempotency records
- structured conflicts
- deterministic claim/path policies
- branch/PR routing for review-required paths
- JSON Patch validation against cfgit HEAD/live state
- safe patch application through cfgit core
- MCP wrappers

The current apply path goes through cfgit core's normal commit/apply safety
checks rather than writing the database directly. Agent state is opt-in; if
`[agent].enabled = true`, use `state_backend = "auto"` to store sessions, leases,
intents, conflicts, idempotency, and events beside cfgit history.
If `[agent.policies].review_paths` matches a patch path, `apply_patch` does not
mutate runtime; it creates a cfgit branch draft commit, opens a cfgit PR, links
the PR to the agent session/intent, and returns `review_requested`.
The localhost UI includes an agent manager view for enabled projects, so humans
can inspect active sessions, claims, intents, conflicts, and events without
tailing MCP logs.

Minimal config:

```toml
[agent]
enabled = true
state_backend = "auto"
state_collection = "cfgit_agent_state"
events_collection = "cfgit_agent_events"

[agent.policies]
deny_paths = ["/provider_config*"]
review_paths = ["/rollout*", "/pricing*"]
require_claims = true
```

See [AGENT_COORDINATION_SPEC.md](AGENT_COORDINATION_SPEC.md).
