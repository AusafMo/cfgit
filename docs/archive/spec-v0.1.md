# `cfg`: Config Version Control: Full Specification

Version 0.1 (build-ready draft). A git-shaped version-control tool for **database-resident config documents** (LLM agent configs first; any JSON-shaped config doc generally). Storage-agnostic core; Mongo is the first backend.

---

## 0. Glossary (precise terms used throughout)

- **Config doc**: one versioned unit: a JSON object identified by a `config_id` (e.g. `agent_planner`). Lives in the **runtime store** as its *current* value.
- **Runtime store**: the live DB the application reads at runtime (e.g. Mongo `agent_configs`). Holds **only current** docs. Knows nothing about versioning. Untouched by `cfg` except via `put_config`.
- **History store**: the tool-owned record of every version ever (e.g. Mongo `config_history`). The source of truth for versioning. The application never reads it.
- **Version / entry**: one immutable record in the history store representing the state of one config doc at one moment.
- **hash**: content hash of a config doc; the stable id of a version (git-blob analog).
- **seq**: monotonically increasing integer per `config_id` (human-friendly version number; `planner@7`).
- **HEAD(config_id)**: the latest committed version of a config (highest `seq`). Analogous to git HEAD.
- **live(config_id)**: what the *runtime store* currently holds for a config. May differ from HEAD if someone bypassed the tool (= **dirty**).
- **as-of T**: the reconstructed system (or single-config) state at timestamp T, derived by query, not stored.
- **Adapter**: a class implementing `StorageAdapter` for one DB technology. The only DB-specific code.

---

## 1. Architecture: three layers, hard boundaries

```
┌──────────────────────────────────────────────────────────────┐
│  INTERFACES (3 doors, one engine)                             │
│   • Porcelain CLI   `cfg <verb>`         (humans)             │
│   • Plumbing/JSON   `cfg <verb> --json`  (scripts/agents)     │
│   • Agent surface   Claude skill + MCP server (Claude/Codex)  │
├──────────────────────────────────────────────────────────────┤
│  CORE ENGINE (pure logic, NO db imports)                      │
│   commit · log · diff · show · status · restore · tag · etc.  │
│   hashing · as-of reconstruction · dirty detection            │
│   depends ONLY on the StorageAdapter interface                │
├──────────────────────────────────────────────────────────────┤
│  STORAGE ADAPTER (interface)                                  │
│   MongoAdapter (now) · PostgresAdapter (later) · …            │
└──────────────────────────────────────────────────────────────┘
```

**Inviolable rules:**
1. Core imports no DB driver. It imports only `StorageAdapter`. (Enforce in CI: grep the core package for `pymongo|psycopg|sqlalchemy` → fail.)
2. All three interfaces call the **same** core functions. The CLI is a thin arg-parser; the MCP server is a thin RPC wrapper. No business logic in any interface.
3. The history schema (Section 3) is DB-neutral. Every adapter stores the same logical fields.

---

## 2. The StorageAdapter interface (the DB seam)

Language-neutral contract. Mongo first; Postgres/any later by writing one class. **No core change** to add a backend.

```python
class StorageAdapter(Protocol):
    # ── runtime store (current docs the app reads) ──
    def get_config(self, config_id: str) -> dict | None: ...
        # current runtime doc, or None if absent.
    def put_config(self, config_id: str, doc: dict) -> None: ...
        # overwrite the runtime doc. The "apply".

    def list_config_ids(self) -> list[str]: ...
        # all config_ids present in the runtime store (for `status`, `--as-of` over all).

    # ── history store (versions) ──
    def append_history(self, entry: dict) -> None: ...
        # insert ONE immutable history entry (schema §3). Never updates an existing entry
        # EXCEPT the git_sha backfill (see set_git_sha).
    def query_history(
        self, *,
        config_id: str | None = None,   # None = across all configs
        as_of: datetime | None = None,  # latest entry per config with ts <= as_of
        tag: str | None = None,         # entries carrying this tag
        ref: str | None = None,         # a specific version: "planner@7" or a hash
        limit: int | None = None,
        order: str = "desc",            # by (config_id, seq)
    ) -> list[dict]: ...
    def get_head(self, config_id: str) -> dict | None: ...
        # the highest-seq history entry for a config (HEAD). None if never committed.
    def next_seq(self, config_id: str) -> int: ...
        # atomic; returns get_head().seq + 1 (or 1).

    # ── linkage + labels ──
    def set_git_sha(self, version_hash: str, git_sha: str) -> bool: ...
        # backfill git_sha onto an existing entry (post-commit hook). Idempotent. True if set.
    def add_tag(self, version_hash: str, tag: str) -> None: ...
    def remove_tag(self, version_hash: str, tag: str) -> None: ...
    def list_tags(self) -> list[dict]: ...   # [{tag, config_id, hash, ts}]

    # ── atomicity ──
    def commit_apply(self, *, history_entry: dict, new_doc: dict, config_id: str) -> None: ...
        # ATOMIC where the backend supports it: append_history(history_entry) AND
        # put_config(config_id, new_doc) succeed together or neither does.
        # Mongo: multi-doc txn (replica set) or best-effort+compensation if standalone.
        # Postgres: single SQL txn.

    # ── meta ──
    def ensure_schema(self) -> None: ...     # create collections/tables + indexes (idempotent).
    def backend_name(self) -> str: ...       # "mongo" | "postgres" | ...
```

**Index requirements every adapter MUST create (`ensure_schema`):**
- history: unique `(config_id, seq)`; index `(config_id, ts)`; index `hash` (unique); index `tags` (multikey/GIN); index `git_sha`.
- runtime: whatever the app already has (adapter does not impose).

---

## 3. History entry schema (DB-neutral, exact)

One entry = the state of ONE config at ONE moment. **Immutable** after write (except `git_sha` backfill + `tags` mutation).

```jsonc
{
  "_id":         "<storage pk>",          // adapter-managed
  "config_id":   "agent_planner",         // REQUIRED. which config.
  "seq":         7,                        // REQUIRED. monotonic per config_id, starts at 1.
  "hash":        "sha256:16hex",           // REQUIRED. canonical content hash of `doc` (§4). UNIQUE.
  "parent_hash": "sha256:16hex | null",    // hash of the prior version (seq-1). null for seq 1.
  "doc":         { /* full config doc */ },// REQUIRED. FULL snapshot. exactly what put_config writes.
  "message":     "bumped planner multi-turn", // REQUIRED (non-empty). the "why".
  "author": "developer",                  // REQUIRED. resolved identity (§9).
  "ts":          "2026-06-21T10:30:00Z",   // REQUIRED. UTC ISO-8601. server-trusted, not client.
  "op":          "commit",                 // REQUIRED. enum: commit | restore | import | revert
  "git_sha":     "def456… | null",         // code commit live at this moment. backfilled by hook.
  "tags":        ["june7-good"],           // human labels. mutable.
  "meta": {                                 // optional, open.
    "restored_from": "planner@5",           // set when op=restore: which version this re-applied
    "tool_version": "cfg/0.1.0",
    "hostname": "dev-laptop"
  }
}
```

**Field rules:**
- `op=commit` → a normal new version (user edited, applied).
- `op=restore` → re-applying an OLD doc as a NEW HEAD version (non-destructive rollback). `doc` = the restored doc; `meta.restored_from` set; `seq` is still the next seq (history only moves forward: like `git revert`, never rewrites).
- `op=import` → first-time ingestion of an already-live doc that predates the tool (the migration; §11). `parent_hash=null`, `message="import baseline"`.
- `seq` is **per config_id**, gapless, assigned via `next_seq` atomically.
- **No diffs stored.** Full docs. (Justification: ~43 configs × few edits/week = trivial volume; restore = copy; zero replay risk. Packing/compaction is a future optimization, never a correctness concern.)

---

## 4. Canonical content hashing (exact algorithm)

`hash(doc)` must be **stable, order-independent, and exclude runtime-owned noise** so that "same meaningful content ⇒ same hash" (idempotent commits, honest dirty-detection).

Algorithm:
1. **Strip ignored keys** from a deep copy of `doc` before hashing. Ignored = runtime-managed/volatile fields that the tool does not own and that the app mutates out-of-band. Configurable per project (Section 8 `ignore_fields`). Defaults for our agent_configs: `_id`, `metrics`, `updated_at`, `updated_by`, `created_at`, `created_by`, and any key matching `instructions_backup_*` (the legacy cruft: never versioned, never hashed).
2. **Canonical-JSON serialize** the stripped doc: keys sorted recursively, UTF-8, no insignificant whitespace, numbers in a fixed normal form (ints as ints; floats via shortest round-trip repr; reject NaN/Inf). Arrays preserve order (order is semantic for `tools`, etc.).
3. `sha256` the canonical bytes; take a stable prefix. Store as `"sha256:<first 16 hex>"`. (16 hex = 64 bits; collision-safe at this scale; full digest retained internally if ever needed.)

**Why strip before hash:** otherwise `metrics.total_invocations` ticking up would make every config look "dirty" forever and every commit produce a new hash for no real change. Stripping makes hash track *meaningful* content only.

`live == HEAD` test (dirty detection) = `hash(strip(live_doc)) == HEAD.hash`.

---

## 5. The command surface (porcelain): every command, exhaustively

Global form: `cfg [global-flags] <verb> [args] [flags]`

**Global flags (apply to all verbs):**
- `--env <name>`: which environment/profile (dev|prod|preview|…); selects the adapter + connection (Section 8). Default: `CFG_ENV` env var, else `dev`.
- `--json`: emit machine-readable JSON to stdout instead of human text. (Plumbing mode.)
- `--quiet` / `-q`: suppress non-essential human output.
- `--yes` / `-y`: assume "yes" to confirmation prompts (for scripts).
- `--config-file <path>`: path to the `cfg` project config (Section 8). Default: walk up for `.cfg.toml`.

**Exit codes (uniform, agent-critical):**
- `0` success / clean.
- `1` generic error (bad args, not found).
- `2` **dirty / conflict** (status found drift; commit base is stale). Distinct so agents branch on it.
- `3` connection/storage error.
- `4` confirmation declined.

---

### 5.1 `cfg init`
Initialize `cfg` for a project/env: create history collection/tables + indexes via `adapter.ensure_schema()`, write `.cfg.toml` if absent.
```
cfg init --env dev
```
Output: backend name, collections/tables ensured, indexes created. Idempotent.

---

### 5.2 `cfg import` (one-time baseline)
Ingest already-live runtime docs as `seq=1` `op=import` baselines so history starts from reality.
```
cfg import                       # import ALL runtime config_ids not yet in history
cfg import agent_planner         # just one
cfg import --strip-backups       # ALSO remove instructions_backup_* keys from the runtime doc on import (cleanup)
```
Behavior per config: if no history exists → snapshot current live doc as `seq=1, op=import, parent_hash=null, message="import baseline"`. If history exists → skip (idempotent), warn.
`--strip-backups`: additionally `put_config` a cleaned doc (backup keys removed) AND record that as the baseline. **Confirm required** (mutates runtime). The 15 legacy `instructions_backup_*` keys die here, preserved forever in nothing-needed because the import IS the first version.

---

### 5.3 `cfg edit` (stage a change)
Pull live doc → temp file → `$EDITOR` → on save, stage it (write to a local staging area, NOT the DB).
```
cfg edit agent_planner           # opens the doc; large `instructions` as a readable block
```
- Opens the **stripped** doc (no `_id`/metrics/backup noise) so the editor view is clean.
- On save: validates JSON; stores staged doc in `.cfg/staged/<config_id>.json`. Nothing hits the DB yet.
- `--from <file>` skips the editor and stages the given file.
- `cfg edit --abort <config_id>` discards staging.

(Rationale: separates "prepare a change" from "apply it": like git's working tree vs commit. Lets you review with `cfg diff` before `commit`.)

---

### 5.4 `cfg set` (field-level quick edit)
For tiny changes without opening an editor.
```
cfg set agent_planner client_config.temperature 0.6 -m "lower temp"
cfg set agent_planner tools+= run_validator -m "grant validator"     # array append
cfg set agent_planner tools-= old_tool -m "drop tool"                # array remove
cfg set agent_planner some.key --delete -m "remove key"
```
Dotted path into the doc. Stages + (with `-m`) commits in one step. Without `-m`, only stages (then `cfg commit`). Type inference: `true/false/null/int/float` parsed; quote to force string (`'0.6'`).

---

### 5.5 `cfg commit` (the workhorse)
Apply a staged (or `--from`) new version: snapshot prior → write new → record.
```
cfg commit agent_planner -m "multi-turn fix"
cfg commit agent_planner --from new.json -m "..."
cfg commit --all -m "batch tweak"        # commit every staged config (one entry each)
```
**Algorithm (exact):**
1. Resolve `new_doc` = staged doc for config (or `--from`). Error if nothing staged.
2. `live = adapter.get_config(id)`; `head = adapter.get_head(id)`.
3. **Stale-base / dirty check (optimistic concurrency):**
   - If `head` exists and `hash(strip(live)) != head.hash` → **runtime drifted** (someone bypassed the tool since HEAD). STOP, exit `2`, message: *"runtime for `agent_planner` changed outside cfg (live hash X ≠ HEAD Y). Run `cfg status` / `cfg adopt` first."* Unless `--force` (records the bypass as an `import`-flavored entry first, then proceeds; logs loudly).
   - This is the CAS that kills silent overwrite: your commit refuses if the live state isn't the HEAD you based on.
4. `new_hash = hash(strip(new_doc))`. If `new_hash == head.hash` → **no-op**, exit `0`, message "nothing to commit (identical to HEAD)". (Idempotent.)
5. Build entry: `seq=next_seq(id)`, `hash=new_hash`, `parent_hash=head.hash or null`, `doc=new_doc`, `message`, `author`, `ts=server_now()`, `op="commit"`, `git_sha=null`.
6. `adapter.commit_apply(history_entry=entry, new_doc=new_doc, config_id=id)`: atomic append + apply.
7. Clear staging for that config. Print `agent_planner@7 (sha256:a1b2c3d4)`.

`-m` is REQUIRED (non-empty). No silent commits. `--allow-empty-message` exists only for scripts and is discouraged.

---

### 5.6 `cfg status` (dirty-tree / bypass detector)
For each config: compare `live` vs `HEAD`.
```
cfg status                  # all configs
cfg status agent_planner    # one
```
States per config:
- `clean`: `hash(strip(live)) == HEAD.hash`.
- `DIRTY`: live differs from HEAD (bypass happened) → show short diff summary.
- `staged`: there's a staged edit not yet committed.
- `untracked`: live doc exists but no history (never imported).
- `missing`: HEAD exists but runtime doc gone.
Exit `2` if ANY config is DIRTY (so CI/agents catch drift). `--json` → `[{config_id, state, live_hash, head_hash, head_seq, staged}]`.

---

### 5.7 `cfg adopt` (reconcile a bypass)
When `status` shows DIRTY (someone hand-edited runtime), record reality as a new version so history catches up.
```
cfg adopt agent_planner -m "adopt hand-edit (INC-123 hotfix)"
cfg adopt --all -m "reconcile drift"
```
Snapshots the current `live` doc as a new HEAD entry (`op="import"`, `meta.adopted=true`). After adopt, `status` = clean. (This is how the tool survives the inevitable raw-Mongo edit honestly: it can't prevent the bypass, but `adopt` folds it into history with attribution instead of losing it.)

---

### 5.8 `cfg log`
History views.
```
cfg log agent_planner                 # versions of one config (newest first)
cfg log --all                         # every version of every config (the reflog/safety net)
cfg log --as-of 2026-06-07            # the SYSTEM view: HEAD-as-of-T for every config (one line each)
cfg log agent_planner --as-of 2026-06-07   # what THIS config was at T
cfg log --tag june7-good
cfg log --git <sha>                   # versions linked to a code commit
```
Human columns: `seq  hash  ts  author  op  git_sha?  message`. `--json` → array of entries (sans `doc` unless `--with-doc`). `-n <k>` limit. `--oneline`.

---

### 5.9 `cfg diff`
Compare two versions, or live-vs-HEAD.
```
cfg diff agent_planner @5 @7           # version 5 vs 7
cfg diff agent_planner a1b2c3 9f8e7d   # by hash
cfg diff agent_planner                 # live vs HEAD (your uncommitted/bypass delta)
cfg diff agent_planner @5 live         # version 5 vs current runtime
cfg diff --as-of 2026-06-07 --to now   # SYSTEM diff: every config that changed between June7 and now
```
Output: structured field-level diff (not raw text blob). For the big `instructions` string, default to a **line/section diff** (git-style unified) so it's reviewable; `--semantic` invokes the optional LLM diff (Section 7). `--json` → `{config_id, changes:[{path, op:add|del|mod, before, after}]}`. `--stat` = summary counts only.

---

### 5.10 `cfg show`
Full doc at a version.
```
cfg show agent_planner @7
cfg show agent_planner @7 --field instructions     # just one field
cfg show agent_planner live
```

---

### 5.11 `cfg restore` (single + emergent system)
Re-apply an old state as a new HEAD (non-destructive).
```
# single config
cfg restore agent_planner @5                 # restore one config to version 5
cfg restore agent_planner a1b2c3
cfg restore agent_planner --as-of 2026-06-07 # this config's state at T

# SYSTEM-WIDE (emergent: loops all configs)
cfg restore --as-of 2026-06-07               # ALL configs → their June-7 HEAD
cfg restore --tag june7-good                 # ALL configs → the tagged moment
```
**Modifiers:**
- `--preview`: do NOT touch the target runtime; apply into the **preview env** instead (Section 6). Prints preview details/URL. (requested preview workflow)
- `--dry-run`: compute and print exactly what would change (per config: HEADnow→target), change nothing. Exit `0`.
- `--only a,b,c` / `--except x`: scope a system restore to a subset.
- `--include-code`: also resolve the git SHA for the chosen moment and check out / trigger a code deploy at that SHA (Section 5.14 linkage). Off by default (config-only restore is the common case).

**Algorithm: `restore --as-of T` (the emergent loop), exact:**
1. `ids = adapter.list_config_ids()` (∪ any config that has history but no live doc, optionally).
2. For each `id`: `target = query_history(config_id=id, as_of=T, limit=1, order=desc)` → the latest entry with `ts ≤ T`. If none (config didn't exist at T) → skip (and report "did not exist at T").
3. Build the plan: `[(id, head_now.seq → target.seq, hash delta)]`. If `--dry-run`, print and stop.
4. **Pre-flight dirty check:** if any target config is currently DIRTY vs its HEAD, warn (its live state will be overwritten by the restore; that's usually intended, but surface it). `--force` to proceed without prompt.
5. For each `id` whose `target.hash != head_now.hash`: write `target.doc` to runtime via `commit_apply` with a NEW entry `op="restore"`, `seq=next_seq`, `parent_hash=head_now.hash`, `doc=target.doc`, `meta.restored_from="<id>@<target.seq>"`, `message="restore as-of <T>"` (or user `-m`).
6. Configs already equal to target → skip (no-op, reported).
7. Print summary: N restored, M unchanged, K did-not-exist.

History only moves forward: a restore is a new commit, fully revertible (you can `cfg restore --as-of <yesterday-before-the-restore>` to undo). Nothing is destroyed.

---

### 5.12 `cfg tag` / `cfg tags`
Human labels for a moment.
```
cfg tag june7-good --as-of 2026-06-07         # tag the HEAD-as-of-T of EVERY config with this label
cfg tag planner-good agent_planner @7         # tag a single version
cfg tags                                       # list
cfg tag --delete june7-good
```
A system tag (`--as-of`) attaches the same tag string to the resolved version of each config, so `cfg restore --tag june7-good` rebuilds that exact moment. (Tags decouple humans from juggling timestamps.)

---

### 5.13 `cfg points` (find a moment)
List change-moments to help pick `--as-of`/tag targets.
```
cfg points --around 2026-06-07 [--window 3d]   # entries near a date, all configs
cfg points agent_planner                        # change-moments of one config
```
Shows clustered timeline: `ts · config_id · seq · git_sha? · message`. Lets a human eyeball "the June-7 deploy" and grab its timestamp/tag.

---

### 5.14 Git linkage (code ⇄ config)
For changes that ship WITH code. No custom git flag (git won't allow it). Use a **commit trailer + post-commit hook**.

Author flow:
```
cfg commit agent_planner -m "multi-turn fix"      # → agent_planner@7 (a1b2c3)
git add agent.py
git commit -m "planner multi-turn

Cfg-Version: agent_planner@7"                       # trailer; can list multiple
```
Hook (`.git/hooks/post-commit`, installed by `cfg hooks install`):
1. Read the new commit's message; `git interpret-trailers --parse` to extract all `Cfg-Version:` values.
2. For each `id@seq` (or hash): resolve to a history `hash`, call `adapter.set_git_sha(hash, <new commit sha>)`.
3. Idempotent; silent on no trailer.

Effect: `cfg log --git <sha>` shows configs that shipped with that code; `cfg restore --as-of T --include-code` knows which SHA pairs with the config moment. Config-alone commits leave `git_sha=null` (the majority).

`cfg hooks install` / `cfg hooks uninstall` manage the hook. (Also offer a `prepare-commit-msg` helper that, if a config was committed in the last N minutes, auto-suggests the `Cfg-Version:` trailer.)

---

### 5.15 Misc
- `cfg whoami`: show resolved author identity + env.
- `cfg config`: get/set `.cfg.toml` values.
- `cfg gc`: (future) compact history (e.g. keep full docs but dedup identical blobs by hash). No-op v0.1.
- `cfg version`: tool version.

---

## 6. Preview environment (`--preview`)

`--preview` must target something. Define a **preview profile** in `.cfg.toml`:
```toml
[env.preview]
backend = "mongo"
runtime_uri = "mongodb+srv://…/appdb-preview"   # a SEPARATE preview db
history_uri = "…"                                   # can share history with dev
deploy = { type = "command", run = "scripts/deploy_preview.sh {git_sha}" }  # optional code deploy
```
`cfg restore --as-of T --preview`:
1. Resolves the same plan as a normal restore but targets the **preview runtime** (`put_config` goes to preview db).
2. If `--include-code` and a `deploy` command is configured, runs it with the resolved `{git_sha}` (e.g. spins a preview container at that SHA).
3. Prints: preview db name, configs applied, git_sha used, and (if the deploy command emits one) a preview URL.
Tiers of support:
- **Minimal (v0.1):** preview = "restore into `*-preview` runtime db; you point your local/staging backend at it." No code deploy orchestration.
- **Full (later):** the `deploy` command does a real ephemeral deploy at the SHA.

---

## 7. Optional LLM semantic layer (off by default)

Not core. A pluggable hook + a diff mode.
- `cfg diff … --semantic`: sends `{before, after, + the other configs’ summaries}` to an LLM; returns: nature of change, cross-config contradictions, downstream-agent impact, risk level. Human-readable + `--json` structured.
- `commit.pre_hook = "semantic"` in `.cfg.toml`: on `cfg commit`, runs the semantic check and **warns** (never blocks unless `--strict`). Surfaces "this loosens shot-count rule which the validator assumes."
- Implemented as a `cfg-semantic` plugin so the core has zero LLM dependency. Provider configurable (Claude default).

---

## 8. Project config: `.cfg.toml`

```toml
[project]
name = "example-agent-configs"

[stores]
# logical mapping of config_id → which collection/table holds the runtime doc
runtime_collection = "agent_configs"
history_collection = "config_history"
id_field = "config_id"           # the field that identifies a config in the runtime store

[hash]
ignore_fields = [                # stripped before hashing/diff (runtime-owned noise)
  "_id", "metrics", "updated_at", "updated_by", "created_at", "created_by",
]
ignore_field_patterns = ["instructions_backup_*"]

[identity]
author_from = "git"              # git user.email | env CFG_AUTHOR | os user

[env.dev]
backend = "mongo"
runtime_uri = "env:MONGODB_URI"          # read from env var (never hardcode secrets)
history_uri = "env:MONGODB_URI"
db = "appdb-dev"
gated = false                            # dev = ungated, instant

[env.prod]
backend = "mongo"
runtime_uri = "env:PROD_MONGODB_URI"
db = "appdb"
gated = true                             # prod = requires confirmation / review policy (§10)

[env.preview]
backend = "mongo"
runtime_uri = "env:PREVIEW_MONGODB_URI"
db = "appdb-preview"
```

Secrets always via `env:VAR`, never literals.

---

## 9. Identity / authorship

`author` resolved at commit time, in order: `--author` flag → `CFG_AUTHOR` env → git `user.email` → OS username. Stored verbatim on the entry. (No auth system; trust-based like git, but always attributed: which already beats today's overwritten `updated_by`.)

---

## 10. Environments + gating policy (dev instant, prod controlled)

- **dev** (`gated=false`): `commit`/`restore` apply instantly, no confirmation. Matches today's raw-Mongo speed. This is where 95% of edits happen.
- **prod** (`gated=true`): mutating verbs (`commit`, `restore`, `set`, `adopt`) require explicit confirmation (`--yes` to bypass interactively) and honor an optional **policy**:
  ```toml
  [env.prod.policy]
  require_reason = true          # -m must reference a ticket/INC for prod
  require_clean = true           # refuse prod commit if status is DIRTY (force adopt first)
  break_glass = true             # allow --force with a recorded reason for incidents
  ```
- The *propagation* is still instant in both (no deploy): gating adds a human checkpoint, not latency. Emergency prod edits: `cfg commit … --env prod --force -m "INC-123 break-glass"` records the bypass with reason so the audit trail stays honest.
- **The load-bearing org control (out of tool scope but stated):** to make the history trustworthy, restrict *direct* prod-DB write creds so `cfg` (its service identity) is effectively the only prod writer; devs keep raw access to dev. The tool's `status`/`adopt` are the backstop for the inevitable break-glass.

---

## 11. Migration (cutover from today)

1. `cfg init --env dev` and `--env prod` (creates `config_history` + indexes; runtime untouched).
2. `cfg import --strip-backups` on dev, review, then prod (with confirm): baselines all 43 configs as `seq=1`, removes the 15 legacy `instructions_backup_*` keys (their content is preserved AS the baseline version).
3. `cfg hooks install` in the backend repo.
4. Replace the old `seed_*.py`/raw-Mongo habit: devs now `cfg edit/set` → `cfg commit`. (The existing `seed_agent_planner_shape.py` "owned-fields" logic informs `ignore_fields`/staging.)
5. Optional: lock down prod write creds (Section 10).
No runtime/app change required: the application keeps reading the same runtime collection; it never knows `cfg` exists.

---

## 12. Agent / AI interface (Claude, Codex, others): first-class, not an afterthought

Three mechanisms, increasing structure:

**(a) `--json` on every verb.** Stable, documented JSON schemas (the "plumbing": git's lesson: agents depend on stable output, never the pretty text). Meaningful exit codes (Section 5). An agent can already drive the CLI safely with `--json -y`.

**(b) MCP server (`cfg-mcp`).** Exposes the core as typed tools so ANY agent runtime calls them as first-class tools (no shell-string fragility):
```
cfg.status(env?, config_id?)                         -> [{config_id, state, head_seq, …}]
cfg.log(env?, config_id?, as_of?, tag?, limit?)      -> [entry…]
cfg.diff(env?, config_id, a, b, semantic?)           -> {changes:[…]}
cfg.show(env?, config_id, ref)                        -> {doc}
cfg.commit(env, config_id, doc|patch, message, author?) -> {config_id, seq, hash}
cfg.restore(env, {config_id?|as_of?|tag?}, preview?, dry_run?, only?, include_code?) -> {plan|result}
cfg.tag(env, tag, as_of?|ref?)                        -> {ok}
cfg.points(env, around, window?)                     -> [moment…]
```
Each tool: typed args (JSON-schema validated), structured result, idempotency where applicable, and a `dry_run` on every mutating tool so an agent can preview before applying. Mutating tools on `gated` envs return a "needs confirmation" result unless an explicit `confirm:true` is passed (so an agent can't silently mutate prod).

**(c) Claude skill (`/cfg`).** A SKILL.md that teaches the model the workflow + guardrails, wrapping (b):
- Verbs + when to use them; the dirty-tree rule ("ALWAYS `cfg.status` before `cfg.commit`; if DIRTY, surface and `cfg.adopt` with attribution, don't clobber").
- Safety contract: "NEVER write the runtime DB directly; only via `cfg.*`. `restore` is non-destructive. On prod, require explicit human confirm."
- Canned flows: "reproduce June-7" → `cfg.points(around)` → `cfg.restore(as_of, preview=true)` → report URL → on human ok `cfg.restore(as_of)`. "version a prompt edit" → stage → `cfg.diff` (optional `semantic`) → `cfg.commit`.
- Output etiquette: always show the resulting `config_id@seq (hash)` and the one-line diff stat so the human sees what landed.

The same engine serves all three; the skill/MCP are thin. An agent gets *git-for-configs* as typed tools, with previews and confirmation gates baked in so it's safe to let Claude/Codex operate it autonomously.

---

## 13. Edge cases & failure modes (explicit decisions)

| Case | Decision |
|---|---|
| Commit when runtime drifted from HEAD (bypass) | Refuse, exit 2, tell user to `status`/`adopt`. `--force` records the bypass as a baseline first. |
| Commit identical to HEAD | No-op, exit 0 ("nothing to commit"). |
| Restore a config that didn't exist at T | Skip it, report "did not exist at T"; don't delete the now-existing config (no destructive deletes in v0.1). |
| Config deleted from runtime but has history | `status`=missing. `restore` can re-create it (put_config). |
| Two devs commit same config concurrently | Second `commit_apply` sees a seq/hash collision (unique `(config_id,seq)` + CAS on base hash) → second one fails with "base is stale, re-pull", exit 2. No lost update. |
| `commit_apply` partial failure (history written, runtime not) on a non-txn backend | Adapter must compensate: either retry the runtime write or roll back the history entry; never leave history claiming a state that isn't live. Standalone Mongo: do runtime write first only after history insert succeeds, then if runtime write fails, mark/delete the orphan entry. (Prefer replica-set txn.) |
| Huge `instructions` diff noise from reflow | Default diff is section/line unified; offer `--semantic`. Never block on diff size. |
| Clock skew on `ts` | Server/adapter assigns `ts` (trusted), not the client. |
| Hash collision | 64-bit prefix; on the astronomically unlikely collision, compare full sha256 retained internally. |
| Restore mid-incident with dirty prod | Warn (live will be overwritten); `--force` to skip prompt; the pre-restore live state is still captured (adopt it first or it's recoverable via the just-overwritten config's history if it was committed). |
| Secret in a config doc | Out of scope to encrypt; docs shouldn't hold secrets. `ignore_fields` can exclude a field from history if needed. |

---

## 14. Non-goals (v0.1)

- Branching/merging of configs (git has it; we don't need it for DB configs: linear history per config + restore covers the real workflow; revisit only if a real branching need appears).
- Diff-based storage / packing (full docs; `gc` is future).
- Built-in auth/RBAC (rely on DB creds + gating policy + org cred lockdown).
- Encrypting docs at rest.
- A GUI (CLI + JSON + MCP + skill cover humans and agents).

---

## 15. Build order (ships value incrementally)

1. **Core + MongoAdapter + history schema + hashing + `init`/`import`/`status`/`commit`/`log`/`show`/`diff`.** (Kills silent overwrite via CAS; gives history + diff immediately; replaces backup-key habit.)
2. **`restore` single + `--as-of` emergent + `--dry-run` + `--preview` (minimal) + `tag`/`points`.** (Delivers the rollback flow.)
3. **Git linkage (`hooks install`, trailer parsing, `set_git_sha`).**
4. **Agent surface: `--json` everywhere → MCP server → Claude skill.**
5. **Optional LLM semantic diff plugin.** Full preview deploy. `PostgresAdapter` (proves the seam).

Each stage is independently useful; stop anywhere and you still have a coherent tool.
```
```
