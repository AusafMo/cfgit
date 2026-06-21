---
name: cfgit
description: Use cfgit to inspect, diff, version, adopt, restore, tag, and explain records in a live datastore without owning the datastore. Trigger when working in a repo that has `.cfg.toml`, the `cfg` CLI, or a cfgit MCP server, especially for runtime config, prompt, model routing, feature flag, or control-plane records.
---

# cfgit

Use cfgit as the safety layer around a live datastore. The app still reads and writes the datastore directly; cfgit records history beside it and detects drift from out-of-band writes.

## Rules

- Never write through a raw database client when a `cfg` operation can do the mutation.
- Never use cross-project Mongo URIs for writes. Remote managed Mongo writes are forbidden unless the user explicitly grants a per-turn exception and asks for that exact target.
- Treat `changed_outside_cfgit` as the central state. Do not commit over it. Run `cfg diff`, explain what changed, then `cfg adopt` if the user wants to fold that live state into history.
- Prefer `--json` for agent parsing.
- Use deterministic `cfg impact` first. Add `--llm` only when the user asks for LLM narration and the impact plugin is installed.

## Workflow

1. Identify the env and record.
   - Record syntax is `collection:id`, for example `agent_configs:agent_planner`.
   - Use `cfg whoami --json` and `cfg status --json`.

2. Inspect before mutation.
   - `cfg diff <record> =HEAD =live --json`
   - `cfg log <record> --json`
   - `cfg impact <record> =HEAD =live --json`

3. Mutate through cfgit.
   - Write the full target document to a temp JSON file.
   - Run `cfg commit <record> --from <file> -m "<message>" --json`.
   - If commit returns `changed_outside_cfgit`, stop and inspect drift.

4. Reconcile drift.
   - `cfg diff <record> =HEAD =live --json`
   - `cfg adopt <record> -m "<message>" --json`

5. Restore.
   - Single record: `cfg restore <record> <ref> -m "<message>" --json`
   - System preview: `cfg restore --as-of YYYY-MM-DD --dry-run -m "<message>" --json`
   - System apply only when the author is allowed: `cfg restore --as-of YYYY-MM-DD -m "<message>" --json`

## MCP Tools

If the cfgit MCP server is available, prefer its tools over shelling out:

- `cfg_status`
- `cfg_diff`
- `cfg_impact`
- `cfg_commit`
- `cfg_log`
- `cfg_show`
- `cfg_adopt`
- `cfg_restore`
- `cfg_tag`
- `cfg_fsck`
- `cfg_whoami`
- `cfg_init`

Every MCP tool returns the same envelope shape as the CLI exit status: `status`, `code`, `message`, `data`.
