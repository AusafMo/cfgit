# `cfg`: Version Control Engine: Specification v0.3.2

> **Read `docs/SPEC_CORE.md` first.** It owns the project framing (non-custodial version control for live datastores: git that doesn't make you move in), the comparison to Dolt/TerminusDB/Langfuse, and the **v1 MVP scope**. THIS doc is the deep engine reference: the exact adapter contract, atomic apply, bitemporal model, hashing, and the parts SPEC_CORE **defers** (approval/gating §10-§11, agent/MCP surface §12, the config-specific LLM impact layer §7). Where the two differ on scope, **SPEC_CORE wins for v1**; build the subset SPEC_CORE §8 lists, using the detail here. The "config documents" framing below predates the non-custodial reframing and is retained as the worked example (the origin case), not the definition.

Storage-agnostic version-control engine for **opaque JSON records keyed by a stable id**, versioned *in place* beside a live datastore. Mongo + Postgres at MVP. Driven by **humans (CLI)**; the agent (MCP + skill) surface is deferred.

> v0.3.2 supersedes v0.3.1. v0.2 folded in the first adversarial review (**[FIX-N]**, §16); v0.3 the second review's write-path defects (**[V3-N]**, §18); v0.3.1 the third review's residual edges (**[V3.1-N]**, §20); v0.3.2 the single Tier-1 item the exit-gate found (`strip_on_store` omitted from the field-set invariant + hash strip). **Exit-gate status: GREEN**: four review rounds; the build-ready verdict is recorded in §20. The CAS, dual-CAS, approval/agent-safety, read-side keying, redact-vs-oid, and valid-time model were verified to hold across all rounds.

> **How to read this doc.** **Part 1 (Using cfg)** is plain language for the people and agents who USE the tool: start here. **Part 2 (Internals)** is the precise technical spec for whoever BUILDS it. The user-facing names in Part 1 (config keys, commands, statuses) are the real ones; Part 2's technical terms (`oid`, `valid_from`, `StorageAdapter`, the CAS) are engine internals a user never has to know.

---

# PART 1: Using cfg (plain language)

## What cfg does, in one breath
Your AI agent configs live in a database, and the app reads them live, so editing one changes the agent right away. cfg gives those configs the things git gives code: a **history** you can look back through, a **safe save** that warns you instead of overwriting someone else's change, an easy **roll back** to how things were on any past date, and on production, a **human has to approve** before anything changes. Both people (typing commands) and AI agents (Claude/Codex) use the same tool.

## The everyday commands
You rarely need more than these. (Full list with every flag is in Part 2 §5.)

- `cfg status`: what changed, and whether anything was edited outside cfg.
- `cfg log <config>`: the history of a config: who changed it, when, why.
- `cfg diff <config>`: show what's different between two versions (or between the saved version and what's live now).
- `cfg edit <config>`: open a config in your editor, change it, save it.
- `cfg set <config> <field> <value>`: change one field without opening an editor.
- `cfg commit <config> -m "why"`: save your change as a new version. On dev it's instant. On prod it asks a human to approve first.
- `cfg restore <config> <version>`: put a config back to an earlier version. Nothing is lost; a restore is just a new version on top.
- `cfg restore --tag june7-good`: put EVERY config back to a saved moment (this is the "get us back to the 7th of June" button).
- `cfg tag <name>`: bookmark the current state of everything so you can return to it later by name.
- `cfg impact <config>`: before you save, see what a change would affect: which other configs depend on it, and what might break.

## How a roll-back-everything works (the "June 7" case)
1. `cfg points --around june7`: list the change-moments around that date.
2. Pick one, bookmark it: `cfg tag june7-good`.
3. Preview it safely first: `cfg restore --tag june7-good --preview` (writes to a preview copy, not prod).
4. Happy with it? On prod, a human approves, and `cfg restore --tag june7-good` puts everything back.

## If you're an AI agent (Claude / Codex), read this
- You drive cfg through its tools (the `cfg.*` MCP tools), and every result tells you a plain **status**: `ok`, `changed_outside_cfg`, `conflict`, `needs_human_ok`, `was_declined`, `not_found`, `bad_config`, or `error`. Branch on that word, not on guesswork.
- **You can look at and propose anything. You cannot push to production by yourself.** Any production change comes back as `needs_human_ok` with a plan and an approval id; a real person approves it on a separate channel. There is no flag you can set to skip that, and there is no "approve" tool for you: by design.
- Before you save a config, call `cfg.impact` and read the consequences. If it says a change is **breaking** because other configs depend on it, fix those or ask the human, don't ship it silently.
- The safe default for you is **preview + dry-run**. Show the human the preview, let them approve, then apply.
- Golden rule: never write the database directly. Only use `cfg.*`. A restore never destroys anything; a "save" that would clobber someone else returns `conflict`: re-pull and try again, don't force it.

## Setting it up (your `.cfg.toml`)
You describe your setup once in a `.cfg.toml` file. The keys are named so they read in plain English. Here is the whole thing for our project, with what each part means:

```toml
[project]
name = "example-agent-configs"

[storage]                               # where your configs live
live_collection = "agent_configs"       # the collection the app reads
id_field        = "config_id"           # the field that names each config
live_when       = { is_active = true }  # how to pick the live one if there are several with the same name
# (cfg keeps its own history in two private collections; you usually never touch these)
history_collection = "config_history"
heads_collection   = "config_heads"

[versioning]                            # what counts as a real change vs noise
ignore_fields   = ["_id","metrics","updated_at","updated_by","created_at","created_by","is_active"]
ignore_patterns = ["instructions_backup_*"]   # old hand-made backup keys: ignore them
secret_fields   = []                          # fields with secrets: never stored in history, never sent anywhere

[secrets]                               # stop a secret from being saved by accident
block_fields = ["*_key","*_secret","*_token","*api_key*","*password*"]
block_values = ["sk-[A-Za-z0-9]{20,}","AKIA[0-9A-Z]{16}"]
on_match     = "refuse"                 # refuse to save | warn ; override needs --allow-secret (logged)

[author]
from = "git"                            # who gets recorded as the author (your git email)

[connections]                           # ONLY needed for `cfg impact`: how one config depends on another
enabled       = false                   # the structural part still works locally even when off
share_with_ai = []                      # names of configs whose text may be sent to the AI for the "why" explanation
ai_provider   = "claude"
warn_level    = "none"                  # none | breaking: when to flag a change in a pre-save check
# "links" tells cfg which fields connect configs, so it can tell you what a change ripples into:
links = [
  { field = "phase_contract",   means = "a contract other configs rely on" },
  { field = "tools",            means = "a tool several configs share" },
  { field = "prompt_templates", means = "a template referenced across configs" },
  { field = "fallback_models",  means = "a model another config falls back to" },
  { field = "skills",           means = "a skill several configs share" },
]

[code_search]                           # where to grep when cfg checks if old keys are still used in code
roots = ["../backend-repo/app"]

# Each environment: which database, and whether changes need human approval.
[env.dev]
database = "mongo"
uri = "env:DEV_MONGODB_URI"
db = "appdb-dev"
needs_approval = false
[env.prod]
database = "mongo"
uri = "env:PROD_MONGODB_URI"
db = "appdb"
needs_approval = true
[env.prod.rules]
require_reason     = true    # every prod change must say why (a ticket/incident)
require_clean      = true    # refuse if something was edited outside cfg; clean it up first
emergency_override = false   # the break-glass switch, off by default
confirm_word       = "prod"  # a human must type this word to approve
[env.preview]
database = "mongo"
uri = "env:PREVIEW_MONGODB_URI"
db = "appdb-preview"
needs_approval = false
```

That's everything a user needs. The rest of this document (Part 2) is for building the tool.

> **Naming note for the implementer:** Part 1's friendly config keys map 1:1 onto Part 2's technical fields: `live_collection`=`runtime_collection`, `live_when`=`runtime_filter`, `ignore_patterns`=`ignore_globs`, `secret_fields`=`strip_on_store`, `[connections].links`=`[impact].edge_fields`, `share_with_ai`=`[impact].allow`, `warn_level`=`block_on`, `needs_approval`=`gated`, `confirm_word`=`affirm_phrase`, `[env].database`=`backend`. The plain **statuses** map too: `changed_outside_cfg`=`dirty`, `needs_human_ok`=`needs_approval`, `was_declined`=`declined`, `bad_config`=`invariant_violation`. The loader reads the friendly keys; internals keep the technical names. Keep this mapping authoritative in one place (the config loader).

---

# PART 2: Internals (technical specification)

> Everything below is the precise build contract. Terms here (`oid`, `recorded_at`/`valid_from`, `StorageAdapter`, CAS, the `apply()` algorithm) are engine internals; users interact only with Part 1's names.

## 0. Glossary

- **Config doc**: one versioned unit: a JSON object identified by a `config_id` (e.g. `agent_planner`) **within an environment**. Lives in the **runtime store** as its current value.
- **Config selector**: **[FIX-15]** the runtime store may hold MULTIPLE docs sharing a `config_id` (e.g. our `agent_configs` keys on `(config_id, is_active)`). The *versioned* doc is the one matching the project's `runtime_filter` (default `{is_active: true}`). "The config doc" always means "the doc for `config_id` satisfying `runtime_filter`." There is exactly one such doc per `config_id` per env (enforced; see §2.7).
- **Runtime store**: the live DB the app reads (e.g. Mongo `agent_configs`). Holds current docs only. Untouched by `cfg` except `put_config`.
- **History store**: tool-owned record of every version. Source of truth for versioning. The app never reads it.
- **Entry / version**: one immutable record = the state of one config at one logical moment.
- **oid**: **[FIX-10]** the full sha256 of an entry's canonical content; the entry's identity and FK value. A 12-hex **short oid** is a display/lookup convenience that resolves to a unique full oid (git-style).
- **seq**: monotonic integer per `config_id` per env; a human label (`planner@7`). **[FIX-2]** NOT guaranteed gapless.
- **HEAD(config_id)**: the entry the history store currently designates as current for a config (tracked by an explicit HEAD pointer, §3.2). Not "max seq."
- **live(config_id)**: what the runtime store currently holds (the `runtime_filter` doc). May differ from HEAD ⇒ **dirty** (a bypass happened).
- **recorded_at**: **[FIX-4]** transaction time: when `cfg` wrote the entry. Trusted (assigned by the history store, not the client).
- **valid_from**: **[FIX-4]** valid time: when this doc became the live value in reality. For `commit`: = recorded_at. For `restore`: = recorded_at (the restored value becomes live now). For `import`: the operator MAY supply the real historical time; defaults to recorded_at with a `valid_from_estimated: true` flag.
- **as-of T**: **[FIX-4]** reconstructed state at time T. Two precise, separately-named queries (§5.8): **`--as-of-recorded T`** (what cfg had recorded by T: transaction time) and **`--as-of-valid T`** (what was actually live at wall-clock T: valid time). The CLI default `--as-of` = valid-time, the one humans mean.
- **Adapter**: a class implementing `StorageAdapter` for one DB. Only DB-specific code.

---

## 1. Architecture: three layers, hard boundaries

```
INTERFACES: Porcelain CLI `cfg <verb>` (humans)
             Plumbing JSON  `cfg <verb> --json` (scripts/agents)
             Agent surface  MCP server + Claude skill (Claude/Codex)
CORE ENGINE: commit·log·diff·show·status·adopt·restore·tag·redact·…
             hashing · as-of reconstruction · dirty detection · approval flow
             depends ONLY on StorageAdapter + ApprovalProvider
STORAGE:    MongoAdapter (now) · PostgresAdapter (later) · …
```

**Inviolable rules:**
1. Core imports no DB driver: only `StorageAdapter`. **CI gate:** grep core package for `pymongo|psycopg|sqlalchemy|motor` → fail.
2. All three interfaces call the same core functions. No business logic in any interface.
3. History schema (§3) is DB-neutral; every adapter stores the same logical fields.
4. **[FIX-1-agent]** A *gated-env mutation* is completed only through `ApprovalProvider` (§11), never by an argument the caller controls.

---

## 2. StorageAdapter (the DB seam)

Language-neutral contract. **[FIX-9]** The `as-of` query is a single grouped query, not an N-loop. **[FIX-1/2/8]** Atomicity + CAS + seq allocation live INSIDE one method.

```python
class StorageAdapter(Protocol):
    # 2.1 runtime store (current docs; identified by config_id + runtime_filter)
    def get_config(self, config_id: str) -> dict | None: ...
        # the single doc matching (config_id AND runtime_filter). None if absent.
        # Raises AmbiguousConfig if >1 doc matches (data error; see §2.7).
    def put_config(self, config_id: str, doc: dict) -> None: ...   # [V3-2, V3.1-1]
        # UPDATES the unique doc matching (config_id AND runtime_filter), in place.
        # `doc` is the FULL effective doc cfg versions (same shape `get_config` returned);
        # cfg never writes a sparse/partial doc here. Identity/_id is never rewritten.
        # The ONLY runtime fields put_config leaves untouched are those in
        # ignore_fields/ignore_paths (which are ALSO not hashed): so post-put
        # oid(strip(live)) always equals the entry's oid and the dirty test cannot trip
        # on a difference cfg itself created. [V3.1-1] (see §4 "Field-set invariant")
        # NEVER inserts. Raises AmbiguousConfig if >1 match, NoSuchConfig if 0 match.
        # Creating the FIRST doc for a config_id is `seed_config` (init/import only), not put_config.
    def seed_config(self, config_id: str, doc: dict) -> None: ...  # [V3-2]
        # Inserts the first runtime doc for a config_id (used only by import of a
        # config that has history but no live doc: e.g. system restore of a deleted
        # config). Refuses if a runtime_filter match already exists.
    def activate_config(self, config_id: str, doc: dict,           # [V3-3]
                        deactivate_filter: dict) -> None: ...
        # Atomically: set the doc matching `deactivate_filter` to is_active=false AND
        # upsert `doc` with is_active=true: the cfg-blessed way to flip the active
        # row when the runtime keys on (config_id, is_active). Leaves exactly one
        # runtime_filter match. Used by `cfg activate` (§5.17). Same-txn where supported.
    def list_config_ids(self) -> list[str]: ...
        # distinct config_ids in the runtime store matching runtime_filter.

    # 2.2 history store: reads
    def get_head(self, config_id: str) -> dict | None: ...   # the HEAD entry (via HEAD pointer §3.2)
    def query_history(self, *, config_id: str|None=None, ref: str|None=None,
                      as_of_recorded: datetime|None=None, as_of_valid: datetime|None=None,
                      tag: str|None=None, git_sha: str|None=None,
                      limit: int|None=None, order: str="desc",
                      with_doc: bool=False) -> list[dict]: ...
        # [FIX-9] When config_id=None AND an as_of_* is given: returns the SINGLE
        # latest qualifying entry PER config_id in ONE backend query
        # (Mongo: $sort+$group first; Postgres: DISTINCT ON). Never an N-loop.
        # as_of_recorded -> latest entry with recorded_at <= T.
        # as_of_valid    -> latest entry with valid_from   <= T (and not superseded; §5.8).
    def list_tags(self) -> list[dict]: ...

    # 2.3 the ONE atomic mutation: commit/restore/adopt/import all route here
    def apply(self, *, config_id: str, new_doc: dict | None, entry: dict,
              expected_head_oid: str | None, expected_live_oid: str | None = None,
              make_head: bool = True) -> "ApplyResult": ...
        # ATOMIC, single transaction (replica set / SQL txn). Steps, all-or-nothing:
        #   1a. Verify current HEAD pointer oid == expected_head_oid.
        #       Mismatch -> raise StaleHead(current_oid). (concurrent-commit CAS. [FIX-1/8])
        #   1b. [V3-4] If expected_live_oid is not None: re-read live and verify
        #       oid(strip(live)) == expected_live_oid INSIDE the txn. Mismatch ->
        #       raise StaleLive(live_oid). This is what catches a RAW-MONGO bypass
        #       (which moves the runtime doc but not the HEAD pointer); without it the
        #       put in step 4 would silently clobber the bypass. commit/restore pass it.
        #   2.  Allocate entry.seq = (current head seq + 1) within the txn. [FIX-2]
        #   3.  Insert `entry` (immutable). Unique (config_id, oid) and (config_id, seq).
        #   3b. [V3-5] Close the prior HEAD entry's validity: set its valid_to =
        #       entry.valid_from (so valid-time intervals are explicit and gap/overlap-free;
        #       §5.8). No-op when there is no prior HEAD.
        #   4.  If new_doc is not None: write it to the runtime store (put_config, or
        #       seed_config/activate_config per the verb). [FIX-3]
        #   5.  If make_head: atomically move HEAD pointer to entry.oid.
        # Returns ApplyResult{seq, oid, head_oid}. On ANY step failure: full rollback.
        # expected_head_oid=None means "no prior HEAD" (first version); step 1a asserts none exists.

    # 2.4 linkage + labels (the only post-write mutations on entries)
    def link_git_sha(self, oid: str, git_sha: str) -> None: ...   # [FIX-6] ADD to git_shas[]; idempotent; never clobbers.
    def add_tag(self, oid: str, tag: str) -> None: ...
    def remove_tag(self, oid: str, tag: str) -> None: ...

    # 2.5 redaction (the ONE sanctioned content mutation; audited) [FIX-18, V3-6]
    def redact_field(self, *, config_id: str, json_path: str, replacement: str,
                     reason: str, actor: str) -> int: ...
        # Overwrites the value at json_path with `replacement` in EVERY historical
        # entry's stored doc for this config. Appends a redaction audit record.
        # Returns count of entries changed.
        # [V3-6] oid is FROZEN identity: redaction NEVER recomputes it (that would break
        # every parent_oid/HEAD/tag/git_sha FK). To keep the invariant oid==oid(strip(doc))
        # intact, `json_path` MUST resolve to a field that is in `strip_on_store` OR
        # `ignore_fields`/`ignore_globs`/`ignore_paths` (i.e. NOT part of hashed content);
        # the secret pre-flight (§9) already steers leaked secrets into strip_on_store so
        # this holds. If a secret reached a HASHED business field, redaction alone cannot
        # fix it without breaking identity: cfg requires `cfg redact --rewrite-history`
        # (§5.16) which re-chains oids forward from the redaction point and is itself a
        # gated, audited, parent-relinking operation: never the default.

    # 2.6 crash recovery [FIX-3]
    def list_pending(self) -> list[dict]: ...   # entries written but never made HEAD / never confirmed.
    def reconcile(self) -> "ReconcileReport": ...
        # idempotent: for each pending entry, roll-forward (if live == entry.oid) or
        # roll-back (drop the orphan). Used by `cfg fsck`.

    # 2.7 meta
    def ensure_schema(self) -> None: ...   # collections/tables + indexes (idempotent).
    def check_runtime_invariant(self) -> list[str]: ...  # [FIX-15] config_ids with >1 runtime_filter match.
    def backend_name(self) -> str: ...
    def supports_transactions(self) -> bool: ...   # [FIX-3] False on standalone Mongo.
    def check_atomicity_scope(self) -> "AtomicityReport": ...   # [V3-1]
        # Verifies runtime + history + heads collections are reachable in ONE transaction
        # i.e. on the SAME replica set / cluster. Returns {atomic: bool, runtime_cluster,
        # history_cluster, reason}. The apply() atomicity guarantee is ONLY valid when
        # atomic==True. (Mongo: a multi-doc txn requires one replica set; cross-DB on one
        # cluster is fine, cross-CLUSTER is not.)
    def now(self) -> datetime: ...   # server-trusted UTC clock for recorded_at. [FIX-4]
```

**Indexes every adapter MUST create:** history unique `(config_id, oid)` and `(config_id, seq)`; index `(config_id, recorded_at)`, `(config_id, valid_from)`, `(config_id, valid_to)` **[V3-5]**, `git_shas` (multikey/GIN), `tags`; a HEAD-pointer doc/row per `(config_id)` unique. Runtime: none imposed.

**[FIX-3 + V3-1] Transaction + co-location requirement:** `apply` MUST be atomic across the runtime put AND the history/heads writes. Two conditions, checked by `cfg init` and re-asserted before every gated mutation:
1. `supports_transactions()`: False on standalone Mongo.
2. **[V3-1] `check_atomicity_scope().atomic`**: runtime, history, and heads collections must be on the same replica set/cluster so a single txn can span them. The natural deployment (history/heads in a tool-owned cluster, runtime = the prod app cluster) **splits** them and silently makes the "all-or-nothing" claim false; this check catches it loudly.

If EITHER is false: `cfg` **refuses mutating verbs on any `gated` env** (error names which condition failed and the remedy: "gated env requires a transactional, co-located backend; runtime is on cluster A but history/heads on cluster B" / "point at a replica set"). On ungated envs it runs the **non-atomic fallback**: `apply` writes a `pending` intent record first, does the put, **[V3.1-5] then stamps the intent `put_confirmed=true`**, then moves HEAD + closes validity, then marks the intent done. `reconcile` (run at next open / by `fsck` / by cron) uses the marker, not a live-equality guess: an intent with `put_confirmed=true` rolls **forward** (finish the HEAD move + validity close) even if a later writer has since changed `live`; an intent without it rolls **back** (drop the orphan, the put never happened). This removes the v0.3 window where reconcile could roll back a put that actually succeeded and was already serving. The fallback is best-effort, loudly logged, and **never** used on a gated env. No silent unsafe path on prod.

---

## 3. History schema (DB-neutral)

### 3.1 Entry (immutable except git_shas/tags/redaction)
```jsonc
{
  "_id":           "<storage pk>",
  "config_id":     "agent_planner",
  "env":           "dev",                  // [FIX-9] entries are per-env; histories don't mix across envs
  "seq":           7,                       // monotonic per (config_id, env). NOT gapless. [FIX-2]
  "oid":           "<full sha256 hex>",     // identity. UNIQUE per (config_id). [FIX-10]
  "parent_oid":    "<sha256> | null",       // the HEAD oid this entry was based on (the real chain)
  "doc":           { /* full config doc */ },
  "message":       "bumped planner multi-turn",
  "author": "developer",
  "recorded_at":   "2026-06-21T10:30:00Z",  // txn time, server-assigned. [FIX-4]
  "valid_from":    "2026-06-21T10:30:00Z",  // valid time: when this became live. [FIX-4]
  "valid_to":      null,                     // [V3-5] valid time: when it STOPPED being live.
                                              // null = still the valid value for its interval.
                                              // set to the next entry's valid_from on apply (§2.3 step 3b).
  "valid_from_estimated": false,             // true when import couldn't know the real time
  "op":            "commit",                // [FIX-13] enum: commit | restore | adopt | force | import | redact
  "git_shas":      [],                       // [FIX-6] MANY code commits may ship this version
  "tags":          ["june7-good"],
  "meta": {
    "restored_from": "agent_planner@5",       // op=restore
    "bypass_detected_oid": "…",               // op=adopt/force: the live oid we folded in
    "tool_version": "cfg/0.2.0", "hostname": "…"
  }
}
```

### 3.2 HEAD pointer (separate, the CAS target): **[FIX-1]**
```jsonc
// collection: config_heads  (one per (config_id, env))
{ "config_id": "agent_planner", "env": "dev",
  "head_oid": "<sha256>", "head_seq": 7, "updated_at": "…" }
```
`apply` does its compare-and-swap on `head_oid` here (concurrent-commit safety: exactly one writer can move HEAD from a given `expected_head_oid`). **[V3-4]** A *raw-Mongo bypass* changes the runtime doc but NOT this pointer, so the `head_oid` CAS alone would not see it; that is why `apply` ALSO takes `expected_live_oid` and re-checks `oid(strip(live))` inside the txn (§2.3 step 1b): the two CAS checks together make both concurrent commits AND silent runtime bypasses fail closed.

### 3.3 op semantics: **[FIX-13]**
- `commit`: user edit applied.
- `restore`: re-apply an OLD doc as a NEW HEAD (non-destructive rollback). `valid_from=now`.
- `adopt`: fold a detected runtime bypass into history with attribution (§5.7).
- `force`: a `--force` override that bypassed the concurrent-commit CAS, recording the prior live state first. **[V3-9]** On a `gated` env `--force` still routes through §11 approval AND emits the §10 alert; force skips the *CAS*, never the *human gate*.
- `import`: first-time baseline of a pre-cfg doc (§5.2). May set a real `valid_from`.
- `redact`: a redaction audit entry (content scrub; §5.16).

Full docs per entry (no diffs). Volume is trivial at this scale; restore = copy; zero replay risk.

---

## 4. Canonical content hashing: **[FIX-5, FIX-10]**

`oid(doc) = sha256( canonical( strip( doc ) ) )`, full hex. Short oid = first 12 hex, resolved uniquely.

**strip(doc):** deep copy, then remove:
- **Top-level keys only** in `ignore_fields` (default: `_id, metrics, updated_at, updated_by, created_at, created_by, is_active`). **[FIX-5]** Matching is **top-level exact** by default; nested stripping requires explicit dotted paths in `ignore_paths`. Glob patterns (`instructions_backup_*`) match **top-level keys only**, never recursively: so a nested business field that happens to match is NOT dropped.
- **[FIX-20]** Distinguish `ignore_fields` (excluded from HASH but still STORED in `doc`) from `strip_on_store` (excluded from the stored `doc` entirely: for secrets). §3 stores `strip(doc, strip_on_store)`; hashing further strips `ignore_fields`.
- **[V3.1-1b] `strip_on_store` fields are ALSO removed before hashing.** Hash input = `doc` minus `ignore_fields`/`ignore_paths` **AND** minus `strip_on_store`. This is required so that `oid(strip(live))` (live still carries the secret) equals `HEAD.oid` (hashed from a stored doc that never had it): otherwise every secret-bearing config would read as permanently DIRTY. So the hash never sees a `strip_on_store` value, and `oid == oid(strip(stored_doc))` holds for every entry.

**canonical(d):**
- Keys sorted recursively; UTF-8; **NFC Unicode normalization** of all strings. **[FIX-5]**
- **Numbers:** int and float that are numerically equal canonicalize identically (e.g. `1` and `1.0` → `1`). Non-integers → shortest round-trip decimal, trailing zeros stripped. Reject NaN/Inf. **[FIX-5]**
- Arrays preserve order (order is semantic, e.g. `tools`).
- **`null` vs missing:** keys with `null` value are **dropped** before canonicalization (treated identical to absent). **[FIX-5]** (Documented; `--delete` and `:= null` then mean the same thing.)

**Dirty test:** `oid(strip(live)) == HEAD.oid`.

**[V3.1-1] Field-set invariant (closes self-induced drift).** The doc cfg writes to the runtime store on a commit/restore/activate MUST be the **full effective doc**: every field present in the live runtime doc except those declared in `ignore_fields`/`ignore_paths`. Two consequences, both enforced:
- **Staging always materializes the full doc.** `cfg edit` pulls the whole `runtime_filter` doc; `cfg set <path> <value>` pulls the whole live doc, applies the patch, and stages the WHOLE result (never a sparse patch). `cfg commit --from <file>` requires a full doc and **rejects** (exit 1) a file missing any live field that is not in `ignore_*` (the error lists the dropped fields). So `new_doc` always carries the same field set that was hashed into HEAD.
- **Any runtime field cfg does not version must be in `ignore_*` or `strip_on_store`.** `cfg init`/`status`/`fsck` compare the live doc's key set against `ignore_fields` + `ignore_paths` + **`strip_on_store`** (**[V3.1-1b]** the latter included so a secret field: the §9-sanctioned destination: is NOT falsely flagged); a runtime field in none of these is a config error (warns at init, flagged by fsck) because it would otherwise be hashed yet absent from the stored doc. With this, `{fields cfg writes}` and `{fields cfg hashes}` reconcile (a `strip_on_store` secret is written back to live by put but excluded from BOTH the stored doc and the hash, §4 [V3.1-1b]), so `oid(strip(live))` after a put always equals the committed entry's oid: cfg can never trip its own dirty test or a false StaleLive.

**[FIX-5] No-op guard:** if `new_doc` differs from HEAD's doc *pre-strip* but is identical *post-strip*, `commit` does NOT silently exit 0: it warns "change is confined to ignored fields; nothing versioned" and exits 1 unless `--allow-noop`.
**[V3-6] Hash stability under redaction:** because redaction is constrained to non-hashed fields (`strip_on_store`/`ignore_*`; §2.5), `oid == oid(strip(doc))` holds for every entry, redacted or not: so the dirty test, `fsck`, and `reconcile` keep working after a redaction. The rare hashed-field secret needs `cfg redact --rewrite-history` (§5.16), which re-chains oids deliberately.

---

## 5. Commands (porcelain)

Global: `cfg [global] <verb> [args] [flags]`.

**Global flags:** `--env <name>` (selects adapter+conn; **[FIX-11]** see §10 for prod safety), `--json`, `--quiet/-q`, `--yes/-y` (**[FIX-11]** does NOT satisfy prod affirmation), `--config-file <path>`.

**Exit codes (namespaced to cfg; agents branch on these: but see §12 for the MCP envelope that carries them across RPC):** **[FIX-16, V3-8]**
- `0` ok/clean · `1` user/arg error · `2` **dirty/conflict (stale HEAD or stale live)** · `3` storage/connection · `4` **needs-approval (awaiting human; retry after approval)** · `5` **not found** · `6` runtime-invariant violation (multi-doc) · `7` **[V3-8] approval declined (a human said no; terminal, do NOT retry)**.
- **[V3-8]** `4` and `7` are split so an agent/script can tell "awaiting approval" (poll, then re-invoke with the token) from "denied" (stop). This matches the MCP `needs_approval` vs `declined` statuses (§12); CLI and MCP now agree.

### 5.1 `cfg init --env X`
`ensure_schema()` (history + heads collections + indexes), write `.cfg.toml` if absent, run `check_runtime_invariant()` and **refuse to proceed if any config_id has >1 runtime_filter match** (tells you the offending ids). **[FIX-15]** Idempotent.

### 5.2 `cfg import`
Baseline live docs as `seq=1, op=import`.
```
cfg import [config_id|--all] [--strip-backups] [--valid-from <ISO>]
```
- Per config with no history: snapshot the `runtime_filter` doc as `seq=1`, `parent_oid=null`, `op=import`, `valid_from = --valid-from or recorded_at` (`valid_from_estimated=true` if defaulted). **[FIX-4]**
- `--strip-backups`: also `put_config` a cleaned doc (drop `instructions_backup_*`) and record THAT as baseline. **Mutates runtime ⇒ gated-env approval (§11).** **[FIX-11]** The legacy backup keys' content is preserved as the baseline `doc`.
- **[FIX-12] safe-on-prod note:** before `--strip-backups` on prod, `cfg import` runs `cfg refs check` (greps the codebase for reads of `instructions_backup_*`); refuses if any reader exists. (Our `persona_loader` does not read them; verified.)

### 5.3–5.4 staging: **[FIX-8, FIX-9, FIX-10]**
```
cfg edit <config_id>        # pull runtime_filter doc -> $EDITOR -> stage to .cfg/staged/<env>/<id>.json
cfg edit --from <file> <config_id>
cfg edit --abort <config_id>
cfg set <config_id> <path> <value> [--commit] [-m "..."]   # field edit; STAGES only unless --commit
cfg set <id> tools+=x | tools-=x | <path>:=null | <path> --delete
```
- **[FIX-9]** Staged files are **env-scoped** (`.cfg/staged/<env>/...`) and record `{source_env, base_head_oid, staged_at}`. `cfg commit --env Y` **refuses** a doc staged under env X.
- **[FIX-8]** `cfg set` STAGES only; applying requires `--commit` (with `-m`). `-m` is the message, never the apply trigger.
- **[FIX-10]** `tools-=x` removes the **first** matching element (documented); use index form `tools-=@2` to remove a position.
- **[FIX-5]** `:=null` and `--delete` are equivalent (null==absent).

### 5.5 `cfg commit <config_id> -m "msg"`: the workhorse
`--from <file>`, `--all`, `--force` (§10), `--allow-noop`.
**Algorithm:**
1. `new_doc` = staged doc (verify `source_env == --env`; **[FIX-9]**) or `--from`. Else error 1.
2. `head = get_head(id)`. `live = get_config(id)` (raises 6 if ambiguous: **[FIX-15]**).
3. **[FIX-1/8 + V3-4]** Set `expected_head = head.oid` (or None) and `expected_live = oid(strip(live))`. The CAS happens atomically in `apply` (step 6); we do NOT pre-check-then-write. If a concurrent COMMIT moved HEAD, `apply` rejects with StaleHead; if a raw-Mongo BYPASS moved the runtime doc, `apply` rejects with StaleLive (**[V3-4]**): both surface as exit 2 ("runtime changed outside cfg; run `cfg status`/`cfg adopt`"), so a bypass is never silently clobbered. `--force` path: first `apply` an `op="force"` entry capturing the current `live` (so the bypass isn't lost), then proceed; **[V3-9]** on a `gated` env `--force` first obtains §11 approval and emits the §10 alert.
4. `new_oid = oid(strip(new_doc))`. If `new_oid == head.oid`: no-op exit 0 ("nothing to commit"). **[FIX-5]** If equal post-strip but differs pre-strip → warn + exit 1 unless `--allow-noop`.
5. Build entry (op=commit, recorded_at=`adapter.now()`, valid_from=recorded_at, valid_to=null, parent_oid=head.oid, git_shas=[]).
6. `adapter.apply(config_id=id, new_doc=new_doc, entry=entry, expected_head_oid=expected_head, expected_live_oid=expected_live, make_head=True)`: **atomic dual-CAS + insert + close-prior-validity + put + HEAD move**. On StaleHead/StaleLive → exit 2.
7. Clear staging. Print `agent_planner@7 (sha256:a1b2c3d4e5f6)`.
`-m` required (non-empty).

### 5.6 `cfg status [config_id]`
Per config: `clean | DIRTY | staged | untracked | missing`. **[FIX-15]** `ambiguous` if >1 runtime_filter doc. Exit `2` if any DIRTY (CI/agents catch drift); exit `6` if any ambiguous. `--json` → `[{config_id, state, live_oid, head_oid, head_seq, staged}]`.

### 5.7 `cfg adopt <config_id|--all> -m "reason"`
Fold a detected bypass into history: `apply` a new entry `op="adopt"`, `doc=live`, `meta.bypass_detected_oid=live_oid`, `expected_head_oid=current head`, **`expected_live_oid=None`** (adopt deliberately accepts whatever `live` is: it is the one verb that does NOT reject on live-drift, since folding the drift in is its job; **[V3-4]**). Atomic. After adopt → clean. **[FIX-14]** Intended to be run by a scheduled job too (see §13).

### 5.8 `cfg log` / as-of semantics: **[FIX-4, FIX-9]**
```
cfg log <config_id>                       # versions of one config
cfg log --all                             # everything (the reflog/safety net)
cfg log --as-of <T>                       # SYSTEM view, VALID-time (what was live at T): default
cfg log --as-of-recorded <T>              # what cfg had RECORDED by T (txn-time)
cfg log --tag X | --git <sha> | -n K | --oneline
```
- **[FIX-4 + V3-5] `--as-of <T>` = valid-time** (the human meaning), defined by an explicit validity **interval**, not a hand-wavy "superseded" rule. Each entry has `[valid_from, valid_to)` (§3.1); `apply` closes the prior HEAD's `valid_to` to the new entry's `valid_from` (§2.3 step 3b). Reconstruction = per config, the entry whose interval **contains T**: `valid_from <= T < valid_to` (treating `valid_to = null` as +∞). This is exactly one entry per config (intervals are contiguous and non-overlapping by construction), computed by the single grouped query (§2): `valid_from <= T` AND `(valid_to is null OR valid_to > T)`, latest by `valid_from`.
  - *Why this is unambiguous where v0.2 wasn't:* a `restore` that re-applies an OLD value writes a NEW entry with `valid_from=now` and closes the previous entry's interval: so "what was live at T" between an old change and the restore returns the genuinely-live entry; a backdated `import --valid-from` does a proper interval SPLIT ([V3.1-2] below), adjusting both neighbors so no two intervals overlap. The grouped query returns exactly one row per config when intervals are well-formed; if it ever returns two for a config (only possible after a half-applied non-atomic-fallback import), that is a detectable fsck interval violation, not a silent wrong answer: `--as-of` warns and names the config rather than guessing.
  - **[V3.1-2] Backfilling intervals on import: a proper interval SPLIT, not just a forward edge.** A backdated `import --valid-from=V` inserts into the existing chain by adjusting BOTH neighbors atomically: (1) set the new entry's `valid_to` = the `valid_from` of the chronologically-next entry (or null if latest); (2) **also set the chronologically-PRIOR entry's `valid_to` = V** (close it at the insertion point). Without step (2) the predecessor still spans across V and two intervals contain V: the overlap v0.2 had. With both, intervals stay contiguous and non-overlapping even for repeated/out-of-order backdated imports. If V falls inside a single existing interval `[a, b)`, that interval is split into `[a, V)` and the new `[V, b)`. The whole re-stitch is one atomic operation (same txn / resumable on the non-atomic fallback). **[V3.1-2b]** A degenerate `--valid-from` exactly equal to an existing entry's `valid_from` would yield a zero-width `[a, a)` interval; cfg **collapses zero-width intervals** (drops them) so fsck output stays clean: they are inert anyway (no T satisfies `a <= T < a`). `cfg fsck` adds an **interval check** (per config: sorted intervals must be gap-free and overlap-free; valid_to[i] == valid_from[i+1]); a half-applied import or fallback shows up here as an overlap/gap and is reported.
- **[FIX-4] `--as-of <date>` (no time) = end-of-day UTC inclusive** (`T = date + 23:59:59.999Z`). Full timestamps accepted to disambiguate same-day. Stated once, applies to `log`/`diff`/`restore`/`tag`.
- **[FIX-4] Pre-import warning:** if `--as-of T` predates a config's earliest `valid_from` AND that baseline is `valid_from_estimated`, the row is flagged "estimated/unknown before import" rather than silently "did not exist."

### 5.9 `cfg diff`: **[FIX-7]**
Ref grammar (one canonical form, sigils, **no bare ambiguous tokens**):
`@<seq>` | `sha256:<hex>` (or `#<hex>` short) | `@{<date|ISO>}` (valid-time) | `tag:<name>` | `=live` | `=HEAD`.
```
cfg diff <id> @5 @7 | cfg diff <id> #a1b2c3 #9f8e7d | cfg diff <id> @5 =live
cfg diff <id>                       # =HEAD vs =live (the dirty delta)
cfg diff --as-of @{2026-06-07} --to =live   # SYSTEM diff between June7 and now
```
Field-level structured diff; big `instructions` rendered as unified section diff by default; `--semantic` (§7, off by default). `--json` → `{config_id, changes:[{path, op, before, after}]}`. `--stat`.

### 5.10 `cfg show <id> <ref> [--field path]`

### 5.11 `cfg restore`: **[FIX-7, FIX-4-system-atomicity]**
```
cfg restore <id> <ref>                  # single config to a version (@5 | #hash | @{date} | tag:X)
cfg restore --as-of @{2026-06-07}       # SYSTEM: all configs to their VALID-time-T state
cfg restore --tag june7-good            # SYSTEM: all configs to a tagged moment (RECOMMENDED) [FIX-3-agent]
```
**Modifiers:** `--preview` (§6), `--dry-run` (compute plan, change nothing), `--only a,b` / `--except x`, `--include-code` (resolve git_sha for the moment, run deploy cmd §6), `--include-deleted` (also restore configs that have history but no current runtime doc: **[FIX-9] default TRUE for `--tag`/system restore so the moment is complete**).
**System restore algorithm:**
1. `ids = list_config_ids() ∪ {configs with history but no live doc}`. **[FIX-9]**
2. For each id, resolve `target` (valid-time-T entry, or tagged entry). Skip + report configs with no state at T.
3. Build plan `[(id, head_now → target, oid delta)]`. `--dry-run` prints + stops.
4. **[FIX-4-atomicity]** Apply per-config via `apply` (each atomic), passing `expected_live_oid` so a config that drifted mid-restore fails closed rather than clobbering (**[V3-4]**); a config that has history but no live doc (deleted) is restored via `seed_config` instead of `put_config` (**[V3-2]**). **Re-runnable by construction:** a config already at `target.oid` is a no-op (step skips). On partial failure, emit a machine-readable report + `restore_token`; **`cfg restore --resume <restore_token>`** re-applies only the not-yet-converged configs (idempotent). System restore is NOT all-or-nothing across configs (cross-config Mongo txn over 43 docs is impractical) but IS guaranteed convergent on re-run: and `--dry-run`/`--preview` are the safe default for agents (§12). **[V3-10]** For a gated env, the whole plan is approved once as a unit: the §11 approval is bound to `plan_oid = oid([(id, target_oid) sorted])`; if drift changes the plan between approval and apply, the plan_oid no longer matches and re-approval is required (`--resume` carries the same plan_oid as long as the remaining targets are unchanged).
History only moves forward; a restore is itself revertible.

### 5.12 `cfg tag`: **[FIX-6]**
```
cfg tag <name> --as-of @{T}            # tag the resolved entry of EVERY config: TRANSACTIONAL or resumable
cfg tag <name> <id> @7
cfg tags | cfg tag --delete <name>
```
System tag applies to N entries; **[FIX-6]** done in one txn where supported, else resumable with a completeness check. `cfg restore --tag` verifies the tag covers all expected configs and warns on a partial tag.

### 5.13 `cfg points --around @{T} [--window 3d]` / `cfg points <id>`
List change-moments (recorded_at · valid_from · config_id · seq · git_shas · message) to pick `--as-of`/tag targets.

### 5.14 git linkage: **[FIX-6, FIX-19]**
Trailer + post-commit hook (no custom git flag).
```
cfg commit agent_planner -m "multi-turn fix"     # -> agent_planner@7 (sha256:a1b2c3)
git commit -m "planner multi-turn

Cfg-Version: agent_planner@7"
```
Hook reads `Cfg-Version:` trailers (`git interpret-trailers`), resolves each to an `oid`, calls `link_git_sha(oid, sha)` (**add-to-set**, never clobber; a version can carry many shas; a sha can carry many configs). **[FIX-19]** Trailers carry only `id@seq`: NEVER doc content. `cfg hooks install|uninstall`.

### 5.15 `cfg whoami | config | version | fsck`
`cfg fsck` runs `adapter.reconcile()` (**[FIX-3]** roll-forward/back pending entries) + `check_runtime_invariant()` + **[V3-1]** `check_atomicity_scope()`; report.

### 5.16 `cfg redact <config_id> --field <path> --reason "..."`: **[FIX-18, V3-6]**
The ONE sanctioned content mutation. Overwrites the value at `<path>` with `[REDACTED]` across **all** historical entries' docs for the config, appends an `op="redact"` audit entry (`actor, reason, recorded_at, paths`). For purging a leaked secret. Gated-env ⇒ approval (§11). Also auto-suggested when `cfg commit` detects a secret-shaped value (§9).
- **[V3-6]** `<path>` must be a non-hashed field (in `strip_on_store`/`ignore_*`) so entry `oid`s stay valid; the secret pre-flight (§9) steers leaked secrets there. Refuses otherwise with the remedy below.
- **[V3-6] `cfg redact --field <path> --rewrite-history`**: the escape hatch for a secret that reached a HASHED business field. It recomputes `oid` from the redaction point forward and **relinks** every `parent_oid`, HEAD pointer, tag, and `git_sha` along the chain, recording an `op="redact"` entry per touched version. Destructive to oids → gated (§11) + alert (§10) even on dev, and `--dry-run` shows the full set of oids that will change. This is the only operation in cfg that rewrites identity; it exists so a leaked key is never unfixable.
  - **[V3.1-8] Ordering + resumability** (it cannot be one txn over N entries at prod scale): re-chain proceeds in **ascending `seq` order** from the redaction point: each step recomputes the entry's `oid`, sets its `parent_oid` to the prior step's new `oid`, and migrates that entry's `tags`/`git_shas`/any HEAD pointer onto the new `oid`. The operation is **idempotent and resumable** (`cfg redact --resume <token>`): a step whose entry already hashes to its expected new `oid` is skipped, so a crash mid-rewrite is recovered by re-running. `git_shas` survive because git trailers key on `id@seq` (stable), not `oid` (§5.14): only the entry's stored `git_shas[]` array moves to the new oid. `fsck` verifies the chain (`parent_oid` links + `oid==oid(strip(doc))`) after a rewrite.

### 5.17 `cfg activate <config_id> <ref>`: **[V3-3]**
For runtimes that key on `(config_id, is_active)` and roll out by flipping which doc is active. Promotes the doc at `<ref>` (a version) to be the live `is_active:true` row and deactivates the current one, leaving exactly one `runtime_filter` match: so the §2.7 invariant (and exit 6) is never tripped by a legitimate activation. **[V3.1-3] Routes through `apply()` like every other mutation:** it passes `expected_head_oid = head.oid` AND `expected_live_oid = oid(strip(current active live))` (the dual-CAS, §2.3 step 1a+1b), with `activate_config` as the put-variant (§2.3 step 4) that does the atomic deactivate-old + upsert-new. So activate is NOT a side path: a concurrent commit or raw bypass of the active row makes it fail StaleHead/StaleLive (exit 2), exactly like `commit`; it never skips the CAS. Records an `op="commit"` entry (`meta.activated_from`) so the flip is itself versioned, and moves HEAD to it. Gated-env ⇒ approval (§11). Without this verb, promoting a new active row would require a raw bypass; with it, activation is cfg-blessed. (If a project's runtime does NOT use an `is_active`-style flag, this verb is simply unused.)

### 5.18 approval verbs: **[V3-7]**
The human side of §11. These are the ONLY surface that resolves a pending approval; no mutating verb can self-resolve.
```
cfg approvals                         # list pending approvals (id, action, requester, plan summary, age)
cfg approve <approval_id>             # approve: prompts for affirm_phrase (local) / records approver identity
cfg deny <approval_id> [--reason]     # deny: terminal; the bound verb returns exit 7 / status:"declined"
cfg approval show <approval_id>       # full plan + dry-run diff for the pending action
```
`cfg approve`/`deny` are deliberately a **separate invocation** from the verb that requested approval (and, for `slack`/`webhook`, a separate human on a separate channel): that separation is what makes the gate real for agents (§11). There is intentionally **no `cfg.approve` MCP tool** (§12): an agent can *observe* status but can never *grant* it.

### 5.19 `cfg refs check [glob]`: **[V3-11 / FIX-12]**
Greps the codebase (configurable roots in `.cfg.toml`) for readers of soon-to-be-removed keys (default glob `instructions_backup_*`); used internally by `cfg import --strip-backups` (refuses if a reader exists) and runnable standalone. Reports file:line of any match. Read-only.

---

## 6. Preview environment: **[FIX-12]**
`--preview` targets `[env.preview]` in `.cfg.toml` (separate runtime DB; optional deploy cmd).
- **[FIX-12]** `cfg init --env preview` **provisions/validates** the preview store and fails loudly with the exact missing-config remedy if absent: so `restore --preview` never dies with a generic error mid-incident.
- v0.2 tiers: **Minimal** = restore into `*-preview` DB; you point a backend at it. **Full (later)** = `deploy` cmd spins an ephemeral app at the resolved git_sha and returns a URL.
- **[FIX-19]** Preview DB inherits the SAME secret-handling (`strip_on_store`) and access posture as prod; it is not a lax copy.

---

## 7. LLM system-impact layer: **[FIX-19, V3-LLM]**

This is the capability that separates `cfg` from off-the-shelf prompt tooling. Text/embedding diff answers *"how much did the words change?"* `cfg` answers the two questions a reviewer actually has: **what is the nature of this change, and what are its consequences for the rest of the agent system?** It is opt-in (egress + cost), but it is a first-class feature, not a footnote.

### 7.1 Why this is the gap (and what prior art does NOT do)
The landscape (credited in `CREDITS.md`, §17.x) splits three ways and **none model a fat multi-field config doc against a whole agent graph**:
- **Embedding-% diff** (e.g. *llm-prompt-semantic-diff*): one cosine score on a prompt string. Blind to which field changed, blind to other configs.
- **Behavioral diff** (e.g. *llm-behavior-diff*): runs a prompt suite through two model versions and scores divergence. Needs a model swap + an eval suite; it judges *outputs*, which we explicitly do NOT trust as the signal (we eval reasoning/tool-calls/payloads, not generations).
- **In-prompt inconsistency CoT** (*"Prompting in the Wild"*, arXiv:2412.17298): read old+new prompt, find changed parts, flag self-contradiction *within one prompt*. The method is sound and we adopt it, but it stops at a single prompt's four walls.

`cfg`'s differentiator: the unit is the **config doc** (instructions + `tools` + `client_config` + `prompt_templates` + `phase_contract` + `fallback_models` + `skills` + `default_options` …), and the frame is the **system** (the other 42 configs and the contracts between them). The analysis is over **structured fields and cross-config edges**, not a prompt string.

### 7.2 The system model `cfg` builds (no output generation, no eval suite)
On demand, `cfg` constructs a lightweight **config graph** from the history store + a project manifest:
- **Nodes** = configs (each at a chosen ref).
- **Declared edges** = relationships the docs already encode: a `phase_contract` that names an upstream/downstream persona; a `tools[]` entry another config also lists; a `prompt_template` key referenced across configs; a `fallback_models` entry pointing at a model another config owns; shared `skills`. (Edge extractors are pluggable per project; the manifest declares which fields carry cross-config meaning: see `[impact]` in §8.)
- The graph is **static and declarative**: built from config CONTENT, never by running the agents. This keeps the analysis cheap, deterministic in what it inspects, and honoring the "no vision/output evals" rule.

### 7.3 What it reports: the nature of the change and its consequences
`cfg impact <id> <a> <b>` (and `cfg diff … --impact`) emit a structured report with these dimensions:

**(A) Nature of the change**: classify the delta, per field, not as a percentage:
- `intent_shift`: the instructions changed what the agent is trying to do (CoT: read both, name the behavioral delta in one line).
- `scope_change`: added/removed a capability (`tools±`, `skills±`), widened/narrowed allowed actions.
- `contract_change`: `phase_contract` / output-shape / hand-off keys changed (the dangerous class: breaks downstream consumers).
- `policy_change`: temperature/model/`max_tokens`/timeout/`fallback_models` (behavioral envelope, not task).
- `cosmetic`: wording with no behavioral or contract effect (the thing embedding tools over-flag).
- `self_inconsistency`: the new doc contradicts itself (adopted CoT method, intra-config).

**(B) Consequences across the system**: the part nothing off-the-shelf does:
- **Downstream impact:** which configs consume a contract/tool/template this change touched, and HOW they could break (e.g. "planner@8 stopped emitting `shot_breakdown.groups`; `dag_builder` and `fill_agent` read that key → likely null-deref / empty plan"). Grounded in declared edges, so it cites the specific consuming config + field.
- **Cross-config conflict:** the change now contradicts another live config (e.g. two personas claim the same hand-off; a tool removed here is still required by a sibling's contract; a model pinned here is in another's `fallback_models` that you just deleted).
- **Orphan / dangling references:** a `prompt_template`/tool/skill/model the new doc references that no longer exists anywhere in the system (or vice-versa: a now-unreferenced shared asset).
- **Blast radius:** the set + count of configs reachable from the change over declared edges (1-hop and transitive), so a reviewer sees "touches 1 config" vs "touches 9."

**(C) Severity + recommendation**: `info | caution | breaking`, with the reason and the suggested guard (e.g. "breaking: restore `phase_contract.outputs` or update the 2 downstream readers first"). Severity is about *system contracts*, deterministically derived from which dimension fired (contract_change with ≥1 downstream consumer = breaking), with the LLM supplying the human-readable *why* and the cross-config reasoning: not a vibe score.

### 7.4 Surfaces
```
cfg impact <id> <a> <b>            # full system-impact report between two refs of one config
cfg impact --as-of @{T} --to =live # SYSTEM impact: everything that changed since T, graph-wide
cfg diff <id> a b --impact         # diff + the impact summary inline
commit.pre_hook = "impact"         # on commit: print the report; WARN only (never blocks) unless --strict
```
- **Warn, never block, by default.** `--strict` (or `pre_hook` policy `block_on=breaking`) turns a `breaking` verdict into a non-zero exit / `needs_approval`, for teams that want a hard gate.
- `--json` returns the full structured report (dimensions, edges, blast radius) so an **agent** can read consequences before it commits: the MCP tool `cfg.impact(...)` (§12) exposes exactly this. An agent proposing a config edit can call `cfg.impact`, see "breaking: 3 downstream readers," and choose to fix them or ask the human, instead of shipping a silent contract break.
- Runs against any two refs, including a proposed (staged, uncommitted) doc: so you get the consequences *before* the commit, which is the whole point.

### 7.5 Data-egress controls (mandatory): **[FIX-19, V3.1-4]**
- Before ANY LLM send: apply `strip_on_store` AND secret-shaped-field redaction (§9). A leaked secret never rides along to the provider.
- **Off by default; opt-in PER CONFIG** via `semantic_allowlist`/`[impact].allow` in `.cfg.toml` (a global switch is not enough: some configs may be too sensitive to ever send).
- **[V3.1-4] Per-config gating is enforced at the SEND boundary for EVERY config in a batch, and a config's TEXT crosses egress ONLY if that config is itself allowlisted: including neighbors.** A system-wide run (`cfg impact --as-of T --to =live`) computes structure for ALL configs locally, but the LLM call for config X is fed ONLY: X's own text (X must be allowlisted, else X gets structure-only, no prose) PLUS, for cross-config narration, **only the declared edge METADATA of neighbors: field names and consuming `config_id`s, never a neighbor's doc text.** So explaining "planner@8 broke `dag_builder`'s `shot_breakdown.groups` contract" sends the edge fact (field name + that `dag_builder` consumes it), not `dag_builder`'s instructions. A neighbor's text is sent only when that neighbor is also in `allow`. This closes the system-wide leak where narrating one allowlisted config could exfiltrate a non-allowlisted neighbor. **[V3.1-4b] Honest caveat:** because contracts are mutually referential, a config's OWN doc may quote a neighbor by name (e.g. `phase_contract` embedding a downstream key name): that substring crosses under the config's own allowlist decision (it is the config's own text, which the owner consented to send), so "only X's text crosses" bounds egress to allowlisted configs but does not guarantee no neighbor *identifier* ever appears; the orphan-reference check needs only the dangling name, never the neighbor's value.
- The consent/log line **names the provider**, states that full config text leaves the org, and **lists exactly which config_ids' text will be sent** (so a batch can't silently widen the set); printed on first use per session.
- The **graph extraction in §7.2 and the deterministic severity in §7.3C run locally with NO LLM**: only the natural-language *nature* classification (A) and the *why* narration (B/C) call a model. So even with the LLM fully disabled, `cfg impact` still reports blast radius, downstream consumers, cross-config conflicts, and orphan references (structural facts): you lose only the prose, not the consequence detection. **[V3.1-6] Severity (§7.3C) is keyed SOLELY off the LOCAL structural detection** (e.g. a `phase_contract` edge-field diff with ≥1 declared consumer ⇒ `breaking`); the LLM's nature label (A) is descriptive prose and is NEVER an input to severity: so severity is deterministic and reproducible even if the model is off or its prose varies. The LLM is the explainer, not the detector.

### 7.6 Implementation
- Shipped as a `cfg-impact` plugin; **core has zero LLM dependency** (§1 boundary holds). The plugin provides the model client + the edge-extractors; the manifest (`[impact]`) wires field→edge semantics per project.
- Provider-agnostic (Claude/OpenAI/local); default Claude. No model SDK in core.

---

## 8. `.cfg.toml`
```toml
[project]
name = "example-agent-configs"

[stores]
runtime_collection = "agent_configs"
history_collection = "config_history"
heads_collection   = "config_heads"
id_field    = "config_id"
runtime_filter = { is_active = true }    # [FIX-15] which doc per config_id is "the" doc

[hash]
ignore_fields = ["_id","metrics","updated_at","updated_by","created_at","created_by","is_active"]
ignore_globs  = ["instructions_backup_*"]   # top-level keys only
ignore_paths  = []                           # explicit dotted paths for nested
strip_on_store = []                          # [FIX-20] fields removed from STORED doc (secrets)

[secrets]                                    # [FIX-18]
deny_field_globs = ["*_key","*_secret","*_token","*api_key*","*password*"]
deny_value_regex = ["sk-[A-Za-z0-9]{20,}","AKIA[0-9A-Z]{16}"]
on_match = "refuse"                          # refuse | warn ; bypass with --allow-secret (audited)

[impact]                                     # [V3-LLM] §7 system-impact layer (plugin)
enabled = false                              # master switch; structural analysis still runs locally when sending is off
allow = []                                   # PER-CONFIG egress allowlist (config_ids whose text may go to the LLM)
provider = "claude"                          # claude | openai | local ; SDK lives in the plugin, never core
block_on = "none"                            # none | breaking ; pre_hook hard-gate threshold
# edge_fields: which doc fields carry CROSS-CONFIG meaning, and how to read them (the §7.2 graph)
edge_fields = [
  { field = "phase_contract", kind = "contract" },   # names upstream/downstream personas + output keys
  { field = "tools",          kind = "shared_set"  }, # a tool listed by multiple configs
  { field = "prompt_templates", kind = "ref_keys" },  # template keys referenced across configs
  { field = "fallback_models", kind = "model_ref" },  # points at a model another config owns
  { field = "skills",         kind = "shared_set"  },
]

[identity]
author_from = "git"                          # git user.email | env CFG_AUTHOR | os user

[env.dev]
backend="mongo"; uri="env:DEV_MONGODB_URI"; db="appdb-dev"; gated=false
[env.prod]
backend="mongo"; uri="env:PROD_MONGODB_URI"; db="appdb"; gated=true
[env.prod.policy]                            # [FIX-11,17]
require_reason=true; require_clean=true; break_glass=false; affirm_phrase="prod"
[env.preview]
backend="mongo"; uri="env:PREVIEW_MONGODB_URI"; db="appdb-preview"; gated=false
```
**[FIX-11]** Secrets only via `env:VAR`. **dev and prod URIs are distinct env vars** even if the same cluster: so `--env prod` requires `PROD_MONGODB_URI` to be set, it is not silently reachable from the dev URI.

---

## 9. Identity / secrets at commit: **[FIX-18]**
`author` = `--author` → `CFG_AUTHOR` → git `user.email` → OS user. Stored verbatim.
**Secret pre-flight on every `commit`/`set --commit`:** scan `new_doc` against `[secrets]` deny lists. On match: refuse (exit 1) unless `--allow-secret` (records `meta.allow_secret=true`, author, reason). Suggest `strip_on_store` or `cfg redact`.

---

## 10. Environments + gating: **[FIX-11, FIX-17]**
- **dev** (`gated=false`): instant, ungated, no confirmation. 95% of edits. Matches today's speed.
- **prod** (`gated=true`): mutating verbs require **§11 approval** (not the generic `-y`). Policy: `require_reason` (`-m` must cite a ticket/INC), `require_clean` (refuse if DIRTY; `adopt` first), `break_glass` **default false**, `affirm_phrase` (human must type `prod`).
- **[FIX-11]** `--env prod` resolves from a **distinct** `PROD_MONGODB_URI`; if unset, prod ops error (you cannot fat-finger into prod without prod creds present). `cfg whoami` prints the env prominently. Refuse if `CFG_ENV=prod` is merely inherited ambient (require explicit per-invocation `--env prod` OR an explicit `CFG_CONFIRM_AMBIENT_PROD=1`).
- **[FIX-17]** `--force`/break-glass on prod still routes through §11 approval (a typed reason alone is not a control); every `--force`/`break_glass` use emits an alert (e.g. Slack webhook) and is rate-limited.
- **Propagation stays instant** in both (no code deploy); gating adds a human checkpoint, not latency.
- **Org control (stated, out of tool scope but load-bearing):** restrict direct *prod* write creds so the cfg service identity is effectively the only prod writer; devs keep raw **dev** access. `status`/`adopt`/scheduled-cron are the backstop for break-glass.

---

## 11. Approval flow (the real human-in-the-loop): **[FIX-1-agent, FIX-4-agent]**
The keystone for "safe for agents AND safe for fat-fingers."

`ApprovalProvider` interface; a gated mutation NEVER completes from a caller-supplied flag.
```python
class ApprovalProvider(Protocol):
    def request(self, *, action: dict, requester: str, env: str) -> "Pending": ...  # returns {approval_id, state:"pending"}
    def status(self, approval_id: str) -> "ApprovalState": ...                       # pending|approved|denied|expired
    # resolution happens OUT OF BAND (not via this caller).
```
Flow for any gated mutation (`commit/restore/adopt/redact/import --strip-backups` on a `gated` env):
1. The verb computes its plan and calls `ApprovalProvider.request(...)` → returns `approval_id`. **It applies nothing.** (CLI exit `4` / MCP `status:"needs_approval"` with the id + a human-readable plan + the `--dry-run` diff.)
2. A **human**, through a SEPARATE channel, approves: interactive CLI (`cfg approve <id>` typing the `affirm_phrase`), or a Slack approve button, or a signed token from a human-authenticated session. The approver identity is recorded.
3. Re-invoking the verb with the now-approved `approval_id` (`cfg commit … --approval <id>`) applies it. The id is single-use, bound to the exact `(action, plan-oid)`; if the plan changed, it's invalid.
4. **[V3.1-7]** If the human **denies** (`cfg deny <id>`, or status becomes `denied`), the bound verb terminates with **CLI exit `7`** / MCP **`status:"declined"`**: terminal, do NOT retry (distinct from `4`/`needs_approval`, which means "still awaiting, retry after approval"). An `expired` approval also ends the verb (re-request to try again). This is the mapping `ApprovalProvider` `denied`→exit 7 referenced in §5/§5.18/§12/§14.

**Providers:**
- `local` (default for solo/CLI): an interactive TTY prompt that requires typing `affirm_phrase`; cannot be satisfied by `-y` or by piped stdin → an unattended agent literally cannot pass it.
- `slack` / `webhook` (for teams + agents): the request posts to a channel; a human clicks approve; the agent polls `status`. **The agent cannot self-approve because approval requires a human identity on a different channel.** **[FIX-1-agent]**

This makes the §12 "safe for agents" claim real: the agent can *propose* and *preview* anything, but *applying* to a gated env always crosses a human.

---

## 12. Agent interface (Claude/Codex): first-class: **[FIX-2-agent, FIX-4-agent, FIX-5-agent, FIX-6-agent]**

**(a) `--json` everywhere.** Stable schemas. But agents must read the result envelope, not exit codes (next).

**(b) MCP server (`cfg-mcp`).** Every tool returns a **uniform envelope** carrying what CLI exit codes carry: **[FIX-2-agent]**. The wire `status` values are the **plain, agent-readable words** (the technical exit-code name is in parentheses for the implementer; agents branch on the plain word):
```
{ "status": "ok" | "changed_outside_cfg"(dirty) | "conflict" | "needs_human_ok"(needs_approval)
            | "was_declined"(declined) | "not_found" | "error" | "bad_config"(invariant_violation),
  "code": "...", "message": "...", "data": { ... } }
```
Tools (now COMPLETE vs the skill flow: **[FIX-6-agent]**):
```
cfg.status(env?, config_id?)                     -> data: [{config_id,state,head_seq,...}]   (status:"changed_outside_cfg" if any)
cfg.log(env?, config_id?, as_of?, as_of_recorded?, tag?, limit?)
cfg.diff(env?, config_id, a, b, semantic?)
cfg.impact(env?, config_id?, a, b | as_of, to?)   # [V3-LLM] §7 nature-of-change + system consequences; read-only; structural facts even with LLM off
cfg.show(env?, config_id, ref)
cfg.points(env?, around, window?)
cfg.commit(env, config_id, doc|patch, message, author?, approval?)   # gated env -> status:"needs_human_ok" + approval_id
cfg.adopt(env, config_id|all, message, approval?)                    # [FIX-6-agent] EXISTS now
cfg.set(env, config_id, path, value, message, commit?, approval?)    # [FIX-6-agent] EXISTS now
cfg.restore(env, {ref?|tag?|as_of?}, preview=?, dry_run=?, only?, include_code?, approval?)
cfg.tag(env, name, as_of?|ref?, approval?)
cfg.redact(env, config_id, field, reason, approval?)
cfg.approval_status(approval_id)
```
**Agent-safety rules baked into the tool contracts: [FIX-4-agent]:**
- `cfg.restore` and other gated mutations: on a `gated` env, ALWAYS return `status:"needs_human_ok"` with an `approval_id` + the dry-run plan; they apply ONLY when re-called with a *human-resolved* `approval` token. There is NO boolean an agent can set to skip this.
- `cfg.restore` defaults: `dry_run=true`, `preview=true` for agents. The agent must consciously set them false AND obtain approval to hit real prod.
- **[FIX-3-agent]** Agents resolve a moment via `cfg.points` → present candidates to the human → restore by **`tag`** (created by the human) or an explicit human-confirmed `ref`. Free-form `as_of` restore on prod is gated behind approval like any mutation. The skill never lets the agent pick `as_of` for a prod write unilaterally.

**Idempotency/retry table (stated for agents): [FIX-5-agent]:**
| tool | retry-safe | note |
|---|---|---|
| status/log/diff/show/points/approval_status | yes | pure reads |
| commit | conditional | re-call with same staged doc: no-op if already HEAD; a StaleHead (status:dirty) must NOT be blind-retried: re-pull first |
| restore | conditional | supply `restore_token`/idempotency key; a retried restore converges (no-op if at target), never double-applies |
| adopt | yes | converges (live==HEAD after first success → no-op) |
| set --commit | conditional | same as commit |
| tag | yes | re-tag is idempotent |
| redact | yes | replacement is idempotent |

**(c) Claude skill (`/cfg`).** SKILL.md teaches it in plain language (the words an agent reads should match the wire statuses): the verbs; the **check-first rule** (`cfg.status` before any save; if it says `changed_outside_cfg`, surface it and fold it in with `cfg.adopt` + a reason: both tools exist); the **safety contract** ("never write the database directly, only `cfg.*`; a restore never destroys anything; production changes always need a human's OK; restore by a bookmark/tag, never pick a date for a prod write yourself"); canned flows expressed ONLY in tools that exist:
- *Reproduce June-7:* `cfg.points(around)` → show candidates to human → human picks/creates `tag` → `cfg.restore(tag, preview=true, dry_run first)` → report preview → on human approval token, `cfg.restore(tag)` to prod.
- *Version a prompt edit:* `cfg.status` → stage via `cfg.set`/doc → `cfg.diff` → **`cfg.impact` (read the system consequences before committing: if it reports `breaking` with downstream readers, fix them or surface to the human)** → `cfg.commit` (dev: instant; prod: needs_approval).

---

## 13. Adoption / operational: **[FIX-14, FIX-15]**
- **[FIX-14] Migrate the workflow, don't just ban it.** The ~20 `seed_*.py`/`backfill_*.py` scripts that raw-write `agent_configs` are converted to call `cfg commit --from` (or the engine API) so the daily path goes through `cfg`. Provide a shim (`cfg apply-doc <id> --from <file> -m`) the scripts call.
- **Detective controls that actually run: [FIX-14]:** (1) **CI gate**: `cfg status --json` on dev/prod fails the pipeline on DIRTY or invariant violation. (2) **Scheduled `cfg adopt --all`** (cron) so any bypass is folded into history with attribution within a day, not lost. (3) `cfg fsck` recovers crash-orphans.
- **[FIX-15]** `cfg init`/`status`/`fsck` enforce the runtime invariant (one `runtime_filter` doc per config_id); a violation is exit 6 with the offending ids: caught before it corrupts versioning.
- **Honest framing:** without the §10 org cred-lockdown, drift is continuous; the cron-`adopt` + CI-gate keep history *honest* (every change attributed) even when not *gated*.

---

## 14. Edge cases & failure modes (decisions): consolidated
| Case | Decision |
|---|---|
| Two devs commit same config concurrently | `apply` CAS on `head_oid`: exactly one wins; loser gets StaleHead → exit 2 "re-pull". No lost update (HEAD pointer, not seq-uniqueness). **[FIX-1]** |
| Raw-Mongo bypass lands between read and write | **[V3-4]** `apply` ALSO CAS-checks `expected_live_oid` inside the txn → bypass → StaleLive → commit aborts (exit 2), not clobbered. (`head_oid` CAS alone misses it; a bypass doesn't move HEAD.) `adopt` is the one verb that intentionally accepts the drift. |
| Commit identical to HEAD (post-strip) | no-op exit 0. Differs pre-strip only → warn, exit 1 unless `--allow-noop`. **[FIX-5]** |
| Crash mid-apply / non-atomic fallback | `apply` writes a `pending` intent; `fsck`/`reconcile` rolls forward/back on next open; gated envs refuse a non-transactional OR non-co-located backend entirely. **[FIX-3, V3-1]** |
| Runtime + history/heads on different clusters | **[V3-1]** `check_atomicity_scope()` detects the split; gated envs refuse (single txn can't span clusters); ungated use the logged fallback. The atomicity guarantee is never silently false. |
| `as-of` over restored/imported configs | valid-time **intervals** `[valid_from, valid_to)` drive it; exactly one entry contains T; backdated import slots into the chain without overlap. **[FIX-4, V3-5]** |
| Restore re-applies an OLD value | new entry `valid_from=now`, prior interval closed → "what was live at T" stays correct for T before the restore. **[V3-5]** |
| Config deleted then recreated | history-without-live restored via `seed_config`; `--include-deleted` default true for system restore so completeness holds. **[FIX-9, V3-2]** |
| Multi-doc per config_id (is_active) | `runtime_filter` selects the one; >1 match → exit 6. Legitimately promoting a new active row → `cfg activate` (atomic flip), not a bypass. **[FIX-15, V3-3]** |
| `put_config` target | updates the unique `runtime_filter` doc in place; never inserts a 2nd active doc; 0/≠1 match → error. **[V3-2]** |
| Sparse commit / runtime-only field not ignored | **[V3.1-1]** staging always materializes the FULL doc (`cfg set` pulls+patches+stages whole; `--from` rejects partial); any runtime field outside `ignore_*` is a config error flagged at init/fsck → cfg never trips its own dirty test / false StaleLive. |
| Backdated import into an existing interval | **[V3.1-2]** proper interval SPLIT: closes the PRIOR entry's `valid_to` at the insertion point too, not just the new entry's forward edge; fsck interval check catches any overlap/gap. |
| `cfg activate` concurrency | **[V3.1-3]** routes through `apply()` with dual-CAS (`expected_head_oid`+`expected_live_oid`); a concurrent commit/bypass → exit 2, never a silent clobber; not a side path. |
| System-wide impact egress | **[V3.1-4]** only a config's OWN text crosses egress (if allowlisted); cross-config narration sends edge METADATA (field names + config_ids), never a non-allowlisted neighbor's text. |
| Hash short-oid collision | full sha256 is identity/FK; 12-hex is display, resolved unique-prefix-or-error. **[FIX-10]** |
| Secret committed | refused at commit (deny lists) unless `--allow-secret`; leaked → `cfg redact` (non-hashed field, oid frozen) or `cfg redact --rewrite-history` (hashed field, oids re-chained, gated). **[FIX-18, V3-6]** |
| Impact/semantic egress | structural facts (blast radius, downstream readers, conflicts) computed LOCALLY; only prose calls the LLM; strips strip_on_store+secrets, off-by-default per-config, names provider. **[FIX-19, V3-LLM]** |
| System restore partial failure | report + `restore --resume <restore_token>`; convergent on re-run; gated approval bound to `plan_oid`; preview/dry-run default for agents. **[FIX-4, V3-10]** |
| Agent tries to mutate prod | needs_approval envelope + human out-of-band approval (`cfg approve`/`deny`, no MCP equivalent); exit 4 (awaiting) vs 7 (declined) distinct. **[FIX-1-agent, V3-7, V3-8]** |

---

## 15. Non-goals (v0.3.1)
Branching/merging of configs; diff-based storage/packing; built-in RBAC (rely on DB creds + gating + approval + org lockdown); encrypting docs at rest beyond `strip_on_store`/`redact`; a GUI; **running the agents or evaluating their generated outputs** (the impact layer §7 is static/declarative over config content + declared edges, by design: consistent with "eval reasoning/payloads, not generations").

---

## 16. Changes from v0.1 (the review fixes)
- **FIX-15** multi-doc-per-config_id (`runtime_filter`, invariant check, exit 6): *foundational.*
- **FIX-1/2/8** real CAS via a HEAD pointer + atomic `apply` (seq inside the txn; drop gapless): *closes the lost-update + TOCTOU.*
- **FIX-4** bitemporal `recorded_at`/`valid_from`; `--as-of` = valid-time; date = end-of-day UTC; import valid_from: *makes "reproduce June-7" trustworthy.*
- **FIX-3** transaction requirement; refuse standalone on gated; `fsck`/`reconcile` crash recovery.
- **FIX-1-agent / FIX-4-agent** `ApprovalProvider` out-of-band human approval (no agent-suppliable confirm); agent `restore` defaults preview+dry-run; restore-by-tag.
- **FIX-2-agent** MCP uniform result envelope (carries dirty/needs_approval across RPC).
- **FIX-6-agent** MCP tool set ⟷ skill flow consistency (`adopt`/`set` exist).
- **FIX-5-agent** per-tool idempotency/retry table.
- **FIX-18/19/20** secret pre-flight + `redact` + `strip_on_store` (≠ ignore_fields) + LLM/preview egress controls.
- **FIX-11/17** prod = distinct creds + affirm phrase (not `-y`); `--force`/break-glass routes through approval + alerts; break_glass default false.
- **FIX-7** unambiguous ref grammar with sigils.
- **FIX-5** hashing: int/float equality, NFC, null==missing, top-level-only stripping, no-op-confined-to-ignored warning.
- **FIX-9** single grouped `as-of` query (no N-loop); `include-deleted` default for system restore; env-scoped staging.
- **FIX-10** full sha256 identity, 12-hex display.
- **FIX-13** `op` enum split (adopt/force/redact added; revert dropped).
- **FIX-14** migrate seed scripts to cfg; CI gate + cron-adopt as the running detective controls.
- **FIX-12** preview env provisioned/validated by `init`; `refs check` before `--strip-backups` on prod.
- **FIX-16** namespaced exit codes; split not-found (5) from arg error (1); invariant (6).
- **FIX-6** `git_shas[]` many-to-many; `link_git_sha` add-to-set.

---

## 17. Build order (v0.3)
1. **Core + MongoAdapter (replica-set) + schema (entries+`valid_to` + HEAD pointer) + co-location/txn checks `[V3-1]` + bitemporal-interval fields + hashing + `init`(+invariant+atomicity-scope) + `import`(interval backfill) + `put_config`/`seed_config` `[V3-2]` + `status` + `commit`(dual-CAS `[V3-4]`) + `log`(interval as-of `[V3-5]`) + `show` + `diff`.** Kills lost-update AND silent bypass-clobber; gives history/diff; enforces multi-doc invariant; atomicity guarantee is real or refused.
2. **`restore` single + system (valid-time-interval `--as-of` + `--tag` + `--resume`/plan_oid `[V3-10]`) + `--dry-run` + `--preview`(minimal) + `activate` `[V3-3]` + `tag`/`points` + `fsck`.** Delivers the rollback flow, correctly; activation is cfg-blessed.
3. **Approval flow (`local` then `slack`) + `cfg approve`/`deny`/`approvals` `[V3-7]` + exit-4/7 split `[V3-8]` + gating/secret pre-flight + `redact`(+`--rewrite-history` `[V3-6]`) + force-routing `[V3-9]`.** Makes prod + agents safe.
4. **Agent surface: `--json` → MCP (envelope incl. `cfg.impact`) → Claude skill.** With approval + idempotency baked in.
5. **`cfg apply-doc` shim + migrate seed scripts + CI gate + cron-adopt + `refs check` `[V3-11]`.** Adoption.
6. **LLM system-impact plugin (`cfg impact`, egress-controlled, local structural core) `[V3-LLM]` + full preview deploy + PostgresAdapter (proves the seam).**

Each stage independently useful. The §7 impact layer's *structural* analysis (blast radius, downstream readers, conflicts) can land in stage 1-2 as a local, LLM-free feature; the prose/classification arrives in stage 6.

---

## 18. Changes from v0.2 (second review: write-path defects)
The v0.2 CAS, human-approval/agent-safety, read-side keying, and the June-7 valid-time case were verified to HOLD and are unchanged. v0.3 closes the write-path edges those fixes didn't fully reach:
- **V3-1 (Tier-0)** atomicity is contingent on runtime+history+heads being co-located on one cluster; `check_atomicity_scope()` + `init`/pre-mutation assert it; gated envs refuse a split or non-transactional backend; ungated use a logged `pending`+`reconcile` fallback.
- **V3-2 (Tier-1)** `put_config` (update the unique `runtime_filter` doc in place, never insert a 2nd active) + `seed_config` (first/deleted-config insert) given real signatures + semantics.
- **V3-3 (Tier-1)** `cfg activate <id> <ref>` + `activate_config`: the cfg-blessed atomic `is_active` flip, so promoting a new active row never trips the invariant or needs a bypass.
- **V3-4 (Tier-1)** `apply` takes `expected_live_oid` and dual-CAS-checks live inside the txn → a raw-Mongo bypass fails closed (StaleLive, exit 2) instead of being silently clobbered. §14 row corrected.
- **V3-5 (Tier-1)** explicit validity **intervals** `[valid_from, valid_to)`; `apply` closes the prior interval; replaces the hand-wavy "superseded" rule with "the entry whose interval contains T," unambiguous even under backdated import.
- **V3-6 (Tier-1)** `oid` frozen under redaction; `redact` constrained to non-hashed fields so identity/FKs hold; `--rewrite-history` is the explicit, gated escape hatch for a secret in a hashed field.
- **V3-7** `cfg approve`/`deny`/`approvals`/`approval show`: the human approval surface (no MCP equivalent, by design).
- **V3-8** exit codes split: `4` needs-approval (retry) vs `7` declined (terminal); now matches the MCP envelope.
- **V3-9** `--force` on a gated env routes through §11 approval + §10 alert (force skips the CAS, never the human gate); §5.5 cross-references it.
- **V3-10** system-restore approval bound to `plan_oid`; drift invalidates; `--resume` preserves it.
- **V3-11** `cfg refs check` made a real command (used by `import --strip-backups`).
- **V3-LLM** §7 promoted from a footnote to the **system-impact layer**: nature-of-change classification + cross-config/downstream **consequences** + blast radius, structural facts computed locally (LLM-free) with the model as explainer only. Credited prior art; egress controls retained.

---

## 19. Attribution & licensing (read the licenses, give credit): **[V3-CREDITS]**
Shipped as `CREDITS.md` + `NOTICE` + `LICENSE` in the repo; summarized here so the obligations are explicit.

**What `cfg` borrows, and the actual obligation:**
- **Git: design vocabulary & the porcelain/plumbing split** (commit/log/diff/show/restore/revert/reflog/tag; stable machine layer vs human layer). Git is **GPL-2.0**. GPL-2.0 copyleft binds **copied source code**, NOT ideas, command names, or UX conventions: and Git's own docs explicitly invite building alternative "porcelains" on its interface. **Obligation: attribution + design credit; do NOT copy Git source.** `cfg` is a clean-room reimplementation of the *concepts*. (If we ever vendor a snippet of Git code, that file becomes GPL-2.0 and we isolate/label it: current plan: none.)  → credit in `CREDITS.md`.
- **Semantic/behavioral-diff prior art** for §7: *llm-prompt-semantic-diff* (embedding-% CLI), *llm-behavior-diff* (model-execution + severity + MCP server), and the *"Prompting in the Wild"* method (arXiv:2412.17298, CoT read-old+new→find-changes→flag-inconsistency, which §7 adopts for the intra-config `self_inconsistency` dimension). **Obligation: cite the methods/papers; check each repo's LICENSE before reusing any code** (most are permissive MIT/Apache, but verify per-repo at vendor time; until then we reuse only *ideas*, which needs citation, not license grant).  → credit + per-repo license note in `CREDITS.md`.
- **Standard building blocks** (sha256/canonical-JSON hashing à la git blob; bitemporal modeling from the data-warehousing literature; optimistic-concurrency/CAS) are **public concepts / common knowledge**: credited as influences, no license obligation.

**`cfg`'s own license:** target **Apache-2.0** (permissive + explicit patent grant; lets the company and others adopt freely). Apache-2.0 is compatible with depending on MIT/Apache libraries; it is NOT compatible with *linking* GPL-2.0 code into the core: which is the second reason the Git borrowing stays concept-only. The optional `cfg-impact` plugin keeps any provider SDK out of core, so a differently-licensed model SDK can't taint the core license.

**Process rule (so future borrowing stays honest):** whenever code or a non-obvious design is taken from an external project, add a `CREDITS.md` row (project, what was taken, its license, idea-vs-code) in the SAME change: actually open and read that project's LICENSE before reuse, and isolate anything copyleft. This is itself a `cfg` repo convention, enforced in review.

---

## 20. Changes from v0.3 (third review: residual write-path edges)
The third adversarial pass verified v0.3's dual-CAS [V3-4], redact-vs-oid [V3-6] (incl. git-linkage, which survives because trailers key on `seq` not `oid`), and txn co-location [V3-1] all HOLD. v0.3.1 closes the four Tier-1 edges it surfaced plus the minor items:
- **V3.1-1 (Tier-1)** `put_config` self-induced drift: mandate the committed/put doc is the FULL effective doc; staging always materializes the whole doc (`cfg set` pulls+patches+stages whole, `--from` rejects partial); any runtime field outside `ignore_*` is a config error (init/fsck). `{fields written}`/`{fields hashed}` now reconcile, so cfg can't trip its own dirty test / false StaleLive. (§2.1, §4 Field-set invariant.)
- **V3.1-2 (Tier-1)** backdated-import interval overlap: import now does a proper interval SPLIT, closing the PRIOR entry's `valid_to` at the insertion point (not just the new entry's forward edge); `fsck` gains a per-config interval overlap/gap check. (§5.8.)
- **V3.1-3 (Tier-1)** `cfg activate` now explicitly routes through `apply()` with the dual-CAS (`expected_head_oid` + `expected_live_oid`): no write path skips the live-CAS. (§5.17.)
- **V3.1-4 (Tier-1)** system-wide impact egress: a config's TEXT crosses egress only if it is itself allowlisted; cross-config narration sends only declared edge METADATA (field names + consuming config_ids), never a non-allowlisted neighbor's text; the consent line lists exactly which config_ids will be sent. (§7.5.)
- **V3.1-5 (Tier-2)** non-atomic fallback stamps `put_confirmed` so `reconcile` rolls forward on a confirmed put even if `live` later diverged (no roll-back of a served value). (§2 transaction requirement.)
- **V3.1-6 (Tier-2)** impact severity is keyed solely off LOCAL structural detection; the LLM nature-label is descriptive only, never a severity input → severity is deterministic. (§7.5.)
- **V3.1-7 (Tier-3)** §11 now states the `denied`→exit 7 / `status:"declined"` mapping explicitly (was only in §5/§5.18/§12/§14).
- **V3.1-8 (Tier-2)** `redact --rewrite-history` ordering/atomicity specified: ascending-`seq` re-chain, idempotent + `--resume`-able, fsck-verified. (§5.16.)

### v0.3.2: exit-gate patch (the one Tier-1 the final check found)
The exit-gate review confirmed [V3.1-2/3/4] close cleanly and Tier-0 is clear, and found ONE contained Tier-1 hole the [V3.1-1] edit introduced, plus two cosmetic nits. All patched:
- **V3.1-1b (Tier-1)** `strip_on_store` was omitted from the new field-set invariant's allowed set AND from the hash-time `strip()`. A secret field (the §9-sanctioned `strip_on_store` destination) would therefore (a) be falsely flagged as an "unversioned field not in ignore_*" at init/fsck, and (b) break the dirty test: `oid(strip(live))` keeps the secret while `HEAD.oid` was hashed from a stored doc that dropped it, so the config reads as permanently DIRTY. Fix: include `strip_on_store` in both the field-set whitelist (§4) and the hash-time strip (§4), so live and stored hash identically and `oid == oid(strip(stored_doc))` holds for secret-bearing configs. (§4, §2.1.)
- **V3.1-2b (Tier-3)** collapse zero-width intervals from a degenerate same-instant `import --valid-from`. (§5.8.)
- **V3.1-4b (Tier-2)** §7.5 acknowledges a config's own text may name a neighbor by design (mutually-referential contracts): bounded by the owner's own allowlist, orphan-check needs only the dangling name. (§7.5.)

**Exit-gate verdict (verbatim sense):** "YES: three of the four v0.3.1 fixes close cleanly, Tier-0 none; the fourth closed except for the contained `strip_on_store` omission, now patched. Spec is build-ready for Stage-1 (§17.1)." This was the loop's exit condition; the spec phase is complete.
