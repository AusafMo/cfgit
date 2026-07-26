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

## Preview a commit

Add `--dry-run` to a single-record commit to see the exact field-level delta versus the live
record and exit without writing. It returns `would_commit` with the changes, or the same
`changed_outside_cfgit` / `noop` a real commit would, and still runs the secret policy so a
blocked secret surfaces before you write:

```bash
cfg commit agent_configs:agent_planner --from planner.json --dry-run
```

## Fast field edits (set / edit)

For a few scalar fields you do not need a full document or a temp file. `cfg set` fetches the
live document, applies your changes, and commits through the same path as `cfg commit` — so it
still refuses on out-of-band drift instead of clobbering it (it is not a raw write):

```bash
cfg set modelgarden_models:openai/gpt-4o-mini enabled=true retry.max=3 -m "enable + retry"
```

Paths are dotted, with list indices as `tags[0]`. Values are JSON-coerced — `enabled=true`
becomes a boolean, `n=5` an integer, `tags=["a","b"]` a list — and a `str:` prefix forces a
string (`version=str:1.0`). Add `--dry-run` to preview. To hand-edit the whole document in your
`$EDITOR` instead, use `cfg edit <record> -m "message"`.

## Situational awareness

`cfg doctor --status` prints a one-look header of where you are pointed: resolved config file,
env, target database, identity mode and whether it is verified, store reachability, and the
number of tracked and drifted records. If a `needs_approval` environment is still in `open`
identity mode — so writes would succeed unaudited — it prints a warning with the fix. That same
warning also prints on any mutating command against such an environment; suppress it in scripts
with `CFG_WARN_OPEN_MODE=0`.

## Guidance on every outcome

Every refusal and terminal outcome carries a `next` block: a plain-language reason plus the
exact next command(s) to run (for drift, that is `cfg diff` then `cfg adopt`). On the CLI it
prints to stderr; over `--json` and MCP it is a structured `next` field, so agents can follow
`next.commands` without guessing.

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
layer as the CLI and MCP server. Before a record is selected, the middle rail
shows recent activity across all configured records: current live drift plus the
latest cfgit history entries. Selecting one of those entries opens that record's
normal history and diff view.

## Output mode

By default output is human-readable when stdout is a terminal and JSON when piped or
redirected, so interactive use reads well while pipelines stay parseable. Control it with
`CFG_OUTPUT`:

```bash
cfg status                       # human on a TTY, JSON when piped
cfg --json status                # always JSON
CFG_OUTPUT=json cfg status       # always JSON
CFG_OUTPUT=human cfg status      # always human
```

`--json` and `CFG_OUTPUT=json` always force JSON. Agents and scripts should branch on `status`
and `state`, not on human-readable text.

## Session defaults

To avoid repeating flags, set environment defaults once:

```bash
export CFG_ENV=prod        # default for --env
export CFG_CONFIG=./cfgit/aistudio.cfg.toml   # default for --config-file
export CFG_AUTHOR=you@example.com             # default author (open identity mode)
```

cfgit also discovers `.cfg.toml` by walking up from the current directory, so you can run it
from a subdirectory without `--config-file`. Global flags (`--config-file`, `--env`, `--author`,
`--branch`, `--json`) go before the subcommand: `cfg --env prod status`.
