# Contributing

cfgit is an Apache-2.0 open-source project. Contributions are welcome.

## Development setup

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[mongo,postgres,mcp,dev]'
pip install -e plugins/cfg_impact
```

## Checks

Run these before opening a pull request:

```bash
ruff check src tests plugins/cfg_impact/cfg_impact
pytest tests/ -q
git diff --check
```

## Design constraints

- `src/cfg/core` must not import database drivers.
- `src/cfg/core` must not import LLM providers or SDKs.
- Storage-specific code belongs in `src/cfg/adapters`.
- LLM impact narration belongs in `plugins/cfg_impact`.
- The runtime datastore remains the source of truth for live reads.
- cfgit history is append-only except for explicitly designed maintenance tools.

## Testing database changes

Use local throwaway databases for adapter tests.

Mongo tests should use a local replica set, because cfgit relies on Mongo
transactions for safe apply operations.

Postgres tests should use a local Docker container and the v1 `doc jsonb` table
contract.

Do not run write tests against production databases.

## Documentation

If a change adds a user-facing command, config key, adapter behavior, or safety
rule, update the README or the relevant file in `docs/`.

## License

By contributing, you agree that your contribution is licensed under Apache-2.0.
