# Identity & Attribution

cfgit records who changed a versioned record, when it happened, and why. The
trust level of that "who" is explicit and configured per environment.

The short form is:

- `open`: self-asserted author, useful for cooperative teams.
- `authenticated`: cfgit verifies who used cfgit, but direct DB writes may still
  happen and are handled by drift detection.
- `enforced`: cfgit verifies identity and the database write credentials are
  locked down outside cfgit so cfgit or CI is the only writer.

cfgit cannot prevent a direct DB write by code alone. Prevention is always
database-side credentials and RBAC. cfgit detects bypass with `status`, shows it
with `diff`, and records it with `adopt`.

## Modes

### Open

Open mode is the default and remains a first-class mode. Users can pass
`--author`, set `CFG_AUTHOR`, rely on `git config user.email`, or fall back to
the OS username.

This is attribution, not authentication. It is right for local and dev workflows
where the team is cooperative and drift detection is the safety net.

```toml
[env.dev.identity]
mode = "open"
```

### Authenticated

Authenticated mode requires cfgit to verify identity before mutating history.
It does not take DB write access away; it makes cfgit's own trail trustworthy.
Bypass can still happen, and cfgit still detects it as drift.

```toml
[env.staging.identity]
mode = "authenticated"
sources = ["token", "db_principal"]
```

### Enforced

Enforced mode is the cfgit-side identity posture for production-style setups
where direct database writes are also blocked by DB credentials. The database
must be configured so humans and ad-hoc scripts do not hold write credentials
for the versioned records.

```toml
[env.prod.identity]
mode = "enforced"
sources = ["token"]
```

`enforced` does not magically close direct DB doors. It only becomes real when
the database is locked down to a cfgit service identity or CI identity.

## Token Identity

Token identity is built for private human strings:

```bash
export CFGIT_IDENTITY_TOKEN='imkanyewest'
```

cfgit hashes the full string with SHA-256 and compares it to configured hashes.
The raw token is never stored in cfgit history. The visible 4-12 character
fingerprint is only for humans to distinguish identities; it is never accepted
as proof.

Generate a hash without putting the token in shell history:

```bash
printf '%s' 'imkanyewest' | cfg identity-hash --stdin
```

Then configure the full hash:

```toml
[env.prod.identity]
mode = "authenticated"
sources = ["token"]
token_env = "CFGIT_IDENTITY_TOKEN"
fingerprint_chars = 5
tokens = [
  { author = "alice@example.com", name = "alice-main", sha256 = "sha256:..." },
]
```

Use memorable tokens only when the config containing hashes is private enough
for your risk level. If hashes are public, short or guessable phrases can be
attacked offline. Prefer longer private phrases for production.

## Database Principal Identity

`db_principal` uses the authenticated database connection identity:

- Postgres returns `current_user`.
- Mongo uses `connectionStatus` authenticated users, or the URI username when
  that is all the driver can expose.

Map database principals to author names when needed:

```toml
[env.prod.identity]
mode = "authenticated"
sources = ["db_principal"]
principal_map = { "alice_db" = "alice@example.com" }
```

This is often the cleanest route when each person already has their own DB
credential.

## Permissions

`[env.<name>.permissions]` still controls what a resolved identity can do:

```toml
[env.prod.permissions]
mode = "restricted"
admins = ["owner@example.com"]
writers = ["*@example.com"]
admin_actions = ["init", "restore_system"]
```

In `open` identity mode, role checks match the self-asserted author string. This
is a guardrail.

In `authenticated` or `enforced` identity mode, role checks use the verified
identity. If `--author` does not match the token or DB principal identity, cfgit
refuses the operation.

## History Metadata

Every new history entry includes:

```json
{
  "author": "alice@example.com",
  "meta": {
    "identity": {
      "mode": "authenticated",
      "author": "alice@example.com",
      "source": "token",
      "authenticated": true,
      "fingerprint": "abc12",
      "principal": "alice-main",
      "credential": "alice-main"
    }
  }
}
```

The top-level `author` stays simple for logs and compatibility. The nested
`identity` object tells you how trustworthy that author is.
