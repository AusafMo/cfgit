# Configuration

cfgit reads `.cfg.toml` from the current directory by default. You can also pass:

```bash
cfg --config-file /path/to/.cfg.toml status
```

## Minimal shape

```toml
[project]
name = "runtime-control-plane"

[history]
history_collection = "config_history"
heads_collection = "config_heads"

[[collection]]
name = "agent_configs"
id_field = "config_id"
live_when = { is_active = true }
ignore_fields = ["_id", "is_active", "updated_at", "updated_by"]
secret_fields = []

[env.dev]
database = "mongo"
uri = "env:DEV_MONGODB_URI"
db = "my-dev-db"
needs_approval = false

[env.dev.identity]
mode = "open"

[env.dev.permissions]
mode = "open"
admins = []
writers = []
admin_actions = ["restore_system"]
```

## Project

```toml
[project]
name = "runtime-control-plane"
```

Used for display and operator context.

## History

```toml
[history]
history_collection = "config_history"
heads_collection = "config_heads"
```

For Mongo, these are collection names. For Postgres, these are table names.

## Collections

Each `[[collection]]` declares a live collection or table cfgit versions in place.

```toml
[[collection]]
name = "modelgarden_models"
id_field = "model_path"
live_when = { }
ignore_fields = ["_id", "updated_at", "updated_by"]
ignore_patterns = ["backup_*"]
ignore_paths = []
secret_fields = ["provider_config.api_key"]
```

Fields:

- `name`: live collection or table name.
- `id_field`: stable id field for the record.
- `live_when`: selector for the live record when multiple rows or docs share an id.
- `ignore_fields`: top-level fields ignored for hashing and writes.
- `ignore_patterns`: top-level glob patterns ignored for hashing.
- `ignore_paths`: reserved for path-level ignore support.
- `secret_fields`: dotted paths stripped from stored history.

## Environments

```toml
[env.dev]
database = "mongo"
uri = "env:DEV_MONGODB_URI"
db = "my-dev-db"
needs_approval = false
```

Fields:

- `database`: `mongo` or `postgres`.
- `uri`: literal URI or `env:VAR_NAME`.
- `db`: database name. Required by Mongo. Informational for Postgres.
- `needs_approval`: reserved for the future approval provider.

When `uri` is `env:DEV_MONGODB_URI`, cfgit reads that environment variable.

## Permissions

cfgit is open by default:

```toml
[env.dev.permissions]
mode = "open"
```

Restricted mode checks every mutating operation at the engine boundary:

```toml
[env.prod.permissions]
mode = "restricted"
admins = ["alice@example.com"]
writers = ["*@example.com"]
admin_actions = ["init", "restore_system"]
```

Roles:

- `writers`: can commit, adopt, tag, and restore individual records.
- `admins`: can do writer actions plus configured `admin_actions`.
- `admin_actions`: actions that require admin role, such as `restore_system`.

Patterns use shell-style matching.

## Identity

Identity controls how much cfgit trusts the `author` it records. It is
configured per environment.

```toml
[env.dev.identity]
mode = "open"
fingerprint_chars = 5

[env.prod.identity]
mode = "authenticated"
sources = ["token", "db_principal"]
token_env = "CFGIT_IDENTITY_TOKEN"
fingerprint_chars = 5
principal_map = { "alice_db" = "alice@example.com" }
tokens = [
  { author = "alice@example.com", name = "alice-main", sha256 = "sha256:0000000000000000000000000000000000000000000000000000000000000000" },
]
```

Modes:

- `open`: default. `--author`, `CFG_AUTHOR`, git email, or OS user is recorded.
  This is attribution, not authentication.
- `authenticated`: mutating cfgit operations require a verified identity. Users
  may still hold DB write credentials; bypass is detected by drift.
- `enforced`: same cfgit-side verified identity, intended for environments where
  database write credentials are also locked down to cfgit or CI.

Sources:

- `token`: cfgit reads the private human token from `token_env`, hashes the full
  value with SHA-256, and compares it to configured token hashes. Humans can use
  memorable private strings, but only the full hash is proof.
- `db_principal`: cfgit asks the adapter for the authenticated database principal.
  Postgres uses `current_user`; Mongo uses `connectionStatus` or the URI username
  when available.

The short fingerprint is display-only. It appears in `cfg whoami` and history
metadata so humans can distinguish identities; cfgit never accepts that short
value as authentication.

Generate a token hash without putting the token in shell history:

```bash
printf '%s' 'imkanyewest' | cfg identity-hash --stdin
```

## Connections

`[connections]` configures impact summaries and optional LLM narration.

```toml
[connections]
enabled = false
ai_provider = "claude"
share_with_ai = []
warn_level = "none"
links = [
  { field = "phase_contract", means = "contract other records may rely on" },
  { field = "tools", means = "shared tool list" },
]
```

Fields:

- `enabled`: reserved for deeper impact workflows.
- `ai_provider`: default provider for `cfg impact --llm`.
- `share_with_ai`: allowlist for LLM egress. Entries may be exact record ids
  (`agent_configs:agent_planner`), collection wildcards (`agent_configs:*`), or
  `*`.
- `warn_level`: reserved for future pre-save checks.
- `links`: field hints used by deterministic impact summaries.

## Author

```toml
[author]
from = "git"
```

Open-mode author resolution:

1. `--author`
2. `CFG_AUTHOR`
3. `git config user.email`
4. local OS user

In `authenticated` or `enforced` identity mode, `--author` is only a hint. If it
does not match the verified token or database principal identity, cfgit refuses
the operation.
