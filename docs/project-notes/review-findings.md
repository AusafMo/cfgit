# cfgit Implementation Review — Findings & Defects

Date: 2026-06-21. Source: four parallel adversarial reviewers reading the actual code
(concurrency/drift, history/restore, adapter parity, security). This is a defect list
for a follow-up session to fix. Nothing here is editorialized: each item has a
file:line and a concrete fix. Severities: **H** blocks handing to an external team,
**M** real bug to fix before relying on it, **L** polish/correctness-hygiene.

## What is already correct (do NOT "fix" these)
- **Dual-CAS is real and atomic** on both Mongo and Postgres: the live-doc re-hash
  happens INSIDE the transaction (`mongo.py:173-178`, `postgres.py:211-217`), so a raw
  bypass is caught as `StaleLive`, not clobbered. Concurrent commits serialize via the
  txn + unique `(env,collection,record_id,seq)` index.
- **Hashing is correct** (`hashing.py`): sorted keys, NFC, null==missing, int/float
  equal, NaN/Inf rejected; `ignore_fields`/`ignore_patterns`/`ignore_paths`/`secret_fields`
  all stripped before hashing; secrets also stripped from the stored doc.
- **adopt is correct**: folds live as a new version, `expected_live_oid=None` (waives
  live-CAS) but keeps head-CAS, idempotent no-op when already clean (`engine.py:97-123`).
- **Valid-time "back to June 7" math is real**: true `[valid_from, valid_to)` interval
  containment (`valid_from <= T < valid_to`), not a `recorded_at` approximation; returns
  exactly one version (`mongo.py:98-100`, `postgres.py:143-146`). Single-record restore
  is non-destructive by construction (restore re-applies old content as a new seq;
  identical oid across seqs is expected and handled by the unique key being seq, not oid).
- **Authz is engine-enforced**, not bypassable via MCP/UI/direct engine calls
  (`engine.py:_authorize` called first in every mutator). writers vs admins + admin_actions
  (e.g. `restore_system`) enforced (`authz.py`).
- **LLM SDK isolation is excellent**: all vendor code in `plugins/cfg_impact/` only;
  two CI gates (`test_core_purity.py`, `test_impact_boundary.py`); clean provider
  base+factory pattern. Core stays DB-free and LLM-free.

---

## HIGH (blockers for external handoff)

### H0 — `cfg status`/`log` CRASH on Mongo: `_history_row` is undefined in the Mongo adapter
- **Where:** `src/cfg/adapters/mongo.py:121` calls `_history_row(row, with_doc=with_doc)`, but `_history_row` is defined ONLY in `src/cfg/adapters/postgres.py:503` (never defined in or imported into `mongo.py`).
- **Found by:** the third-party clean-room sim (a fresh user pointing cfgit at their own Mongo). `init` and `import --all` succeed, then the very next command, `cfg status`, dies with `NameError: name '_history_row' is not defined`. This blocks core functionality on Mongo — the DEFAULT backend — for any path that reads history (status, log, system restore, tag listing). Introduced in commit `b042fe1` ("Initial public cfgit release").
- **Severity:** Highest practical severity — the tool is unusable past `import` on Mongo out of the box. (The unit suite missed it because no test exercises `query_history` against a live Mongo; see L9.)
- **Fix:** Define a Mongo `_history_row` normalizer (mirroring `postgres.py:503`) that strips the Mongo `_id`/storage internals and returns the same JSON-safe row shape `{collection, record_id, seq, oid, parent_oid, op, valid_from, valid_to, author, message, recorded_at, [doc]}`, and use it at `mongo.py:121`. This simultaneously fixes L1 (non-JSON-safe ObjectId in Mongo output). Add a live-Mongo `query_history` test (L9) so this can't regress.


### H1 — Secret pre-flight deny-list is completely unimplemented
- **Where:** `src/cfg/core/config.py:81-161` (never parses `[secrets]`), `src/cfg/core/engine.py:126-150` (`commit` does no scan), `src/cfg/cli/main.py:109-112` (no `--allow-secret`).
- **Problem:** The example config ships `[secrets]` with `block_fields`/`block_values`/`on_match="refuse"` (`examples/.cfg.toml:31-34`) and SPEC §9 mandates a deny-list scan on every commit. None of it exists in code (grep for `block_fields|block_values|on_match|allow_secret|SecretsConfig` = zero hits). A secret-shaped value (`sk-...`, AWS key) pasted into any field NOT enumerated in `secret_fields` is committed into `config_history` permanently, no refusal, no audit.
- **Fix:** Add a `SecretsConfig` dataclass (`deny_field_globs`, `deny_value_regex`, `on_match`), parse it in `load_config`, and scan `new_doc` in `Engine.commit` (and future `set --commit`) BEFORE `_entry`/`apply`. On match with `on_match="refuse"`, raise (exit 1) unless `allow_secret=True` threaded from a new `--allow-secret` CLI/MCP flag, recording `meta.allow_secret`, author, reason in the entry.

### H2 — No `share_with_ai` egress allowlist and no consent line on the LLM path
- **Where:** `src/cfg/core/config.py:49,158` (`share_with_ai` parsed but ZERO readers), `plugins/cfg_impact/cfg_impact/overview.py:73-84` (sends whenever `use_llm=True`).
- **Problem:** SPEC §7.5 requires egress to be "off by default; opt-in PER CONFIG" via `share_with_ai`, plus a consent/log line naming the provider and which record_ids' text will be sent. Neither exists. `--llm` ships full (diff-stripped) config text to Anthropic/OpenAI with no gate and no notice. Also `_overview_prompt_payload` (`overview.py:233-238`) has an inverted filter (`key not in {"changes"} or len(value) <= 40`) that KEEPS the full `changes` array (old/new field values) in the common small-diff case.
- **Fix:** Before building/sending the payload, gate the changed record's `record_id` against `engine.config.connections.share_with_ai`; if not allowlisted, return structure-only (no prose, no `changes` text). Emit a one-time-per-session consent line naming provider + exact record_ids before `llm.narrate(...)`. Fix the payload filter to drop `changes` entirely from the LLM payload (send only `changed_paths` + counts). Apply `strip_for_hash`/secret redaction at the egress boundary itself (defense-in-depth), not just upstream in `engine.diff` — `deterministic_overview` currently reads raw unstripped `=live` docs (`overview.py:35-36,131`).

### H3 — System restore cannot restore deleted (history-only) records; one deletion blocks the whole restore
- **Where:** `src/cfg/core/engine.py:271-273` inside `_restore_many`.
- **Problem:** A target whose live doc is `None` is marked `missing`/`blocked` and ABORTS the entire system restore (`:302-303`); there is no `seed_record` path. SPEC §5.11 step 4 + §14 require restoring history-only records via `seed_record` with `--include-deleted` default-true for system restore. As-is, "back to June 7" is incomplete and is actively blocked if anything was deleted since the target moment.
- **Fix:** When `live is None` but the record has history and a target at T, restore via `adapter.seed_record(...)` (method exists in base Protocol, `base.py:64`) instead of treating it as a blocker. Thread `include_deleted` default-true for system restores.

### H4 — Capability honesty is reported but never enforced; `check_atomicity_scope()` is a tautology
- **Where:** engine mutators never check capability (`engine.py:126-254`); `check_atomicity_scope()` (`mongo.py:285-293`) sets `runtime_cluster == history_cluster` from one client.
- **Problem:** SPEC §2 [FIX-3/V3-1] requires the engine to REFUSE mutating verbs on a non-transactional or non-co-located backend (and run a `pending`-intent fallback on ungated). The flag is computed honestly then ignored: on standalone Mongo, `apply` calls `start_transaction()` and throws a raw pymongo `OperationFailure` instead of a clean refusal, and there is NO non-atomic fallback (`list_pending`/`reconcile` are stubs: `mongo.py:247-251`, `postgres.py:300-303`). Separately, `check_atomicity_scope` cannot detect the cross-cluster split it exists to catch (a single `MongoClient`/`self.db` is used, so `runtime_cluster` and `history_cluster` are always equal) — the Tier-0 "never silently non-atomic" promise is unverified.
- **Fix:** (a) In a shared mutation precondition, call `check_atomicity_scope()`; if `not atomic`, raise a typed `AtomicityUnavailable` mapped to exit 3 with the spec's remedy text (refuse). (b) To make the check real, allow a separate runtime connection/URI in `.cfg.toml`, open it distinctly, and compare actual topology (replica-set name + seed hosts, or `hello().me`/host sets) of runtime vs history; set `atomic=False` with a reason naming both when they differ. Until (b), document that history/heads MUST be in the same DB as runtime (the code already assumes this).

---

## MEDIUM

### M1 — Interval close keyed on `oid` (the field the project declares non-unique) instead of `seq`
- **Where:** `src/cfg/adapters/mongo.py:193-203` (and the Postgres equivalent close step).
- **Problem:** The prior-interval close matches `{oid: current_head, valid_to: None}`. Because restore re-applies old content, an oid recurs across seqs; relying on `valid_to: None` alone to disambiguate is fragile and contradicts the project's own "oid is not unique" correction. A half-applied fallback or future path leaving two open intervals would corrupt as-of.
- **Fix:** Close by the unique key `(env, collection, record_id, seq=ptr["head_seq"])` — `apply` already holds `ptr["head_seq"]`.

### M2 — Mid-apply system-restore failure leaves a half-restored system, no report, no resume
- **Where:** `src/cfg/core/engine.py:319-322`.
- **Problem:** The apply loop has no per-record try/except; a `StaleLive`/storage error on record N propagates after 1..N-1 are already committed. No machine-readable per-record report and no `restore_token`/`--resume` (required by SPEC §5.11/§14). (There is a safe all-or-nothing pre-check, so no half-restore from a drift block; the gap is a failure DURING apply.)
- **Fix:** Wrap each `_restore_one` in try/except, accumulate `restored`/`failed` per record, return a structured report + resume token; `--resume` re-applies only non-converged records (convergence already holds via the no-op skip).

### M3 — Single-record valid-time ref `@{date}` is documented but crashes
- **Where:** `src/cfg/core/engine.py:336-337`.
- **Problem:** `@{2026-06-07}` hits the `@`-prefix branch and `int("{2026-06-07}")` raises ValueError (exit 1). README/USAGE/`core/__init__.py:12` advertise `@{date}` as valid-time. So `cfg diff <id> @{date} =live`, `cfg show <id> @{date}`, `cfg restore <id> @{date}` all crash. Valid-time as-of works only for whole-system restore, not single-record refs.
- **Fix:** Add an `@{...}` branch in `resolve_ref` that parses the date (reuse `parse_when`) and calls `query_history(as_of_valid=..., limit=1)`. Until then, remove `@{date}` from the single-record docs.

### M4 — `restore --tag` does not verify tag completeness; silently restores a partial moment
- **Where:** `src/cfg/core/engine.py:189-216`.
- **Problem:** Only tagged rows are restored; a configured record absent from the tag is silently omitted (SPEC §5.12 requires a warning). For a "known-good moment" button this can silently leave records at their drifted current state.
- **Fix:** Compute the set of configured/known records, compare against tagged keys, surface a `partial_tag` warning listing uncovered records before applying.

### M5 — Datetime/Decimal128/Int64 canonicalization can false-positive drift on raw-Mongo-written docs
- **Where:** `src/cfg/core/hashing.py:84-93`.
- **Problem:** `_normalize` renders Python `datetime`→ISO `...Z` and `Decimal`/`float`→decimal strings, but Mongo returns BSON `Date` (ms precision), `Int64`, `Decimal128` depending on writer. A doc cfg committed (Python types) vs the same doc re-read after a raw `mongosh` write can canonicalize differently → false DIRTY / false StaleLive. Since drift detection is THE differentiator and these fields are common in configs, this matters.
- **Fix:** Normalize BSON types explicitly (handle `bson.Decimal128`, `bson.Int64`, truncate datetimes to millisecond UTC to match BSON resolution) before hashing; add round-trip tests through pymongo.

### M6 — `seed_record` can insert a row that won't be found as live
- **Where:** `src/cfg/adapters/postgres.py:66-82` (uses `doc.get(key, configured_value)` per `live_when` key); `src/cfg/adapters/mongo.py:50-53` (inserts verbatim, same latent issue).
- **Problem:** If the seeded doc contains a `live_when` key with a value different from the configured live value (e.g. `is_active=false` while `live_when={is_active:true}`), the seed inserts a row that `get_record` won't see — it silently vanishes. SPEC §2 requires seed to leave exactly one runtime_filter match.
- **Fix:** On seed, force the `live_when` scalar columns (and Mongo doc keys) to the configured live values, or validate the doc satisfies `live_when` and reject otherwise.

### M7 — Postgres `apply` has no transient-retry parity with Mongo
- **Where:** `src/cfg/adapters/postgres.py:184` (no retry) vs `src/cfg/adapters/mongo.py:137-151` (3-attempt retry on `TransientTransactionError`).
- **Problem:** Under concurrent commits to the same record, Postgres surfaces a raw serialization error (`40001`)/deadlock (`40P01`) where Mongo silently retries. Not a correctness break (both fail closed), but inconsistent resilience between the two "proven" stores.
- **Fix:** Wrap PG `apply` in an equivalent retry on `40001`/`40P01`.

---

## LOW

### L1 — Mongo `query_history` returns raw storage docs with non-JSON-safe `_id`
- **Where:** `src/cfg/adapters/mongo.py:73-115` vs `src/cfg/adapters/postgres.py` `_history_row` normalizer.
- **Problem:** Postgres normalizes rows to a clean dict; Mongo returns the storage doc as-is (includes ObjectId `_id`). `log`/`list` JSON output will contain a non-serializable ObjectId on Mongo and not on Postgres.
- **Fix:** Give Mongo a `_history_row`-equivalent projection so both adapters return identical, JSON-safe row shapes.

### L2 — Heads CAS is not self-contained (relies on txn + unique index, not a conditional filter)
- **Where:** `src/cfg/adapters/mongo.py:209-225`.
- **Problem:** The heads `update_one` filter is keyed only on `(env, collection, record_id)`, not `head_oid: expected_head_oid`. Safe today via the surrounding txn + unique-seq index, but fragile defense-in-depth.
- **Fix:** Add `"head_oid": expected_head_oid` (or `{"$exists": False}` when None) to the filter and assert `matched_count == 1`, raising `StaleHead` otherwise — a true CAS that holds even outside a txn.

### L3 — Engine accepts empty `-m` reason
- **Where:** `src/cfg/core/engine.py:375-407` (`_entry`) and all mutators; CLI requires the flag present but accepts `-m ""` (`main.py:133`).
- **Fix:** Validate non-empty (stripped) message in the engine mutators, independent of interface (SPEC §10 `require_reason`).

### L4 — Config-key naming drift between SPEC §8, the loader, and the example
- **Where:** loader reads `ignore_patterns`/`ignore_paths`/`secret_fields` (`config.py:135-138`); SPEC §8 `[hash]` names them `ignore_globs`/`ignore_paths`/`strip_on_store`. Example matches the loader (friendly names), so no runtime bug, but the "authoritative 1:1 mapping in one place" promise is unmet.
- **Fix:** Reconcile loader to accept the §8 technical names (or update §8 to the friendly names) and document the alias map in one place.

### L5 — `restore_system` dry-run skips authorization (info disclosure)
- **Where:** `src/cfg/core/engine.py:164-165,196-197`.
- **Problem:** Dry-run does no writes, but an unauthorized author can enumerate which records a system restore would touch and their seqs.
- **Fix:** Authorize a read-scoped role for dry-run too, or document as intentional.

### L6 — Core-purity test doesn't cover non-core first-party packages
- **Where:** `tests/test_core_purity.py:13` scans only `src/cfg/core/`.
- **Problem:** `src/cfg/{approval,interfaces,mcp,ui}` are unguarded; clean now but a future driver/LLM import there wouldn't fail CI.
- **Fix:** Broaden the scan to all of `src/cfg/` except `src/cfg/adapters/`.

### L7 — `tag`/system restore silently skip records with no HEAD
- **Where:** `src/cfg/core/engine.py:369-370`.
- **Problem:** A live-but-unversioned (`new`) record is excluded from a system tag with no notice, so a later `restore --tag` won't touch it.
- **Fix:** Warn/report skipped `new` records at tag time.

### L8 — `git_shas` / `link_git_sha` / `activate_config` / `redact_field` from SPEC §2 are unimplemented on both adapters
- **Note:** `git_shas` is always written as `[]` (`engine.py:404`); the spec's git-linkage, activation, and redaction adapter methods don't exist in `base.py` or either adapter. Parity is preserved (equally absent), but the spec §2 surface is not the built surface. Fine for v1 (these are deferred), just recording the gap.

### L9 — No integration test runs either adapter ("proven on two stores" is proven by inspection only)
- **Where:** only `test_core_purity` and `test_authz` exist in this area; no Mongo/Postgres contract test, no fixtures.
- **Fix:** Add one parametrized adapter-contract test (same assertions, both adapters) — the artifact that would actually substantiate the headline claim.

---

## Suggested fix order (for the follow-up session)
1. **H1 secret pre-flight** (the embarrassing-in-public one: a forgotten api_key in history forever).
2. **H2 LLM egress allowlist + consent + payload filter.**
3. **H3 deleted-record system restore via seed_record** (makes "back to June 7" actually complete).
4. **H4 capability enforcement** (refuse on non-transactional backend; make `check_atomicity_scope` real).
5. **M1 close intervals by seq**, then M2-M7 as time allows.
6. **L9 add a real two-adapter contract test** to substantiate the parity claim, then the rest of the Lows.
