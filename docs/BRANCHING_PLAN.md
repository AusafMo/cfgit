# Branching Plan

cfgit branching is an opt-in collaboration layer for proposed datastore changes.
It is not a database checkout mechanism.

## Non-Negotiable Invariants

- `cfg branch`, `cfg switch`, branch commits, and PR creation never mutate runtime.
- `cfg pr merge` is the only v1 branch/PR command that mutates runtime.
- `main` is reserved, auto-created, and undeletable.
- Branch commits require explicit documents via `--from` or `--bulk-from`.
- Draft state is stored outside canonical runtime history.
- Stale PRs are blocked in v1. There is no rebase, cherry-pick, field-level merge,
  branch-to-branch merge, or direct apply.
- Multi-record PR merge is allowed only when the adapter can apply all touched
  records atomically.
- CLI, JSON actions, UI, MCP, and the portable skill must expose the same semantics.
- Every branch/PR action result states whether runtime was mutated.

## Opt-In Config

Default cfgit uses only:

```text
cfgit_history
cfgit_heads
```

Branching adds one cfgit-owned sidecar collection/table only when explicitly
enabled:

```toml
[branches]
enabled = true
refs_collection = "cfgit_refs"
default_branch = "main"
```

If branching is disabled, branch/PR commands fail with an actionable message:

```text
branching is not enabled. Add [branches] enabled = true and run cfg init.
```

`cfg init` creates `cfgit_refs` only when `[branches].enabled = true`.

## Storage Model

`cfgit_history` remains applied runtime truth. It contains only versions that
actually became live runtime state.

`cfgit_refs` contains typed draft/review objects:

- `type = "branch"`
- `type = "branch_commit"`
- `type = "pr"`

Branch doc:

```json
{
  "type": "branch",
  "env": "prod",
  "name": "router-test",
  "base_branch": "main",
  "head_commit_id": "bc_123",
  "created_at": "...",
  "created_by": {},
  "updated_at": "...",
  "status": "active"
}
```

Branch commit doc:

```json
{
  "type": "branch_commit",
  "env": "prod",
  "id": "bc_123",
  "branch": "router-test",
  "parent_commit_id": "bc_122",
  "message": "try cheaper routing",
  "author": {},
  "created_at": "...",
  "records": [
    {
      "collection": "agent_configs",
      "record_id": "planner",
      "base_oid": "sha256:...",
      "draft_oid": "sha256:...",
      "draft_doc": {}
    }
  ]
}
```

PR doc:

```json
{
  "type": "pr",
  "env": "prod",
  "id": "pr_7",
  "base_branch": "main",
  "head_branch": "router-test",
  "head_commit_id": "bc_123",
  "changeset_hash": "sha256:...",
  "status": "open",
  "records": [],
  "checks": {},
  "approvals": [],
  "created_at": "...",
  "merged_at": null
}
```

Indexes:

```text
(env, type, name) unique for branch
(env, type, id) unique for branch_commit and pr
(env, type, branch, created_at)
(env, type, status, updated_at)
```

## Branch Semantics

Branches are sparse draft timelines. A branch contains only records it touches.
Untouched records are implicitly inherited from `main`.

On a feature branch, `cfg commit <record> --from doc.json` writes a
`branch_commit` object and updates the branch ref. It does not call adapter
`apply()`, does not write runtime, and does not move canonical `cfgit_heads`.

On merge, cfgit writes normal applied history entries with:

```json
{
  "op": "merge",
  "meta": {
    "source_branch": "router-test",
    "source_pr": "pr_7",
    "draft_commit_id": "bc_123",
    "merge_group_id": "mg_..."
  }
}
```

## V1 Commands

```bash
cfg branch list
cfg branch create <name> --from main
cfg branch delete <name>
cfg switch <name>

cfg commit <record> --from doc.json -m "message"
cfg commit --bulk-from batch.json -m "message"

cfg diff main..<branch>
cfg impact main..<branch>
cfg --branch <branch> log

cfg pr create --base main --head <branch> -m "message"
cfg pr list
cfg pr show <id>
cfg pr close <id>
cfg pr merge <id>
```

Explicitly deferred from v1:

- `checkout`
- direct `apply`
- `rebase`
- `cherry-pick`
- branch-to-branch merge
- multi-env promotion PRs
- review comments and hosted PR workflow

If a user types `cfg checkout`, cfgit should explain:

```text
cfgit does not use checkout because branch switching never mutates runtime.
Use: cfg switch <branch>
To mutate runtime, use: cfg pr merge <pr>
```

## Merge Algorithm

`cfg pr merge <id>`:

1. Load the PR.
2. Confirm PR status is `open`.
3. Recompute the changeset hash and ensure it matches the frozen PR.
4. Confirm the head branch still points at the PR's frozen `head_commit_id`.
5. Check identity and `merge` authorization.
6. Run secret checks over all draft docs.
7. For each touched record, confirm canonical `main` HEAD still equals `base_oid`.
8. For each touched record, confirm live runtime hash still equals `base_oid`.
9. For multi-record PRs, require adapter atomicity.
10. Use the adapter batch apply path to write runtime docs, append canonical
    history entries, move canonical heads, and mark the PR merged in one
    transaction.

If any check fails, runtime remains unchanged and the result includes
`runtime_mutated = false`.

## Stale PRs

v1 merge is fast-forward-ish:

```text
main:    A ---- B
branch:  A ---- C
```

If `main` moved from `A` to `B`, PR merge from `C` is stale and blocked. v1 does
not rebase. The operator creates a fresh branch from current `main` and commits
the intended full docs again.

## Interface Contract

Every branch/PR result includes:

```json
{
  "runtime_mutated": false,
  "env": "prod",
  "branch": "router-test"
}
```

Only `cfg pr merge` can return:

```json
{
  "runtime_mutated": true
}
```

Agents must never claim runtime changed unless this flag is true.
