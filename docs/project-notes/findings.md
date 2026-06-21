# cfgit First-Task Findings

Date: 2026-06-21

## Scope Confirmation

`docs/SPEC_CORE.md` is the right current framing: cfgit is non-custodial version control for live datastore records. The backend supports that framing. Runtime behavior is controlled by live Mongo records, and both human scripts and admin/runtime code can change those records without a deploy.

No production reads or writes were performed. Local Docker Mongo was started and inspected read-only at `mongodb://localhost:27017/appdb-dev?directConnection=true`.

## Backend Runtime Reads

`agent_configs` is runtime-authoritative through `app/services/agentic_v2/persona_loader.py`.

The main loader reads:

`db["agent_configs"].find_one({"config_id": config_id, "is_active": True})`

There is no cache in that function. It returns the live Mongo doc and then applies only optional in-process dev overrides from tool context.

Additional runtime readers exist:

- `load_active_personas` reads active persona docs by `agent_type`, `is_active`, and `participates_in`.
- `deep_research_service` reads `agent_configs` with `config_id` and `is_active`.
- `director.py` reads `director_settings` by `config_id` without `is_active`.
- Many agentic and canvas services call `load_agent_config`, so the same live read path fans out broadly.
- Admin and skills endpoints can update `agent_configs` directly by `config_id`.

Finding: `agent_configs` is live control-plane data, not just prompt text.

## Seed Script Pattern

The seed and patch scripts match the handoff:

- They load `.env`.
- They use `MONGODB_URI` and `MONGODB_DB_NAME`.
- They read or write `agent_configs` directly.
- They commonly update by `config_id`.
- Several scripts own only selected fields and preserve the rest.
- There are 19 `seed_*.py` plus `backfill_*.py` files in `scripts/`.

This confirms the dirty-workflow premise: config changes can arrive through many script paths, not just one official editor.

## Local Mongo State

Docker was initially stopped. After starting Docker, the local container `tinify-jobrouter-mongo-1` was healthy and running Mongo as replica set `rs0`.

Local `appdb-dev` contains `modelgarden_models` but does not contain `agent_configs`.

Because local `agent_configs` is absent, I could not verify the live `agent_planner` document shape from local data. I did not fall back to managed Mongo. The code-level assumptions for `agent_configs` are confirmed; the data-shape confirmation remains open until a local or dev snapshot with `agent_configs` is available.

## Per-Collection Identity And Live Rules

### agent_configs

Recommended v1 identity:

`id_field = "config_id"`

Recommended live rule:

`live_when = { is_active = true }`

Why:

- The authoritative runtime loader uses `config_id` plus `is_active: true`.
- Persona fanout uses `is_active: true`.
- Seed scripts sometimes omit `is_active` in update filters, but the runtime live selector is still active-only.

Open item:

- `director_settings` is read by `config_id` without `is_active`. cfgit should either require that doc to match the collection live rule or leave it out of the first configured set until local data confirms its shape.

### modelgarden_models

Original respec said `id_field = "model_id"`. Real code and local data do not support that for v1.

Recommended v1 identity:

`id_field = "model_path"`

Recommended live rule:

`live_when = {}`

Why:

- Runtime job submission reads models by `model_path`.
- Agentic model tools and executors read by `model_path`.
- Admin config endpoints read and update by `model_path`.
- Local data has 221 docs and `model_path` has no duplicates.
- Local data has duplicate `model_id = "fal-ai-sync-lipsync-v3"` under every obvious live filter checked: none, `enabled`, `active`, `enabled + active`, `jobrouter_enabled`, and `enabled + jobrouter_enabled`.

Finding:

`model_id` is not a safe stable id for cfgit v1. It may still be useful as metadata, but the versioned record identity should be `model_path` unless production data proves otherwise.

## modelgarden_models Duplicate Finding

Local duplicate:

`model_id = "fal-ai-sync-lipsync-v3"`

Both duplicate docs are enabled, active, and jobrouter-enabled. They share the same `model_path`, but local `model_path` is unique across the collection.

The two docs differ in fields such as `is_shortlisted`, `fallback_models`, `updated_at`, and fallback metadata. This is not a harmless inactive duplicate under the currently checked filters.

## Spec Mismatches Flagged

1. `examples/.cfg.toml` should use `model_path` for `modelgarden_models.id_field`, not `model_id`.
2. `docs/SPEC_CORE.md` and `HANDOFF.md` should describe `modelgarden_models` identity as `model_path` based on current code and local data.
3. The local environment note in `HANDOFF.md` is stale: local Mongo was not running at first, but Docker can start the replica-set container successfully.
4. The `agent_configs` data-shape check is incomplete locally because the local database lacks that collection.

## Conclusion

The non-custodial framing holds. The v1 should remain narrow and general:

- Version live records in place.
- Detect out-of-band drift.
- Adopt drift into history.
- Refuse commits that would clobber unadopted drift.
- Restore one record or the configured system to a prior point.

For the origin collections, the current best v1 config is:

- `agent_configs` by `config_id`, live when `is_active = true`
- `modelgarden_models` by `model_path`, live when `{}`
