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

The LLM provider layer lives in `plugins/cfg_impact`. `--llm` is blocked unless
the record is allowlisted in `[connections].share_with_ai`.

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
