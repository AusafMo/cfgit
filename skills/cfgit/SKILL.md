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
- At the start of a cfgit session, call `cfg_check_update` (or `cfg check-update --json`). If `data.update_available` is true and `data.snoozed` is false, tell the user the new version, show `data.notes` (what's new), and offer to upgrade (`pip install -U cfgit`) or snooze — call `cfg_check_update` with `snooze_days=30` if they want to be reminded later. NEVER upgrade for them. It is best-effort and silent when there is nothing to say; skip if `data.disabled` (they set `CFGIT_NO_UPDATE_CHECK`).
- Prefer `--json` for agent parsing. Output is otherwise human-on-a-TTY / JSON-when-piped; `CFG_OUTPUT=json|human|auto` overrides. To avoid repeating flags, `CFG_ENV`, `CFG_CONFIG`, and `CFG_AUTHOR` set session defaults, and cfgit discovers `.cfg.toml` by walking up from the cwd.
- Run `cfg doctor --status` to confirm where you are pointed (config, env, target db, identity mode + whether verified, reachability, tracked/drift counts) before mutating an unfamiliar env. It warns if a `needs_approval` env is still in `open` identity mode — writes there succeed UNAUDITED; surface that to the user.
- Use deterministic `cfg impact` first. Add `--llm` only when the user asks for LLM narration and the impact plugin is installed. Use `--against <collection:id>` when the user wants narration scoped to specific related records instead of the whole system.
- Before the FIRST `cfg import` against a new database or `.cfg.toml`, run `cfg doctor` (read-only). It reports every secret-deny match, oversized field, and key issue at once, with paste-ready `secret_fields`/`ignore_fields` snippets. Fix `.cfg.toml` from its output, then import. This avoids import failing one secret at a time.
- In `authenticated` or `enforced` identity mode, the process must already have a verified identity source, usually `CFGIT_IDENTITY_TOKEN` or a per-user DB principal. `--author` and MCP `author` are hints only; cfgit rejects them if they do not match the verified identity.
- Never paste raw human identity tokens into prompts, logs, commits, or history. For real setup secrets, prefer local CLI hashing: `printf '%s' '<private-token>' | cfg identity-hash --stdin`.
- If `cfg log`, `cfg show`, or the MCP envelope returns `bad_config` saying history exists under another env, stop and switch to the env name that wrote that history or fix `.cfg.toml`; do not treat an empty history rail as proof that the record was never committed.

## Setup from scratch

When the user has a datastore but no `.cfg.toml` yet, help them go from zero to a working (and, for shared/production stores, secured) setup. Do not invent secrets or run destructive commands; propose the config and the exact commands, and let the user supply URIs and tokens.

1. Write `.cfg.toml` at the repo/working root. Ask which datastore (`mongo`/`postgres`), which collections/tables to version, and the stable id field per collection. Put the connection string in an env var, never inline: `uri = "env:DEV_MONGODB_URI"`. Start with one `[env.<name>]`; add `prod` later. Minimal shape is in `docs/CONFIGURATION.md`.
2. Choose the identity mode for each env (`docs/IDENTITY_AND_ATTRIBUTION.md`):
   - `open` — no verified identity; fine for a local/dev store you own. `--author`/`CFG_AUTHOR` is taken at face value. A `needs_approval` env in `open` mode writes UNAUDITED — say so.
   - `authenticated` / `enforced` — for shared or production stores. Requires a verified identity source before any write.
3. For `authenticated`/`enforced` with token identity, set it up without leaking the secret:
   - The user picks a private token string. Hash it locally: `printf '%s' '<private-token>' | cfg identity-hash --stdin` (never put the raw token in a prompt, arg, or history).
   - Add the returned hash under the env: `tokens = [{ author = "alice@example.com", name = "alice-main", sha256 = "sha256:..." }]`, with `sources = ["token"]` and `token_env = "CFGIT_IDENTITY_TOKEN"`.
   - At runtime the process must export `CFGIT_IDENTITY_TOKEN='<the raw token>'`. Alternatively use `db_principal` identity (map DB users to authors) when each person already has their own DB credential — often cleaner than tokens.
4. Set permissions for shared stores: `[env.<name>.permissions] mode = "restricted"`, with `admins`/`writers` author globs and `admin_actions` for system-wide ops. In `open` permission mode anyone the identity layer accepts can write.
5. Initialize and verify, then first import:
   - `cfg init` (creates history/heads, and branch refs if `[branches] enabled = true`).
   - `cfg doctor --status --json` to confirm where you are pointed (config, env, target db, identity mode + whether verified, reachability). It warns if a `needs_approval` env is still in `open` identity mode.
   - `cfg doctor --json` (read-only) BEFORE the first import — apply its `secret_fields`/`ignore_fields` suggestions to `.cfg.toml` and re-run until `ok`.
   - `cfg import --all -m "initial import" --json`, then `cfg status --json`.
6. For a shared store, confirm identity is actually verified before real writes: `cfg whoami --json` → check `identity_mode` and `identity.authenticated`. A display fingerprint like `#abc12` is NOT proof.

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
   - For a coupled multi-record change, write a batch JSON file and run `cfg commit --bulk-from <file> -m "<message>" --json`. The batch file can be `[{"record":"collection:id","doc":{...}}]` or `{"collection:id": {...}}`. Preview a collection-scale batch first with `cfg commit --bulk-from <file> --dry-run --json` — it reports each record's `would_commit`/`noop`/`changed_outside_cfgit` and writes nothing.
   - Before a risky bulk change, snapshot the current live docs with `cfg export --out backup.json --json` (a portable, re-importable artifact stamped with head seq/oid). If export prints a size warning, the config may be pointed at a data-plane collection (events/content/jobs) cfgit is not built to version — flag it. To roll back, `cfg import --from backup.json -m "<message>" --json` writes them back through the drift-guarded path (preview with `--dry-run`). If it returns `partial` with `cancelled: true` (the user interrupted), already-applied records are durable and rerunning the same command resumes. For an in-tool rollback with no file, use `cfg tag` + `cfg restore --tag` instead.
   - For a draft branch, use `cfg --branch <branch> commit <record> --from <file> -m "<message>" --json` or `cfg --branch <branch> commit --bulk-from <file> -m "<message>" --json`. This writes cfgit refs only; runtime is unchanged.
   - If commit returns `changed_outside_cfgit`, stop and inspect drift.
   - If bulk commit returns `blocked`, no record was applied; inspect `failed`. If it returns `partial`, some records were applied before a race/failure; inspect `results`, `failed`, and `pending` before continuing.
   - If the result is `identity_required` or `forbidden`, do not retry with another author's string. Ask the user to provide the correct token/DB principal or change permissions.

4. Reconcile drift.
   - `cfg diff <record> =HEAD =live --json`
   - `cfg adopt <record> -m "<message>" --json`
   - If `cfg doctor --status` reports a high drift ratio (many records `changed_outside_cfgit`), baseline the whole collection once with `cfg adopt --all -m "<message>" --json` so later edits and restore work without a per-record adopt.

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
   - `cfg pr merge <pr-id> --json` only after review. If merge returns `stale`, `changed_outside_cfgit`, or `atomicity_unavailable`, do not retry blindly; inspect current main/live drift or adapter capability first.

## MCP Tools

If the cfgit MCP server is available, prefer its tools over shelling out:

- `cfg_status`
- `cfg_doctor`
- `cfg_diff`
- `cfg_impact`
- `cfg_commit`
- `cfg_bulk_commit`
- `cfg_set` for a few scalar field edits (routes through the drift-guarded commit path)
- `cfg_export`
- `cfg_import` (pass `from_export` to restore a snapshot; `dry_run` to preview)
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
- `cfg_check_update` — check PyPI for a newer cfgit; pass `snooze_days` to snooze. Never upgrades.

Every MCP tool returns the same envelope: `status`, `code`, `message`, `data`, plus top-level `state` and `next`. `state` echoes the outcome's terminal state (e.g. `changed_outside_cfgit`, `noop`, `blocked`, `would_commit`); `next` is a self-teaching remedy `{why, remedy, commands}` when the outcome needs one, else null. Branch on `state` and follow `next.commands` on a refusal instead of guessing.
For MCP bulk commits, pass `items` as structured JSON (`[{record, doc}]`) when the client supports it; use a JSON string only when the client cannot send nested objects cleanly.
For MCP PR merges, `cfg_pr_merge` has the same semantics as `cfg pr merge`: it is the only branch/PR mutation path, multi-record merges are batch-atomic when supported, and `atomicity_unavailable` means no runtime records were changed.
For MCP history/env mismatches, branch on `status="error"` and `code=6`; surface the `message` to the user and do not continue mutation.
