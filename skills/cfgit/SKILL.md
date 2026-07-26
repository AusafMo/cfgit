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
- If `[branches] enabled = true`, draft risky changes on a non-main branch and open a cfgit PR. Branch commits and PR creation do not mutate runtime; `cfg pr merge` is the only branch command that does.
- Prefer `--json` for agent parsing.
- Use deterministic `cfg impact` first. Add `--llm` only when the user asks for LLM narration and the impact plugin is installed. Use `--against <collection:id>` when the user wants narration scoped to specific related records instead of the whole system.
- Before the FIRST `cfg import` against a new database or `.cfg.toml`, run `cfg doctor` (read-only). It reports every secret-deny match, oversized field, and key issue at once, with paste-ready `secret_fields`/`ignore_fields` snippets. Fix `.cfg.toml` from its output, then import. This avoids import failing one secret at a time.
- In `authenticated` or `enforced` identity mode, the process must already have a verified identity source, usually `CFGIT_IDENTITY_TOKEN` or a per-user DB principal. `--author` and MCP `author` are hints only; cfgit rejects them if they do not match the verified identity.
- Never paste raw human identity tokens into prompts, logs, commits, or history. For real setup secrets, prefer local CLI hashing: `printf '%s' '<private-token>' | cfg identity-hash --stdin`.
- If `cfg log`, `cfg show`, or the MCP envelope returns `bad_config` saying history exists under another env, stop and switch to the env name that wrote that history or fix `.cfg.toml`; do not treat an empty history rail as proof that the record was never committed.

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
   - Use MCP `cfg_recent_history` when you need the latest cfgit commits across all configured records before choosing a record.
   - `cfg impact <record> =HEAD =live --json`
   - Scoped LLM review, when explicitly needed: `cfg impact <record> =HEAD =live --against <related-record> --llm --json`
   - If history lookup reports an env mismatch, re-run against the stamped env before making changes.

3. Mutate through cfgit.
   - For a few scalar fields, prefer the fast path — no temp file needed:
     `cfg set <record> field=value other.nested=value -m "<message>" --json`.
     Values are JSON-coerced (`enabled=true`, `n=5`, `tags=["a","b"]`); prefix with `str:` to
     force a string (`version=str:1.0`). `cfg set` fetches the LIVE doc and routes through the
     same commit path, so it refuses on drift exactly like `commit` (it is not a raw write).
     Add `--dry-run` to preview the field-level delta without writing.
   - For a hand edit of the whole doc, `cfg edit <record> -m "<message>"` opens it in $EDITOR.
   - For a large or scripted change, write the full target document to a temp JSON file.
   - Run `cfg commit <record> --from <file> -m "<message>" --json`.
   - Preview any commit before writing with `cfg commit <record> --from <file> --dry-run --json`
     (returns `would_commit` with the delta, or `changed_outside_cfgit`/`noop`; writes nothing).
   - Every outcome now carries a top-level `next` block (why + remedy + copy-paste `commands`);
     on a refusal, follow `next.commands` rather than guessing.
   - For a coupled multi-record change, write a batch JSON file and run `cfg commit --bulk-from <file> -m "<message>" --json`. The batch file can be `[{"record":"collection:id","doc":{...}}]` or `{"collection:id": {...}}`.
   - For a draft branch, use `cfg --branch <branch> commit <record> --from <file> -m "<message>" --json` or `cfg --branch <branch> commit --bulk-from <file> -m "<message>" --json`. This writes cfgit refs only; runtime is unchanged.
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

6. Branch / PR flow when enabled.
   - `cfg branch list --json`
   - `cfg branch create <name> --from main --json`
   - `cfg --branch <name> commit <record> --from <file> -m "<message>" --json`
   - `cfg diff main..<name> --json`
   - `cfg pr create --base main --head <name> -m "<message>" --json`
   - `cfg pr merge <pr-id> --json` only after review. If merge returns `stale` or `changed_outside_cfgit`, do not retry blindly; inspect current main/live drift first.

## MCP Tools

If the cfgit MCP server is available, prefer its tools over shelling out:

- `cfg_status`
- `cfg_doctor`
- `cfg_diff`
- `cfg_impact`
- `cfg_commit`
- `cfg_bulk_commit`
- `cfg_branch_list`
- `cfg_branch_create`
- `cfg_branch_delete`
- `cfg_branch_diff`
- `cfg_branch_log`
- `cfg_pr_create`
- `cfg_pr_list`
- `cfg_pr_show`
- `cfg_pr_close`
- `cfg_pr_merge`
- `cfg_recent_history`
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
For MCP history/env mismatches, branch on `status="error"` and `code=6`; surface the `message` to the user and do not continue mutation.
