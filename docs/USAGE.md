# Usage

This guide covers the day-to-day cfgit workflow.

## Initial setup

Create `.cfg.toml`, then initialize cfgit's history tables or collections:

```bash
cfg init
```

Import the current live records as the initial history:

```bash
cfg import --all -m "initial import"
```

Check that everything is clean:

```bash
cfg status
```

## Check current state

```bash
cfg whoami
cfg status
cfg status agent_configs:agent_planner
```

States:

- `clean`: live record matches cfgit's HEAD.
- `new`: live record exists but cfgit has no history for it.
- `changed_outside_cfgit`: live record differs from cfgit's HEAD.
- `missing`: cfgit has history but the live record is missing.
- `not_found`: neither live nor history exists for that record.

Exit code `2` means drift was found.

## Identity

In the default `open` mode, cfgit records your git email or explicit `--author`
as attribution. In `authenticated` or `enforced` mode, cfgit verifies identity
before mutating history.

For token identity, each human keeps a private memorable string locally:

```bash
export CFGIT_IDENTITY_TOKEN='imkanyewest'
cfg whoami
```

The raw string is never written to history. Configure only its full SHA-256 hash:

```bash
printf '%s' 'imkanyewest' | cfg identity-hash --stdin
```

History and `whoami` show the author plus a short fingerprint, such as
`alice@example.com#abc12`. That fingerprint is display-only and cannot be used
as a login token.

## Diff

Compare saved HEAD to live:

```bash
cfg diff agent_configs:agent_planner
```

Compare explicit refs:

```bash
cfg diff agent_configs:agent_planner @1 @3
cfg diff agent_configs:agent_planner tag:june7-good =live
```

Use JSON for scripts:

```bash
cfg --json diff agent_configs:agent_planner =HEAD =live
```

## Commit

cfgit commits full JSON documents. Write the intended document to a file, then:

```bash
cfg commit agent_configs:agent_planner --from planner.json -m "adjust planner model"
```

For a multi-record change, write a batch file:

```json
[
  {"record": "agent_configs:planner", "doc": {"config_id": "planner", "model": "fast"}},
  {"record": "modelgarden_models:openai/gpt-4o-mini", "doc": {"model_path": "openai/gpt-4o-mini"}}
]
```

Then commit it:

```bash
cfg commit --bulk-from batch.json -m "switch planner routing"
```

Bulk commit preflights every target before writing. If any record has un-adopted
drift, is missing, duplicates another target, or trips the secret policy, no
record in the batch is applied.

If the live record changed after cfgit last recorded it, commit returns
`changed_outside_cfgit` and does not apply your document. Inspect the drift first:

```bash
cfg diff agent_configs:agent_planner =HEAD =live
```

Then adopt or resolve manually.

`commit`, `import`, and `adopt` also scan the would-be-stored document for
secret-shaped fields and values from `[secrets]`. Fields in `secret_fields` are
stripped before history. Use `--allow-secret` only for intentional fixtures or
false positives; cfgit records that override in history metadata.

## Branches and PRs

Branching is opt-in:

```toml
[branches]
enabled = true
refs_collection = "cfgit_refs"
default_branch = "main"
```

Run `cfg init` after enabling it. The default `main` branch is the live runtime
line. Branch and PR objects live in cfgit's refs store; they do not replace
runtime records.

Create a branch and draft a single-record change:

```bash
cfg branch create router-test --from main
cfg --branch router-test commit agent_configs:agent_planner --from planner.json -m "try router"
cfg diff main..router-test
cfg --branch router-test log
```

Draft a multi-record change with the same bulk file format:

```bash
cfg --branch router-test commit --bulk-from batch.json -m "try router batch"
```

Open and merge a PR:

```bash
cfg pr create --base main --head router-test -m "review router"
cfg pr list
cfg pr show <pr-id>
cfg pr merge <pr-id>
```

`cfg pr merge` is the only branch/PR command that mutates runtime. It writes a
normal canonical history entry with `op = "merge"` and source PR metadata. It
refuses stale main heads and live drift. In this v1 slice, multi-record PR merge
is blocked unless the adapter exposes true batch atomicity; split those changes
into one-record PRs when you need a merge today.

`cfg switch <branch>` only writes local CLI state under `.cfgit/state.json`.
It does not change runtime. Agents and scripts should prefer explicit
`--branch <name>` so the target is visible in logs.

## Adopt drift

When a record was changed by a script, admin console, database shell, or another
tool, adopt it into history:

```bash
cfg adopt agent_configs:agent_planner -m "adopt admin console edit"
```

Adopt every currently drifted record:

```bash
cfg adopt --all -m "adopt live drift after release testing"
```

Adopt does not change the live record. It records the current live record as the
new HEAD.

## Log and show

```bash
cfg log agent_configs:agent_planner
cfg show agent_configs:agent_planner HEAD
cfg show agent_configs:agent_planner @2
cfg show agent_configs:agent_planner tag:june7-good
```

## Tags

Tag the current HEAD of every configured record:

```bash
cfg tag before-router-change
```

Restore by tag later:

```bash
cfg restore --tag before-router-change --dry-run -m "preview restore"
cfg restore --tag before-router-change -m "restore known-good router state"
```

## Restore

Single-record restore:

```bash
cfg restore agent_configs:agent_planner @2 -m "restore planner behavior"
```

System restore by timestamp:

```bash
cfg restore --as-of 2026-06-07 --dry-run -m "preview June 7 state"
cfg restore --as-of 2026-06-07 -m "restore June 7 state"
```

System restore is non-destructive. It writes new history entries on top rather
than deleting history.

## Impact

Deterministic local impact summary:

```bash
cfg impact agent_configs:agent_planner
```

Optional LLM narration:

```bash
cfg impact agent_configs:agent_planner --llm --json
```

Scope LLM narration to selected related records instead of the whole system:

```bash
cfg impact agent_configs:agent_planner \
  --against agent_configs:critic \
  --against modelgarden_models:openai/gpt-4o-mini \
  --llm --json
```

The LLM provider layer lives in `plugins/cfg_impact`. `--llm` is blocked unless
the record is allowlisted in `[connections].share_with_ai`. Providers are selected
with `[connections].ai_provider` or `--provider`; the plugin supports `claude`,
`openai`, and `gemini`.

## UI

```bash
cfg ui
```

The UI binds to `127.0.0.1`. It is a local operator surface over the same action
layer as the CLI and MCP server.

## JSON mode

Every command supports JSON output:

```bash
cfg --json status
cfg --json diff agent_configs:agent_planner
```

Agents and scripts should branch on `status` or the command output state, not on
human-readable text.
