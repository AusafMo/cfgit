# cfgit YC-style demo runbook

Goal: record a 2-3 minute founder demo that makes the product obvious fast:
runtime config changed in the database, cfgit catches it, shows the exact
behavior delta, reasons about impact, then proves the same workflow is scriptable.

## Setup

Use only the synthetic demo database:

```bash
.venv/bin/cfg --config-file /private/tmp/cfgit-llm-demo.toml --env dev --author demo.user@example.com
```

The local UI is at:

```text
http://127.0.0.1:8777/
```

For the recorded demo, start the UI with an explicit impact provider/model:

```bash
CFGIT_UI_IMPACT_PROVIDER=gemini CFGIT_UI_IMPACT_MODEL=gemini-2.5-flash \
  .venv/bin/cfg --config-file /private/tmp/cfgit-llm-demo.toml --env dev \
  --author demo.user@example.com ui --port 8777 --no-open
```

Prepared state:

- `Drift 5`
- hero drift: `agent_configs:refund_resolution`
- review branch: `support-orchestrator-review`
- open PR: use the current id from `cfg pr list --status open`

## Screen Flow

1. Start on overview.
   Voiceover: "A lot of AI products keep live behavior in Mongo: prompts, routing,
   policies, eval gates, rollout controls. The app still reads that database.
   cfgit sits beside it and adds git-shaped control."

2. Click `Drift 5`, then `agent_configs:refund_resolution`.
   Voiceover: "This refund agent was edited outside cfgit. cfgit did not block
   the admin-console edit. It just refuses to pretend nothing changed."

3. Pause on the diff.
   Call out: model changed, threshold dropped, refund cap moved from `250` to
   `500`, and enterprise courtesy-credit behavior was added.
   Voiceover: "This is the actual runtime behavior delta, not a stale config
   file or a prompt-registry copy."

4. Click `Impact`.
   Voiceover: "Now cfgit maps the blast radius across related agents, policies,
   evals, tools, routing, and rollout controls. This is why live config changes
   need review."

5. Select branch `support-orchestrator-review`, click `Diff`.
   Voiceover: "Branches are sparse draft timelines. They do not checkout the
   database. Runtime only changes on merge."

6. Click `PR`.
   Voiceover: "A PR is review metadata in cfgit's sidecar store. Humans and
   agents can use the same semantics around records already living in the
   datastore."

7. Switch to terminal and run `/tmp/cfgit_demo_terminal.sh`.
   Voiceover: "And because this is CLI-first, the same flow works for scripts,
   CI, and MCP clients."

## Tight Closing Line

"cfgit is git-shaped safety for live datastore records: history, drift, diff,
impact, rollback, branches, and PRs, without moving the data or putting a gateway
in your runtime path."
