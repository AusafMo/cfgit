# Security

cfgit operates near live datastores. Treat configuration and environment setup as
security-sensitive.

## Supported versions

cfgit is pre-1.0. Security fixes target the main branch until release branches
exist.

## Reporting vulnerabilities

Open a private report with the maintainers if your hosting platform supports
private vulnerability reports. If not, contact the project maintainer directly
before publishing details.

Include:

- affected version or commit
- adapter and datastore
- reproduction steps
- whether live data, secrets, or write permissions are exposed

## Operational guidance

- Start with local or staging databases.
- Keep production write credentials out of `.cfg.toml`.
- Prefer `env:VAR_NAME` URI references.
- Use `secret_fields` for credentials inside versioned records.
- Run `cfg restore --dry-run` before system restore.
- Keep database backups independent of cfgit history.

cfgit is a version-control sidecar. It is not a replacement for backups,
credential management, database access control, or audit log retention.
