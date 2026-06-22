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
- Use deterministic `cfg impact` first. Add `--llm` only when the user asks for LLM narration and the impact plugin is installed. Use `--against <collection:id>` when the user wants narration scoped to specific related records instead of the whole system.
- Before the FIRST `cfg import` against a new database or `.cfg.toml`, run `cfg doctor` (read-only). It reports every secret-deny match, oversized field, and key issue at once, with paste-ready `secret_fields`/`ignore_fields` snippets. Fix `.cfg.toml` from its output, then import. This avoids import failing one secret at a time.
- In `authenticated` or `enforced` identity mode, the process must already have a verified identity source, usually `CFGIT_IDENTITY_TOKEN` or a per-user DB principal. `--author` and MCP `author` are hints only; cfgit rejects them if they do not match the verified identity.
- Never paste raw human identity tokens into prompts, logs, commits, or history. For real setup secrets, prefer local CLI hashing: `printf '%s' '<private-token>' | cfg identity-hash --stdin`.

## Workflow

0. Preflight a new config (first import only).
   - `cfg doctor --json` (writes nothing). If `ok` is false, apply the `suggestions` to `.cfg.toml` and re-run until clean.

1. Identify the env and record.
   - Record syntax is `collection:id`, for example `agent_configs:agent_planner`.
   - Use `cfg whoami --json` and `cfg status --json`.
   - In `whoami`, check `identity_mode`, `identity.authenticated`, and `identity_display`. A short fingerprint such as `#abc12` is display-only, not proof.

2. Inspect before mutation.
   - `cfg diff <record> =HEAD =live --json`
   - `cfg log <record> --json`
   - `cfg impact <record> =HEAD =live --json`
   - Scoped LLM review, when explicitly needed: `cfg impact <record> =HEAD =live --against <related-record> --llm --json`

3. Mutate through cfgit.
   - Write the full target document to a temp JSON file.
   - Run `cfg commit <record> --from <file> -m "<message>" --json`.
   - For a coupled multi-record change, write a batch JSON file and run `cfg commit --bulk-from <file> -m "<message>" --json`. The batch file can be `[{"record":"collection:id","doc":{...}}]` or `{"collection:id": {...}}`.
   - If commit returns `changed_outside_cfgit`, stop and inspect drift.
   - If bulk commit returns `blocked`, no record was applied; inspect `failed`. If it returns `partial`, some records were applied before a race/failure; inspect `results`, `failed`, and `pending` before continuing.
   - If the result is `identity_required` or `forbidden`, do not retry with another author's string. Ask the user to provide the correct token/DB principal or change permissions.

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
- `cfg_doctor`
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
- `cfg_whoami`
- `cfg_init`
- `cfg_identity_hash` for setup only. Prefer the local CLI for real tokens because MCP clients may log tool inputs.

Every MCP tool returns the same envelope shape as the CLI exit status: `status`, `code`, `message`, `data`.
For MCP bulk commits, pass `items` as structured JSON (`[{record, doc}]`) when the client supports it; use a JSON string only when the client cannot send nested objects cleanly.
