# cfgit-agent

Optional agent coordination for cfgit.

This package is deliberately outside `src/cfg/core/`. cfgit core remains
non-custodial version control for live database records; cfgit-agent adds
agent-first coordination primitives around that core.

## Install

Current development install from this repository:

```bash
pip install -e plugins/cfg_agent
```

Once published, install it into the same environment as cfgit:

```bash
pip install cfgit-agent
pip install 'cfgit-agent[mongo,mcp]'
```

## What It Adds

- agent sessions
- resource claims / leases
- declared intents
- structured conflicts
- idempotency records
- event feed
- in-memory, Mongo, and Postgres coordination state adapters
- deterministic policy hooks for claim/path guardrails
- MCP tools for agent-first workflows

It does not plan tasks, schedule agents, route models, or replace LangGraph,
Temporal, AutoGen, CrewAI, or MCP database servers.

## Basic Flow

```text
start session -> claim resource -> open intent -> validate/apply patch
-> release claim -> end session
```

## Configuration

Agent coordination is disabled unless explicitly enabled:

```toml
[agent]
enabled = true
state_backend = "auto" # memory, auto, mongo, or postgres
state_collection = "cfgit_agent_state"
events_collection = "cfgit_agent_events"
default_lease_ttl_seconds = 900

[agent.policies]
deny_paths = ["/provider_config*"]
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

`state_backend = "auto"` uses the active cfgit env database type. The state
adapter stores coordination data beside cfgit history, using the configured
state and events collections/tables.

If a patch touches `review_paths`, `apply_patch` validates the patch and routes
it to cfgit's branch/PR flow instead of mutating runtime. The result state is
`review_requested`, and the opened PR includes the agent session and intent ids.

## Current Slice

The current implementation includes sessions, claims, intents, conflicts,
idempotency, events, JSON Patch validation, safe patch application through cfgit
core, durable Mongo/Postgres state adapters, deterministic policies, and MCP
wrappers, plus branch/PR routing for review-required paths. The localhost cfgit
UI includes an optional agent manager view when the package is installed and
`[agent]` is enabled.
