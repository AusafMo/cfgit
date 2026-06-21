# Storage Adapters

cfgit core talks to a `StorageAdapter` interface. Adapters own database-specific
details. Core owns hashing, history semantics, drift detection, and restore logic.

## Mongo

Install:

```bash
pip install -e '.[mongo]'
```

Configuration:

```toml
[env.dev]
database = "mongo"
uri = "env:DEV_MONGODB_URI"
db = "my-dev-db"
```

Requirements:

- MongoDB replica set or sharded cluster for transactions.
- History collections live in the same database as the runtime collections.
- Every configured collection must have a stable `id_field`.
- If multiple docs share an id, `live_when` must select exactly one live doc.

Example URI:

```bash
export DEV_MONGODB_URI='mongodb://localhost:27017/?replicaSet=rs0'
```

Mongo notes:

- `cfg init` creates indexes on history and heads collections.
- `cfg commit` uses a transaction to write history, update HEAD, and update the live doc.
- `cfg status` hashes the live doc after ignored and secret fields are stripped.
- `cfg commit` preserves existing secret field values when the input document omits them.

## Postgres

Install:

```bash
pip install -e '.[postgres]'
```

Configuration:

```toml
[env.pg]
database = "postgres"
uri = "env:CFGIT_POSTGRES_URI"
db = "postgres"
```

Runtime table contract:

- an id column named by `id_field`
- optional scalar columns referenced by `live_when`
- a `doc jsonb` column containing the full versioned record

Example:

```sql
CREATE TABLE agent_configs (
  config_id text PRIMARY KEY,
  is_active boolean NOT NULL,
  doc jsonb NOT NULL
);
```

Config:

```toml
[[collection]]
name = "agent_configs"
id_field = "config_id"
live_when = { is_active = true }
```

Postgres notes:

- `cfg init` creates history and heads tables if missing.
- The adapter uses ordinary transactions and row locking.
- The scalar id and `live_when` columns are updated from the JSON doc during writes.
- v1 does not infer arbitrary relational schemas. It versions the `doc jsonb` value.

## Adding another adapter

An adapter must implement `cfg.adapters.base.StorageAdapter`.

The important contract is `apply(...)`, which atomically:

1. verifies expected HEAD
2. optionally verifies expected live record hash
3. inserts a history entry
4. updates the live record if requested
5. moves HEAD

If a datastore cannot guarantee that atomicity, the adapter should report limited
capabilities and refuse unsafe operations rather than pretending they are safe.
