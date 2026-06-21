# cfgit: Core Spec (non-custodial version control for live datastores)

> This is the **core / framing** spec: what cfgit is, why it is not the existing "git for data" tools, and the narrow MVP. The exhaustive engine internals (hashing, the atomic apply, bitemporal model, approval flow, agent interface) live in `docs/SPEC.md` (v0.3.2) and are referenced, not duplicated. Where the two differ on scope, **this doc wins for v1**; SPEC.md is the deep reference for the parts v1 builds.

---

## 1. The one-line definition

**cfgit is git for a database you already have and cannot move into a new store.** It versions records *in place*, beside your live datastore, without owning the data, the reads, or the writes. The app keeps reading the same collection; people and scripts keep writing it however they do today; cfgit keeps the history, the diffs, the rollback, and reconciles changes that happened outside it.

Tagline: **a clean tool for dirty workflows.** Git that doesn't make you move in.

---

## 2. The problem (stated generally, then concretely)

**General:** a team runs a live, mutable datastore that drives production behavior. Many people (and increasingly, agents) change records directly, through many paths: the app, admin endpoints, ad-hoc scripts, a shell. Changes take effect immediately. There is no reliable history, no diff, no rollback, and no way to see who changed what or to reconcile two people's intersecting edits. When something breaks, "put it back to how it was on Tuesday" is not a button anyone can press.

**Concretely (the origin case):** an AI-video startup (flat team, everyone holds prod creds, three+ parallel workstreams that intersect) keeps its production control plane in MongoDB:
- `agent_configs`: fat ~30-field docs (instructions, tools, contracts, models, fallbacks) read live per build by the app (`persona_loader`).
- `modelgarden_models`: ~221 docs controlling provider routing, enable/disable, retry policy, pricing. Runtime and admin config paths key models by `model_path`; local dev data showed `model_id` is not unique.

Editing is via raw Mongo writes through scripts. Pain, happening **often**, not theoretically: silent overwrites between people on intersecting threads; no version history (people hand-paste `*_backup_<timestamp>` keys); invisible dev/prod and DB-vs-intended drift; painful rollback. The team will **not** lock down creds (flat, equally-accountable, startup-fast by design), so the fix cannot be access control; it must be *make-it-safe-and-reconcilable-after-the-fact*.

---

## 3. Why the existing tools do NOT solve this

There is a mature "git for data" category. **Every one of them wants to own the data.** That single property disqualifies all of them for the in-place case.

| Tool | What it is | Why it doesn't fit |
|---|---|---|
| **Dolt** | Git semantics on table rows; Prolly-tree store. Most literal "git for data." | **It IS the database.** You run Dolt instead of MySQL; data lives in Dolt. Its own docs: *"Dolt won't help you if you wish to keep your data in place."* |
| **TerminusDB** | Same idea for graph/document data. | **It IS the store.** Data lives in TerminusDB. |
| **lakeFS** | Versioning for object storage / data lakes. | Versions the lake; **is the layer in front of S3/GCS.** Not for live operational records. |
| **Pachyderm / DVC** | ML dataset + pipeline versioning. | Different problem (datasets/large files), not live records. |
| **Temporal tables** (SQL standard) | Built-in `AS OF <ts>` time-travel + instant rollback. | No diff/branch/adopt; **you must adopt that DB's feature**, and it can't reconcile out-of-band writes or span collections/systems. Closest *built-in* primitive to a slice of cfgit. |
| **Liquibase / Flyway** | Git for schema migrations. | Schema, not row data. |
| **Prompt registries** (Langfuse, LangSmith, PromptLayer, Portkey, MLflow) | Versioned prompts + serving + tracing + evals. | **They want your app to fetch from their registry/SDK.** They model a *prompt string*, not a fat control-plane doc with routing/pricing/retry. Migrating runtime reads is exactly what we refuse. |

**The gap nobody fills:** version control that sits **beside** an existing, live, third-party-owned datastore, versioning records written through *any* path, without owning the data, the writes, or the reads. That is **non-custodial** version control. cfgit is that.

**Do not position cfgit as "general database git."** That invites comparison to Dolt and loses (they have branch/merge/Prolly-trees/years of work). Position it as **non-custodial / sidecar VC for a store you already have and can't migrate.**

---

## 4. The actual differentiator: drift reconciliation

The versioning verbs (commit/diff/log/restore/tag) are commodity: Dolt, temporal tables, and a Mongo history-collection pattern each do parts for free. **The novel, load-bearing piece is reconciling writes that bypassed the tool**, because cfgit *accepts* that it does not own the writes.

- Dolt/TerminusDB never need this: nothing bypasses them; all writes go through them.
- cfgit needs it precisely because the team writes directly from 20 code paths and won't stop. So cfgit must:
  - **Detect drift**: `cfg status` sees that the live record differs from what cfgit last recorded (someone wrote the DB outside cfgit).
  - **Adopt drift**: `cfg adopt` folds the out-of-band change into history with attribution, so the trail stays honest even when the tool was bypassed.
  - **Never clobber it**: a `cfg commit` that would overwrite an un-adopted out-of-band change refuses (surfaces the drift) instead of silently winning.

This inversion: *version control designed for a store it does not control*: is the original contribution. Build toward it; it is the thing that is genuinely missing, not the commit/diff verbs.

---

## 5. What cfgit is NOT (non-goals, v1)

- **Not custodial.** cfgit never becomes the store, never proxies reads, never sits in the write path as a gateway. The app reads the live DB directly, unchanged. (This is the whole point; it is a hard non-goal to ever break it.)
- **Not a prompt/eval/experiment platform.** No playground, no evals, no datasets, no model-output scoring. (Use Langfuse/Braintrust alongside if you want that; they don't conflict.)
- **Not a feature-flag / rollout system.** No targeting, no percentage rollouts. (Use LaunchDarkly/Statsig for that.)
- **Not schema migrations.** (Liquibase/Flyway.)
- **Localhost UI only.** The UI is a thin local surface over the same actions as the CLI and MCP, not a hosted product.
- **No branch/merge in v1.** (Linear history + restore + tag is enough; branch/merge is where Dolt is strong and we are not competing.)
- **Not chasing parity with Langfuse/Dolt feature lists.** Any feature whose justification is "to match X" rather than "we bleed from not having it" is cut.

---

## 5b. What to point cfgit at (and what NOT to)

**Use cfgit for control-plane collections, not data-plane collections.** This boundary is load-bearing: pointing cfgit at the wrong kind of collection is not "a bit overkill," it is a category error that makes the tool slow, enormous, and pointless (cfgit stores a full copy of every version forever, and "roll back to a known-good state" must be a sentence someone actually says about the data). A tool that is clear about what it is *not for* is more trustworthy and more adoptable; this is the same discipline as the non-custodial framing.

**Litmus test (a collection fits when MOST of these hold):**
- **It is a control plane, not content/events.** It configures how the system *behaves*; it is not user data, generated outputs, logs, analytics, or transactional rows.
- **Low cardinality, low write rate.** Hundreds-to-low-thousands of records, changed by humans/agents *occasionally*, not millions inserted by traffic.
- **A small, shared team edits it, and edits collide.** The pain cfgit removes is people stepping on each other's curated changes.
- **"Roll it back to a known-good state" is a real operation** someone would ask for.
- **Records are hand-authored-ish**, where *who changed what and why* matters.

**Good fits (the origin collections hit all five):**
- `agent_configs` (agent behavior: instructions, tools, contracts, models).
- `modelgarden_models` (model routing: enable/disable, retry, pricing, provider config).
- Later candidates of the same shape: provider templates/manifests, routing rules, pricing tables, feature/policy config.

**NOT for (data-plane: use backups / temporal tables / a warehouse instead):**
- **User-generated content** (uploaded or generated images/video/audio, documents). High-volume, append-mostly, one-writer-per-row, no team contention, no meaningful "restore the collection to Tuesday." This is the canonical wrong fit.
- **Events / logs / analytics / metrics.** Append-only firehoses; versioning is meaningless.
- **High-write transactional tables** (orders, sessions, jobs, predictions, per-user state). Volume and write-rate break the full-copy-per-version model; these want backups or a temporal table, not a VC tool.
- **Anything where no human curates the rows.** If traffic writes it, cfgit is the wrong layer.

**Rule of thumb:** if the collection *configures the system* and a few people hand-edit it, cfgit. If it *records what the system or its users did*, not cfgit.

---

## 6. Architecture (DB-neutral by construction)

Three layers, already the shape of the existing scaffold (`src/cfg/`):

```
INTERFACES   CLI (humans) · JSON (scripts) · localhost UI · MCP/skill (agents)
CORE ENGINE  versioning, diff, history reconstruction, DRIFT DETECTION + ADOPT
             depends ONLY on StorageAdapter (+ ApprovalProvider, optional/later)
             knows NOTHING about Mongo, Postgres, or "config": operates on
             opaque JSON records keyed by a stable id
STORAGE      MongoAdapter · PostgresAdapter   (TWO at MVP, to PROVE "any DB")
```

**Inviolable:**
1. Core imports no DB driver (CI-enforced; the purity test already exists).
2. The core's unit is an **opaque JSON record** identified by a **stable id** + a **"which one is live" rule** per collection. It does not assume "config," "prompt," `is_active`, or Mongo.
3. **Capability honesty:** correctness scales to what the store can actually guarantee. The adapter declares its capabilities (transactions, atomic compare-and-swap, co-location); the engine **refuses** operations a store cannot back safely rather than silently degrading. "Works anywhere" must never quietly mean "works anywhere, sometimes wrong." (Postgres gives real ACID, so it's a safe second adapter; a non-transactional store gets the reduced, declared guarantee, not a fake one.)

> The detailed adapter contract, the atomic `apply()` (compare-and-swap on a HEAD pointer + live-record check so a raw bypass fails closed), and the history schema are specified in `docs/SPEC.md` §2–§4. v1 implements that contract for Mongo and Postgres.

**Postgres v1 runtime-table contract:** each configured live table has an id column named by `id_field`, any scalar columns referenced by `live_when`, and a `doc jsonb` column containing the full versioned record. cfgit versions the JSON document, uses the scalar columns only to find the live row, and keeps history in Postgres tables beside it. This proves the DB-neutral engine on Postgres without pretending v1 can infer arbitrary relational schemas.

---

## 7. Why two adapters at MVP (Mongo + Postgres)

One adapter builds "Mongo-git that *claims* generality." Two builds "general VC that is *proven* on two very different stores." The cost is real but contained: the core is already DB-neutral, so the second adapter is ~all-storage-layer, ~0 core change. Postgres is the right second target because its native ACID makes it the *easy* proof of the seam (it can back every guarantee the engine wants), so it validates the abstraction without fighting it. (If a non-transactional store like Firestore becomes a real target later, it slots in under the same capability-honesty rule with a declared-weaker guarantee.)

**First USE stays the origin case:** the MVP points at `agent_configs` (id: `config_id`) and `modelgarden_models` (id: `model_path`) on Mongo, so the team gets the seatbelt in week one: while the engine and the Postgres adapter prove it's not Mongo-specific.

---

## 8. The MVP (narrow feature set, general engine)

Commands and surfaces (CLI + `--json`, localhost UI, MCP), each across **all configured collections**:

- `cfg init`: set up cfgit's own history beside the live store; validate the per-collection id + live-rule against real data (catches the duplicate-id case up front).
- `cfg status`: per record: clean / changed-outside-cfgit (drift) / staged / new / missing. The drift line is the headline.
- `cfg diff <record> [a] [b]`: field-level diff between two versions, or saved-vs-live.
- `cfg commit <record> --from <file> -m "why"`: save a new full-record version. Refuses if it would clobber un-adopted drift.
- `cfg log <record>`: history: who, when, why. `cfg log --since <date>` = "what changed this week."
- `cfg restore <record> <version>`: roll one record back (non-destructive: a new version on top).
- `cfg restore --as-of <date>` / `cfg restore --tag <name>`: roll the **whole system** (all configured collections) back to a moment. This is the "put us back to June 7" button.
- `cfg tag <name>`: bookmark the current state of everything to return to by name.
- `cfg adopt <record>` / `cfg adopt --all`: fold an out-of-band change into history with attribution (the reconciliation core). Cron-friendly.
- `cfg fsck`: integrity + drift sweep.
- `cfg impact <record> [a] [b]`: deterministic system-impact overview, with optional LLM narration from the `cfg-impact` plugin.
- `cfg ui`: local web UI for the same operations.
- `cfg-mcp`: MCP server exposing the same operation envelope to agents.
- Optional mutation permissions at the same engine boundary as every write: open by default, or restricted to configured `admins` / `writers`, with admin-only actions such as `restore_system`. This is not a hosted RBAC product; it is a local safety rail for teams that want only an admin to press the system-restore button.

**Deferred (build only on a real incident or real adoption), with where they live in `docs/SPEC.md`:**
- **Approval gating / human-in-the-loop on prod** (SPEC §10–§11). Deferred because the flat team won't gate peers by default; revisit when an *agent* writes prod, or when the team actually wants a separate approval checkpoint. The v1 permission gate is local author authorization, not out-of-band approval.
- **Hosted UI / hosted approval platform.** The current UI binds locally and does not become a multi-user product.
- **Deep impact graph.** The first impact layer is deterministic path/category/reference analysis plus optional narration. Rich project-specific edge extractors stay plugin territory.
- **Config-specific furniture moves OUT of core into adapter-config/plugins:** `--strip-backups` (the `instructions_backup_*` cleanup) and the `is_active` activation verb are origin-schema features, not general VC. Keep the general core schema-agnostic.

---

## 9. Multi-collection config (the v1 shape)

The store config becomes a **list of collections**, each self-describing, instead of one hard-coded collection. Plain-language keys (per the Part-1 naming rule in `docs/SPEC.md`):

```toml
[[collection]]
name      = "agent_configs"      # the live collection the app reads
id_field  = "config_id"          # the stable id per record
live_when = { is_active = true } # which doc is "the live one" if several share an id

[[collection]]
name      = "modelgarden_models"
id_field  = "model_path"
live_when = { }                  # Runtime and admin config paths key models by model_path.
# ignore_fields / secret_fields / etc. are per-collection (see SPEC §8)

[env.dev.permissions]
mode = "open"                    # open | restricted
admins = []                      # authors with every mutation permission
writers = []                     # authors allowed to commit/adopt/tag/restore records
admin_actions = ["restore_system"]

[env.prod.permissions]
mode = "restricted"
admins = ["owner@*", "admin@*"]
writers = ["alice@*", "bob@*", "carol@*"]
admin_actions = ["init", "restore_system"]
```

> **First-task gate (archived in `docs/project-notes/handoff.md`):** before building, pin each collection's real id + live-rule against the actual data. `modelgarden_models` already has duplicate `model_id` values in local dev data, while `model_path` is unique and runtime-authoritative in the code paths checked. Use `model_path` unless production data proves otherwise. Getting this wrong makes the tool wrong on day one.

---

## 10. Honest scope + the things to keep in view

- **A slice of this is achievable for free** (Postgres temporal tables; a Mongo history-collection pattern). The part that is **not** free anywhere: and the reason to build: is: *non-custodial* (in place, not owning writes/reads), *drift reconciliation* (adopt out-of-band changes), *multi-collection / multi-store*, and *whole-system restore to a date*. Build toward those; don't reimplement what a single store gives natively.
- **Commercial value is a later discovery, not a reason to build now.** If it becomes great internally and people ask, extract it then. Treat it as internal-grade; assume not-a-product. The moment "maybe SaaS" drives a decision, it balloons. The plausible wedge, if ever: *"git-style versioning + rollback + drift-reconciliation for live config/control-plane collections, no runtime migration"*: a real niche, not an obvious SaaS.
- **The flat-team reality is a design input, not a flaw to fix.** No cred lockdown, no heavy PR ceremony, no approval-by-default. Optional author permissions should protect high-blast-radius actions without slowing normal prototyping. The tool earns adoption only if it's *faster and safer than the script*, not by mandate. The one-week usage test is the real gate: if the team reaches for it over raw writes, continue; if they route around it, stop and reassess.

---

## 11. Relationship to `docs/SPEC.md`

`SPEC.md` (v0.3.2, GREEN through four review rounds) is the **deep engine reference**: the exact StorageAdapter contract, the atomic apply with dual compare-and-swap, the bitemporal valid-time model, hashing, the approval flow, the agent interface, the edge-case table. v1 **implements the subset in §8 above** against Mongo + Postgres. The hosted approval flow and deeper graph impact remain deferred. This core doc owns **framing, positioning, non-goals, and v1 scope**; SPEC.md owns **how the built parts work in detail**.

**v1 implementation correction:** `oid` is a content hash, not a unique entry id. A restore can intentionally create a new history entry whose document content equals an older version, so `(env, collection, record_id, oid)` must be a non-unique lookup index. The unique history key is `(env, collection, record_id, seq)`. HEAD stores both `head_oid` for dirty checks and `head_seq` for exact entry resolution. Any older SPEC.md wording that says `(config_id, oid)` is unique is superseded for v1.

**Build order for v1:** core engine + history schema + hashing + drift detect + mutation permissions → MongoAdapter → `init`/`status`/`diff`/`commit`/`log`/`adopt` → `restore` (single + system) + `tag` + `fsck` → PostgresAdapter (proves the seam) → localhost UI + MCP/skill over the same action layer → `cfg-impact` plugin boundary → point at the two origin collections and run the one-week usage test. Hosted product surfaces wait for signal.
