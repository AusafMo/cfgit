# cfg / cfgit: Session Handoff (for a fresh Claude Code session to take over)

> **You (the next Claude Code session) are taking over the `cfgit` project (CLI command `cfg`).** This file is your complete brief. Read it top to bottom, THEN do the "FIRST TASK" below (read the real backend code) before touching the build.

> ### SCOPE WAS REFRAMED — read `docs/SPEC_CORE.md` BEFORE this file's older sections.
> After this handoff was first written, the project was reframed (the user explored the prior art and landed on a sharper positioning). The current truth:
> - **cfgit is NOT "config version control" and NOT "general database git."** It is **non-custodial version control for live datastores**: git for a database you already have and cannot migrate, versioning records *in place*, without owning the data/reads/writes. Tagline: "git that doesn't make you move in." This is the defensible niche; "general DB git" invites comparison to **Dolt** (which owns the store) and loses.
> - **The differentiator is DRIFT RECONCILIATION** (`status` detects out-of-band writes, `adopt` folds them into history, `commit` refuses to clobber un-adopted drift), because the team writes the DB from many paths and will NOT lock down creds (flat, equally-accountable startup). Every existing tool avoids this because nothing bypasses them; cfgit needs it.
> - **v1 is a NARROW feature set on a GENERAL engine, with TWO adapters (Mongo + Postgres)** to prove "any DB." Build: `init`/`status`/`diff`/`commit`/`log`/`adopt`/`restore` (single + system)/`tag`/`fsck`, plus optional engine-level mutation permissions for admin-only high-blast-radius actions. CLI + JSON are the base surface; UI/MCP/skill are thin wrappers over the same actions.
> - **CURRENT IMPLEMENTATION UPDATE:** CLI + JSON now has a localhost UI, MCP server, portable skill, and an optional `cfg-impact` plugin. Out-of-band hosted approval remains deferred. The impact provider boundary lives only in `plugins/cfg_impact`, never in `src/cfg/core`.
> - **`docs/SPEC_CORE.md` is now the authority on framing + v1 scope.** `docs/SPEC.md` (v0.3.2) is the deep engine reference for the parts v1 builds; where they differ on scope, SPEC_CORE wins. Sections of THIS handoff written before the reframe (esp. §1's "config" emphasis and §2's approval-centric decisions) are superseded by SPEC_CORE where they conflict; the backend facts, constraints, env gotchas, and scaffold notes below remain accurate.
> - Commercial value is a **later discovery, not a reason to build**; assume internal-grade. The real gate is a **one-week usage test**: if the team reaches for `cfg` over raw writes, continue; if they route around it, stop and reassess.

Everything here was produced in prior sessions that designed and pressure-tested the engine; your job is to understand the real problem against the actual codebase, confirm the spec matches reality, and then implement the v1 scope in SPEC_CORE.

---

## 0. FIRST TASK (do this before anything else: do NOT skip to coding)

The spec describes a real production system secondhand. Before implementing, **read the actual code** so you understand the problem firsthand and can catch any place the spec drifted from reality. Open and read, in this order:

1. `<backend-repo>/app/services/agentic_v2/persona_loader.py`
   - Line ~92: `db["agent_configs"].find_one({"config_id": config_id, "is_active": True})`: this is the **runtime-authoritative read**. The app reads agent configs from Mongo live, per build, no cache, no deploy. THIS is why DB writes change agent behavior instantly, and why versioning has to live beside the runtime store without changing it. Confirm: is there really no cache? Is `is_active:True` always the selector? Are there other readers of `agent_configs`?
2. `<backend-repo>/scripts/seed_agent_planner_shape.py` (and skim 3-4 other `seed_*.py`)
   - This is the **de-facto prototype** of what `cfg` replaces: `find_one({config_id, is_active:True})` → diff/modify owned fields → `update_one`, prompt kept in a sibling `.txt`, idempotent, `return 2` on env-missing. Note the pattern; note the `return 2` exit-code collision the spec calls out.
   - There are **19** such scripts in `scripts/` (`seed_*.py` + `backfill_*.py`). They are the daily raw-Mongo workflow and the **adoption blocker**: `cfg` must absorb this path (the spec's `cfg apply-doc` shim + migration, §13).
3. `<backend-repo>/.env`
   - Keys (names): `MONGODB_URI`, `MONGODB_DB_NAME` (=`appdb-dev`), `LLM_PROVIDER_*`, etc. NOTE: on the current machine local Mongo (`localhost:27017`) is dead and the `env/` venv is gone: see §5 "Environment gotchas." There is **no** PREVIEW or PROD Mongo URI in `.env` yet; the spec requires distinct `DEV_/PREVIEW_/PROD_MONGODB_URI` (§8, §10).
4. Inspect the real doc shapes (read-only). Use `mongosh` against dev (NOT prod). The prod/dev managed Mongo cluster hosts both `appdb` (prod) and `appdb-dev` (dev).
   - `agent_configs`: a real `agent_planner` doc has ~30 fields: `instructions` (~28k chars), `client_config`{provider, model, temperature}, `tools`[7], `skills`, `fallback_models`, `prompt_templates`, `phase_contract`, `default_options`{max_tokens}, `timeout_s`, `version`(never bumped), `updated_by`, `metrics`, PLUS polluting `instructions_backup_<timestamp>` keys (≈15 across 5 configs: the manual versioning `cfg` replaces). Id = `config_id`; live-rule = `is_active:true`.
   - **`modelgarden_models`** (the SECOND v1 collection): ~221 docs controlling provider routing, with fields like `enabled`, `active`, `jobrouter_enabled`, `retry_policy`, `provider_config`, `inputs`, `custom_input_schema`, pricing/timeouts; only `updated_at`/`updated_by` as history-ish fields. A prior session found duplicate `model_id` values in the data. Follow-up code/data inspection found runtime and admin config paths key models by `model_path`, and local dev data has unique `model_path` values. Id = `model_path`; live-rule = `{}` unless production data proves otherwise. It is written from 20+ code paths (services, endpoints, seed scripts), not just seed scripts.
   - **PER-COLLECTION GATE (do this before building):** for EACH of the two collections, pin against real data: (a) the true stable id, (b) the "which one is live" rule (`live_when`), (c) whether the id is actually unique today. For `modelgarden_models`, current evidence points to `model_path`, not `model_id`. Getting this wrong makes the tool wrong on day one (see SPEC_CORE §9).

**After reading**: write a short `FINDINGS.md` confirming SPEC_CORE's assumptions hold against the real code/data, with the per-collection id + live-rule decided for both collections, and flag ANY mismatch (a second reader of either collection, a cache, a genuinely-multi-live id). THEN proceed to build per **SPEC_CORE §11** (NOT SPEC.md §17, which is the older config-only order).

**Hard constraints while doing this (NON-NEGOTIABLE, carried from the user):**
- **NEVER write to prod DB in any condition.** Reads only against prod; all mutations go to local or remote DEV. Ask first if a prod write ever seems needed.
- During dev work, **use the `.env` Mongo URI**; local is the default: don't ask which env unless prod is explicitly requested.
- Before any heavy/risky PROD collection mod (later, with permission), **back up the affected docs first**.
- **No em-dashes** in any user-facing text/docs/comments. Copyable text (commands/prompts) must be **bare** (no code-fence or quote wrappers, no lead-in line).
- Don't use vision/output evals; eval tool-calls, payloads, reasoning.

---

## 1. What `cfg` is (the one-paragraph problem)

A company runs an AI video-studio backend whose LLM agent configs live in a MongoDB `agent_configs` collection and are **runtime-authoritative**: the app reads them live (`persona_loader.py:92`), so a DB edit changes agent behavior instantly with no deploy. A small backend team edit these configs via **raw Mongo writes through Claude Code scripts**. That causes three pains:
1. **Silent overwrites**: no concurrency control; one dev's write clobbers another's (lost update).
2. **No history / rollback**: devs hand-paste `instructions_backup_<timestamp>` keys into the docs (≈15 across 5 configs); there is no real version history.
3. **Invisible drift**: dev vs prod, and DB-vs-what-was-intended, drift silently.

`cfg` is a **git-shaped version-control tool** for these DB-resident config docs. It keeps the runtime store "dumb" (current docs only) and puts versioning in a **tool-owned `config_history` + `config_heads`** collection. Versioning is an **emergent property** of the process: everyone uses the tool instead of raw writes. Humans use a CLI (`cfg commit`/`log`/`diff`/`restore`); **AI agents use an MCP server + Claude skill** (this is a first-class requirement: Claude/Codex must operate it "like a charm"). A standout capability: **system-impact analysis**: given a config change, report its *nature* and *consequences across the whole agent graph* (downstream contract breaks, cross-config conflicts, blast radius), computed locally with an optional LLM narrator.

The anchoring real-world ask: a teammate needed a deploy preview of the June 7 config state after a behavior regression. `cfg` must make "put the whole system back to how it was on June 7" easy (the emergent **system restore**).

---

## 2. Key architecture decisions (already made: do not re-litigate)

- **Keep instant/no-deploy behavior.** Gating adds a human checkpoint on prod, never latency; propagation stays instant in all envs.
- **Asymmetric gating:** dev ungated (95% of edits, matches today's speed); prod gated via out-of-band human approval. The load-bearing real-world control is org-level: lock down raw *prod* write creds so the cfg service identity is effectively the only prod writer (stated, out of tool scope but important).
- **Runtime store stays dumb.** All versioning lives in `config_history` (immutable entries) + `config_heads` (the CAS pointer). Hand someone the runtime collection alone and they couldn't tell versioning exists.
- **Storage-agnostic core.** Core depends ONLY on a `StorageAdapter` seam + an `ApprovalProvider` seam. Mongo first; Postgres later = one adapter class, zero core change. **CI gate: core imports no DB driver and no LLM SDK** (enforced by `tests/test_core_purity.py`, already written).
- **Full-doc-per-version** (no diff storage). Trivial at this scale; restore = copy; zero replay risk.
- **Bitemporal** model: `recorded_at` (transaction time) vs `valid_from`/`valid_to` (valid time): required so restores (old content stamped now) and backdated imports answer "what was live at T" correctly.
- **Real CAS** via a `config_heads` HEAD pointer (NOT seq-uniqueness) + an atomic `apply()` that also CAS-checks the live doc (`expected_live_oid`) so a raw-Mongo bypass fails closed instead of being clobbered.
- **Agents can propose/preview anything but applying to a gated env always crosses a human** (ApprovalProvider, out of band; no agent-suppliable flag, no `cfg.approve` MCP tool).
- **License:** Apache-2.0. Git's command vocabulary/porcelain-plumbing split is reused as *design ideas only* (GPL-2.0 binds copied code, not ideas; no Git source copied). Semantic-diff prior art is cited. See `CREDITS.md`.

What was explicitly REJECTED: building a brand-new general SaaS product (off-the-shelf prompt tools are prompt-string-centric and fight the fat-Mongo-doc shape, but we're not trying to out-build them); branching/merging; RBAC inside the tool; output/vision evals; storing diffs.

---

## 3. THE SPEC: two docs, SPEC_CORE wins on scope

- **`docs/SPEC_CORE.md`** = framing + **v1 scope + build order** (the authority for what to build). Read it first.
- **`docs/SPEC.md` (v0.3.2)** = deep engine reference for the parts v1 builds. Structure: §0 glossary · §1 architecture (3 layers + CI boundary) · §2 StorageAdapter (the one atomic `apply()`, dual-CAS, intervals, reconcile, co-location check) · §3 history schema (entry + HEAD pointer) · §4 hashing + Field-set invariant · §5 commands · §6 preview · §7 LLM impact (DEFERRED) · §8 `.cfg.toml` · §9 secrets · §10 gating (DEFERRED) · §11 approval (DEFERRED) · §12 agent/MCP (DEFERRED) · §13 adoption · §14 edge-cases · §15 non-goals · §16/§18/§20 changelogs · §17 (old config-only build order, SUPERSEDED by SPEC_CORE §11) · §19 attribution.

**v1 BUILD ORDER = `docs/SPEC_CORE.md` §11** (NOT SPEC.md §17). In short:
1. Core engine + history schema + hashing + **drift detection** (depends only on StorageAdapter; no DB driver; opaque records keyed by stable id).
2. **MongoAdapter** → `init` (validate per-collection id + live-rule against real data) → `status` (drift is the headline) → `diff` → `commit` (refuses to clobber un-adopted drift) → `log` → **`adopt`** (fold out-of-band writes into history; the differentiator).
3. `restore` (single + **system / "back to June 7"**) + `tag` + `fsck` + optional mutation permissions.
4. **PostgresAdapter** (proves the DB-neutral seam; Postgres ACID backs every guarantee cleanly).
5. Point at the two origin collections (`agent_configs` by `config_id`, `modelgarden_models` by `model_path`) and run the **one-week usage test**.

**v1 implementation correction found by smoke test:** `oid` is a content hash and can repeat when a restore re-applies old content. Do NOT make `(env, collection, record_id, oid)` unique. Use unique `(env, collection, record_id, seq)` for entry identity, store `head_seq` for exact HEAD lookup, and keep `head_oid` for dirty detection.

**Postgres v1 contract:** the Postgres adapter expects each live table to expose an id column, optional `live_when` scalar columns, and a `doc jsonb` column with the full versioned record. It is a deliberate proof of the DB-neutral opaque-record engine, not a relational-schema diff tool.

**DEFERRED (build only on a real incident / real adoption):** out-of-band approval (SPEC §10-§11), because the flat team won't gate peers by default; the agent/MCP surface (SPEC §12), after CLI core is used; the LLM system-impact layer (SPEC §7), config-specific and worth validating with a throwaway script first; config-specific furniture `--strip-backups` and `is_active` activation move to plugins, not core. CLI + JSON only; no UI; no branch/merge in v1. The v1 permission gate is local author authorization at the engine mutation boundary, not a hosted approval system.

---

## 4. The scaffold (already created: extend it, don't recreate)

The project is a **standalone git repo at `<repo>/`** (OUTSIDE the backend-repo repo, by design; 2 commits on `main`). Layout:
```
docs/SPEC.md            the spec (v0.3.1): the build contract
docs/SPEC_v0.1.md       the original v0.1 (historical, shows what the teardown found)
README.md               project intro
LICENSE                 Apache-2.0
NOTICE, CREDITS.md      attribution (git=idea-only; semantic-diff prior art cited)
pyproject.toml          cfg-vcs package; extras: mongo/cli/mcp/impact/dev; `cfg` entrypoint
.gitignore
src/cfg/__init__.py
src/cfg/core/           engine (no DB driver / no LLM SDK): hashing/asof/engine/refs TO BUILD
src/cfg/adapters/base.py    StorageAdapter Protocol (21 methods): STUB with full v0.3 surface
src/cfg/adapters/__init__.py
src/cfg/approval/base.py    ApprovalProvider Protocol: STUB
src/cfg/approval/__init__.py
src/cfg/cli/main.py     CLI entrypoint: STUB (prints "spec stage")
src/cfg/mcp/__init__.py MCP server: docstring only, TO BUILD
plugins/cfg_impact/     impact plugin (kept OUT of core for license boundary): README only
examples/.cfg.toml      real aistudio agent_configs shape, ready to copy
tests/test_core_purity.py   enforces §1 (no DB/LLM import in core): RUNS, passes
```
`src/cfg/adapters/base.py` is the most useful starting point: it already declares the full `StorageAdapter` contract (`get_config`/`put_config`/`seed_config`/`activate_config`/`apply` with `expected_head_oid`+`expected_live_oid`/`query_history`/`redact_field`/`reconcile`/`check_atomicity_scope`/...) plus the error types (`StaleHead`/`StaleLive`/`AmbiguousConfig`/`NoSuchConfig`) mapped to exit codes. Implement `MongoAdapter` against this Protocol first.

Verify the scaffold runs:
- `cd <repo> && PYTHONPATH=src python3 -c "from cfg.adapters.base import StorageAdapter; print('ok')"`
- `PYTHONPATH=src python3 -m pytest tests/ -q` (after `pip install -e '.[dev]'`)

---

## 5. Environment gotchas (this machine)

- **Local Mongo is dead** (`localhost:27017` not running) and the project `env/` venv + pymongo are gone on the current machine. For schema inspection during the prior session we used `mongosh` (available) against the remote managed Mongo cluster, READ-ONLY, against `appdb-dev`.
- The prod/dev managed Mongo cluster hosts both DBs. **Prod is `appdb`; dev is `appdb-dev`.** Touch only `appdb-dev` for any write; prod is read-only and only when explicitly needed.
- The user's global rule: standalone migration/db scripts must use the `.env` Mongo URI.
- To build/run `cfg` you'll likely set up a fresh venv and a local Mongo **replica set** (single-node RS is fine and is REQUIRED for transactions: `apply()` needs a txn; standalone Mongo makes `cfg` refuse gated mutations by design). `examples/.cfg.toml` shows the expected env var names (`DEV_/PROD_/PREVIEW_MONGODB_URI`).

---

## 6. How the spec was hardened (so you trust it)

The spec went through **three adversarial review rounds**, each revise→attack→revise:
- v0.1 → **20 defects** found. Worst three (all fixed): multi-doc-per-config_id (`is_active`) broke the one-doc model; the CAS was illusory (seq-uniqueness allows lost update); `as-of` was uni-temporal so "reproduce June 7" silently returned wrong state.
- v0.2 → second review found **6 write-path Tier-1 defects + Tier-2s** (all fixed in v0.3): txn co-location (atomicity silently false across clusters), `put_config`/`seed_config` undefined, no `is_active`-flip verb, bypass-not-caught-by-CAS, hand-wavy valid-time, redact-vs-oid identity break.
- v0.3 → third review found **4 residual Tier-1 edges** (all fixed in v0.3.1): `put_config` self-induced drift (full-doc/field-set invariant), backdated-import interval overlap (proper split), `cfg activate` missing the live-CAS, system-wide impact egress leaking a non-allowlisted neighbor's text.
- v0.3.1 → final exit-gate check found **one** contained Tier-1 item (`strip_on_store` omitted from the new field-set invariant + hash strip), now patched in **v0.3.2**. **Exit-gate is GREEN; spec is build-ready.**

Confirmed-holding across rounds (do not second-guess unless the code says otherwise): the HEAD-pointer CAS, the out-of-band human-approval/agent-safety model (strongest part), the read-side `runtime_filter` keying, the dual-CAS, redact-vs-oid (git linkage survives because trailers key on `seq` not `oid`), and the June-7 valid-time case.

---

## 7. The people / context (for tone + the impact-layer edges)

- **Maintainer**: backend + ML. **Two teammates**: applied AI team, also edit configs. **PM**: needs visibility, so read-only and log surfaces matter.
- The configs form a real graph: `agent_planner` (the ~28k-char planner), shape/fill split personas, shot-breakdown persona, intent classifiers, audio/TTS configs, etc. The impact layer's edge model (`phase_contract`, shared `tools`, `prompt_templates`, `fallback_models`, `skills`) is wired in `examples/.cfg.toml [impact].edge_fields`: but you should VALIDATE those field names against the real docs when you build §7.

---

## 8. What NOT to do

- Do **not** start implementation until you've done the §0 FIRST TASK (read the backend code) and confirmed the spec vs reality.
- Do **not** write to prod Mongo, ever, without explicit per-turn permission + a backup first.
- Do **not** copy Git source code (license boundary); reuse only the design vocabulary.
- Do **not** add output/vision evals to the impact layer.
- Do **not** put a DB driver or LLM SDK in `src/cfg/core/` (CI gate fails).
- Do **not** re-do the spec from scratch; it's been hardened three times. Improve it where the real code contradicts it, via small marked edits + a changelog entry.

---

## 9. Exit-gate verdict (the one open item from the prior session)

A final verification subagent checked whether v0.3.1's four Tier-1 fixes fully close their defects with no new Tier-0/1 hole. **Result: GREEN.** Three of the four closed cleanly; Tier-0 none; the fourth (`put_config` field-set invariant) had ONE contained Tier-1 omission: `strip_on_store` was missing from the field-set whitelist and the hash-time strip, which would falsely flag secret fields and break the dirty test for secret-bearing configs. **That has been patched in v0.3.2** (§4 + §2.1; see §20 changelog). The spec phase is therefore **DONE**: the spec is build-ready for Stage-1. Proceed to the build only after the §0 FIRST TASK (read the real backend code). There are no remaining Tier-0/Tier-1 defects.

---

## 9b. Naming convention (carried decision: keep it)
`docs/SPEC.md` is in **two layers**: **Part 1 (Using cfg)** = plain language for users + agents; **Part 2 (Internals)** = the technical spec. The rule the user set: **anything a person or agent types, reads, or edits is human-readable; engine internals stay technical.** Concretely: the `.cfg.toml` keys are plain (`live_collection`, `live_when`, `secret_fields`, `[connections].links`, `needs_approval`, `confirm_word`, ...), and the MCP wire statuses are plain (`changed_outside_cfg`, `needs_human_ok`, `was_declined`, `bad_config`). Internals keep technical names (`oid`, `recorded_at`/`valid_from`, `StorageAdapter`, `expected_live_oid`, the CAS). The 1:1 mapping between the two is in the "Naming note for the implementer" right after Part 1, and MUST live authoritatively in ONE place in code: the **config loader** (reads friendly keys → internal names) and the **MCP envelope serializer** (internal status → plain wire word). When you build, do NOT leak `edge_fields`/`runtime_filter`/`dirty` into anything a user sees; do NOT bother renaming internals. `examples/.cfg.toml` already uses the plain keys: keep it the source of truth for the user-facing shape.

## 10. TL;DR for you, the next session
1. Read this file + `docs/SPEC.md`: **Part 1 first** (plain usage), then Part 2 (internals) when you build.
2. Do the **§0 FIRST TASK**: read `persona_loader.py`, the `seed_*` scripts, `.env`, and the real `agent_configs` shape (dev, read-only). Confirm/flag spec-vs-reality. Write `FINDINGS.md`.
3. Set up a local Mongo **replica set** + venv; `pip install -e '.[dev,mongo,cli]'`; confirm `pytest tests/` passes.
4. Build **Stage 1** (§17): `MongoAdapter` against `src/cfg/adapters/base.py`, then `init`/`import`/`status`/`commit`(dual-CAS)/`log`/`diff`. Keep it usable end of stage.
5. Honor every constraint in §0 and §8.
