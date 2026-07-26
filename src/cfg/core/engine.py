# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""DB-neutral cfgit engine."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
import fnmatch
import re
import uuid
from typing import Any

from cfg.adapters.base import AtomicityUnavailable, NoSuchConfig, StorageAdapter
from cfg.core.authz import authorize_mutation
from cfg.core.config import CollectionConfig, ProjectConfig
from cfg.core.diff import diff_values, format_diff
from cfg.core.hashing import hash_doc, stored_doc, strip_for_hash
from cfg.core.identity import Identity, self_asserted_identity


@dataclass(frozen=True)
class RecordRef:
    collection: str
    record_id: str


@dataclass(frozen=True)
class StatusRow:
    collection: str
    record_id: str
    state: str
    live_oid: str | None
    head_oid: str | None
    head_seq: int | None


class SecretBlocked(ValueError):
    """A configured secret deny-list matched a document about to enter history."""


class BranchingDisabled(ValueError):
    """Branch/PR commands require [branches] enabled = true."""


class Engine:
    def __init__(
        self,
        config: ProjectConfig,
        adapter: StorageAdapter,
        *,
        env: str,
        author: str | None = None,
        identity: Identity | None = None,
    ):
        self.config = config
        self.adapter = adapter
        self.env = env
        env_cfg = config.envs[env]
        self.identity = identity or self_asserted_identity(author or "", cfg=env_cfg.identity)
        self.author = self.identity.author

    def init(self) -> dict[str, Any]:
        self._authorize("init")
        self.adapter.ensure_schema()
        return {
            "atomic": self.adapter.check_atomicity_scope(),
            "invariant_violations": self.adapter.check_runtime_invariant(),
            "branches": {
                "enabled": self.config.branches.enabled,
                "refs_collection": self.config.branches.refs_collection if self.config.branches.enabled else None,
                "default_branch": self.config.branches.default_branch,
            },
        }

    def branch_list(self) -> list[dict[str, Any]]:
        self._ensure_branching()
        default = self.config.branches.default_branch
        rows = [
            {
                "type": "branch",
                "id": default,
                "name": default,
                "base_branch": None,
                "head_commit_id": None,
                "created_at": None,
                "updated_at": None,
                "author": None,
                "message": "runtime branch",
                "runtime_mutated": False,
            }
        ]
        rows.extend(self.adapter.list_refs("branch"))
        return sorted(rows, key=lambda row: (row["name"] != default, row["name"]))

    def branch_create(self, name: str, *, from_branch: str | None = None, message: str | None = None) -> dict[str, Any]:
        self._authorize("branch")
        self._ensure_branching()
        name = _clean_branch_name(name)
        default = self.config.branches.default_branch
        base = _clean_branch_name(from_branch or default)
        if name == default:
            raise ValueError(f"{default!r} is the runtime branch and cannot be created")
        if self.adapter.get_ref("branch", name):
            raise ValueError(f"branch already exists: {name}")
        if base != default and not self.adapter.get_ref("branch", base):
            raise NoSuchConfig(f"base branch not found: {base}")
        now = self.adapter.now()
        base_ref = None if base == default else self.adapter.get_ref("branch", base)
        doc = {
            "type": "branch",
            "id": name,
            "name": name,
            "base_branch": base,
            "base_commit_id": base_ref.get("head_commit_id") if base_ref else None,
            "head_commit_id": None,
            "author": self.author,
            "message": str(message or f"create branch {name}"),
            "created_at": now,
            "updated_at": now,
            "runtime_mutated": False,
        }
        self.adapter.put_ref(doc)
        return doc

    def branch_delete(self, name: str) -> dict[str, Any]:
        self._authorize("branch")
        self._ensure_branching()
        name = _clean_branch_name(name)
        default = self.config.branches.default_branch
        if name == default:
            raise ValueError(f"{default!r} is the runtime branch and cannot be deleted")
        if not self.adapter.get_ref("branch", name):
            raise NoSuchConfig(f"branch not found: {name}")
        open_prs = self.adapter.list_refs("pr", head_branch=name, status="open")
        if open_prs:
            ids = ", ".join(str(pr["id"]) for pr in open_prs)
            raise ValueError(f"branch has open PR(s): {ids}")
        self.adapter.delete_ref("branch", name)
        return {"state": "deleted", "branch": name, "runtime_mutated": False}

    def branch_current(self, name: str | None) -> dict[str, Any]:
        self._ensure_branching()
        branch = _clean_branch_name(name or self.config.branches.default_branch)
        self._require_branch(branch)
        return {"branch": branch, "runtime_mutated": False}

    def branch_commit(
        self,
        branch: str,
        ref: RecordRef,
        doc: dict[str, Any],
        *,
        message: str,
        allow_secret: bool = False,
    ) -> dict[str, Any]:
        self._authorize("commit")
        self._ensure_branching()
        branch_ref = self._require_branch(_clean_branch_name(branch))
        if branch_ref is None:
            raise ValueError("committing to main mutates runtime; omit --branch or use cfg commit on main")
        plan = self._branch_commit_plan(
            branch_ref,
            ref,
            doc,
            message=message,
            allow_secret=allow_secret,
        )
        if plan["state"] != "ready":
            return {key: value for key, value in plan.items() if key != "branch_ref"}
        self.adapter.put_ref(plan["commit"])
        updated_branch = {**branch_ref, "head_commit_id": plan["commit"]["id"], "updated_at": plan["commit"]["created_at"]}
        self.adapter.put_ref(updated_branch)
        return {
            "state": "committed",
            "branch": branch_ref["name"],
            "commit_id": plan["commit"]["id"],
            "collection": ref.collection,
            "record_id": ref.record_id,
            "oid": plan["commit"]["oid"],
            "runtime_mutated": False,
        }

    def branch_commit_many(
        self,
        branch: str,
        items: list[tuple[RecordRef, dict[str, Any]]],
        *,
        message: str,
        allow_secret: bool = False,
    ) -> dict[str, Any]:
        self._authorize("commit")
        self._ensure_branching()
        branch_ref = self._require_branch(_clean_branch_name(branch))
        if branch_ref is None:
            raise ValueError("bulk branch commit needs a non-main branch")
        message = _require_message(message)
        if not items:
            raise ValueError("bulk branch commit needs at least one item")

        seen: set[tuple[str, str]] = set()
        plans: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for index, (ref, doc) in enumerate(items, start=1):
            key = (ref.collection, ref.record_id)
            if key in seen:
                raise ValueError(f"duplicate record in bulk branch commit: {ref.collection}:{ref.record_id}")
            seen.add(key)
            plan = self._branch_commit_plan(
                branch_ref,
                ref,
                doc,
                message=message,
                allow_secret=allow_secret,
                bulk_index=index,
                bulk_count=len(items),
            )
            if plan["state"] in {"missing", "changed_outside_cfgit"}:
                blocked.append(_branch_plan_result(plan))
            else:
                plans.append(plan)
        if blocked:
            return {"state": "blocked", "branch": branch_ref["name"], "results": [], "failed": blocked, "runtime_mutated": False}

        results = []
        last_commit_id = branch_ref.get("head_commit_id")
        updated_at = branch_ref.get("updated_at")
        for plan in plans:
            if plan["state"] == "noop":
                results.append(_branch_plan_result(plan))
                continue
            self.adapter.put_ref(plan["commit"])
            last_commit_id = plan["commit"]["id"]
            updated_at = plan["commit"]["created_at"]
            results.append(_branch_plan_result(plan, state="committed"))
        if last_commit_id != branch_ref.get("head_commit_id"):
            self.adapter.put_ref({**branch_ref, "head_commit_id": last_commit_id, "updated_at": updated_at})
        state = "noop" if all(item["state"] == "noop" for item in results) else "committed"
        return {"state": state, "branch": branch_ref["name"], "results": results, "runtime_mutated": False}

    def branch_log(self, branch: str, *, limit: int | None = 20) -> list[dict[str, Any]]:
        self._ensure_branching()
        branch_name = _clean_branch_name(branch)
        self._require_branch(branch_name)
        rows = self.adapter.list_refs("branch_commit", branch=branch_name)
        rows = sorted(rows, key=lambda row: row["created_at"], reverse=True)
        if limit is not None:
            rows = rows[:limit]
        return rows

    def branch_diff(self, range_expr: str) -> dict[str, Any]:
        self._ensure_branching()
        base, head = _parse_branch_range(range_expr, self.config.branches.default_branch)
        if base != self.config.branches.default_branch:
            raise ValueError("v1 branch diff supports only main..<branch>")
        self._require_branch(head)
        latest = self._branch_latest_by_record(head)
        records = []
        for ref, commit in sorted(latest.items(), key=lambda item: (item[0].collection, item[0].record_id)):
            coll = self.config.collection(ref.collection)
            base_doc = self._main_doc(ref)
            left = strip_for_hash(base_doc or {}, coll)
            right = strip_for_hash(commit["doc"], coll)
            changes = diff_values(left, right)
            if changes:
                records.append(
                    {
                        "collection": ref.collection,
                        "record_id": ref.record_id,
                        "base_oid": hash_doc(base_doc, coll) if base_doc else None,
                        "branch_oid": commit["oid"],
                        "commit_id": commit["id"],
                        "changes": changes,
                    }
                )
        return {"range": f"{base}..{head}", "records": records, "runtime_mutated": False}

    def pr_create(
        self,
        *,
        base: str,
        head: str,
        message: str,
    ) -> dict[str, Any]:
        self._authorize("pr")
        self._ensure_branching()
        default = self.config.branches.default_branch
        base = _clean_branch_name(base)
        head = _clean_branch_name(head)
        if base != default:
            raise ValueError("v1 PRs can target only main")
        branch_ref = self._require_branch(head)
        if branch_ref is None:
            raise ValueError("PR head must be a non-main branch")
        commits = self.branch_log(head, limit=None)
        if not commits:
            raise ValueError(f"branch has no commits: {head}")
        latest = self._branch_latest_by_record(head)
        now = self.adapter.now()
        pr_id = f"pr_{uuid.uuid4().hex[:12]}"
        doc = {
            "type": "pr",
            "id": pr_id,
            "status": "open",
            "base_branch": base,
            "head_branch": head,
            "head_commit_id": branch_ref.get("head_commit_id"),
            "commit_ids": [row["id"] for row in commits],
            "records": [
                {
                    "collection": ref.collection,
                    "record_id": ref.record_id,
                    "commit_id": commit["id"],
                    "oid": commit["oid"],
                }
                for ref, commit in sorted(latest.items(), key=lambda item: (item[0].collection, item[0].record_id))
            ],
            "message": _require_message(message),
            "author": self.author,
            "created_at": now,
            "updated_at": now,
            "runtime_mutated": False,
        }
        self.adapter.put_ref(doc)
        return doc

    def pr_list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        self._ensure_branching()
        filters = {"status": status} if status else {}
        return self.adapter.list_refs("pr", **filters)

    def pr_show(self, pr_id: str) -> dict[str, Any]:
        self._ensure_branching()
        pr = self.adapter.get_ref("pr", pr_id)
        if not pr:
            raise NoSuchConfig(f"PR not found: {pr_id}")
        return pr

    def pr_close(self, pr_id: str) -> dict[str, Any]:
        self._authorize("pr")
        self._ensure_branching()
        pr = self.pr_show(pr_id)
        if pr["status"] != "open":
            raise ValueError(f"PR is not open: {pr_id}")
        now = self.adapter.now()
        closed = {**pr, "status": "closed", "closed_at": now, "updated_at": now, "runtime_mutated": False}
        self.adapter.put_ref(closed)
        return closed

    def pr_merge(self, pr_id: str, *, message: str | None = None) -> dict[str, Any]:
        self._authorize("merge")
        self._ensure_branching()
        self._ensure_atomic("merge")
        pr = self.pr_show(pr_id)
        if pr["status"] != "open":
            raise ValueError(f"PR is not open: {pr_id}")
        if pr["base_branch"] != self.config.branches.default_branch:
            raise ValueError("v1 PR merge supports only main as base")
        branch_ref = self._require_branch(pr["head_branch"])
        if branch_ref is None:
            raise ValueError("PR head branch is main")
        if branch_ref.get("head_commit_id") != pr.get("head_commit_id"):
            raise ValueError("PR is stale: head branch moved after PR creation")
        latest = self._branch_latest_by_record(pr["head_branch"])
        if len(latest) > 1:
            raise AtomicityUnavailable(
                "multi-record PR merge needs adapter-level batch atomicity; split into one-record PRs for v1"
            )
        if not latest:
            raise ValueError("PR has no records to merge")

        ref, commit = next(iter(latest.items()))
        coll = self.config.collection(ref.collection)
        live = self.adapter.get_record(ref.collection, ref.record_id)
        if live is None:
            raise NoSuchConfig(f"{ref.collection}:{ref.record_id}")
        head = self.adapter.get_head(ref.collection, ref.record_id)
        expected_head = head.get("oid") if head else None
        expected_live = hash_doc(live, coll)
        base_head = (commit.get("meta") or {}).get("base_head_oid")
        if expected_head != base_head:
            return {
                "state": "stale",
                "reason": "main moved since branch commit",
                "collection": ref.collection,
                "record_id": ref.record_id,
                "head_oid": expected_head,
                "branch_base_oid": base_head,
                "runtime_mutated": False,
            }
        if head and expected_live != expected_head:
            return {
                "state": "changed_outside_cfgit",
                "collection": ref.collection,
                "record_id": ref.record_id,
                "live_oid": expected_live,
                "head_oid": expected_head,
                "runtime_mutated": False,
            }
        entry = self._entry(
            ref,
            commit["doc"],
            coll,
            message=message or pr["message"],
            op="merge",
            parent_oid=expected_head,
            meta={
                "source_pr_id": pr_id,
                "source_branch": pr["head_branch"],
                "source_branch_commit_id": commit["id"],
                "source_branch_oid": commit["oid"],
            },
        )
        result = self.adapter.apply(
            collection=ref.collection,
            record_id=ref.record_id,
            new_doc=commit["doc"],
            entry=entry,
            expected_head_oid=expected_head,
            expected_live_oid=expected_live,
            make_head=True,
        )
        now = self.adapter.now()
        merged_pr = {
            **pr,
            "status": "merged",
            "merged_at": now,
            "updated_at": now,
            "merge_result": {
                "collection": result.collection,
                "record_id": result.record_id,
                "seq": result.seq,
                "oid": result.oid,
            },
            "runtime_mutated": True,
        }
        self.adapter.put_ref(merged_pr)
        return {"state": "merged", "pr": pr_id, **merged_pr["merge_result"], "runtime_mutated": True}

    def status(self, ref: RecordRef | None = None) -> list[StatusRow]:
        refs = [ref] if ref else self._all_refs(include_history=True)
        rows: list[StatusRow] = []
        for item in refs:
            coll = self.config.collection(item.collection)
            live = self.adapter.get_record(item.collection, item.record_id)
            head = self.adapter.get_head(item.collection, item.record_id)
            live_oid = hash_doc(live, coll) if live else None
            head_oid = head.get("oid") if head else None
            head_seq = head.get("seq") if head else None
            if live is None and head is None:
                state = "not_found"
            elif live is None:
                state = "missing"
            elif head is None:
                state = "new"
            elif live_oid != head_oid:
                state = "changed_outside_cfgit"
            else:
                state = "clean"
            rows.append(StatusRow(item.collection, item.record_id, state, live_oid, head_oid, head_seq))
        return sorted(rows, key=lambda r: (r.collection, r.record_id))

    def import_records(
        self,
        ref: RecordRef | None,
        *,
        message: str,
        allow_secret: bool = False,
    ) -> list[dict[str, Any]]:
        self._authorize("import")
        self._ensure_atomic("import")
        refs = [ref] if ref else self._all_refs(include_history=False)
        results: list[dict[str, Any]] = []
        for item in refs:
            if self.adapter.get_head(item.collection, item.record_id):
                results.append({"collection": item.collection, "record_id": item.record_id, "state": "exists"})
                continue
            live = self.adapter.get_record(item.collection, item.record_id)
            if live is None:
                results.append({"collection": item.collection, "record_id": item.record_id, "state": "missing"})
                continue
            coll = self.config.collection(item.collection)
            meta = self._secret_meta(live, coll, allow_secret=allow_secret, message=message)
            entry = self._entry(item, live, coll, message=message, op="import", parent_oid=None, meta=meta)
            result = self.adapter.apply(
                collection=item.collection,
                record_id=item.record_id,
                new_doc=None,
                entry=entry,
                expected_head_oid=None,
                expected_live_oid=None,
                make_head=True,
            )
            results.append({"collection": item.collection, "record_id": item.record_id, "state": "imported", "seq": result.seq, "oid": result.oid})
        return results

    def doctor(self, ref: RecordRef | None = None, *, large_field_bytes: int = 20000) -> dict[str, Any]:
        """Read-only preflight. Walks live records and reports what would trip an
        import/commit BEFORE anything is written: secret-deny matches (grouped by
        field path), oversized fields, and id values that are not unique under
        live_when. Writes nothing. Returns a structured report plus paste-ready
        config snippets so the user can fix .cfg.toml in one pass.
        """
        refs = [ref] if ref else self._all_refs(include_history=False)
        # group secret hits by (collection, field path) so 200 docs with the same
        # schema field collapse to one actionable line.
        secret_groups: dict[tuple[str, str, str], dict[str, Any]] = {}
        large_groups: dict[tuple[str, str], dict[str, Any]] = {}
        invariant_violations = self.adapter.check_runtime_invariant(ref.collection if ref else None)
        scanned = 0
        for item in refs:
            live = self.adapter.get_record(item.collection, item.record_id)
            if live is None:
                continue
            scanned += 1
            coll = self.config.collection(item.collection)
            scan = stored_doc(live, coll)  # same view the secret scan + storage use
            for match in _secret_matches(scan, self.config.secrets.block_fields, self.config.secrets.block_values):
                # normalize list indices so foo[0].bar and foo[3].bar group together
                norm = re.sub(r"\[\d+\]", "[]", match["path"])
                key = (item.collection, norm, match["kind"])
                g = secret_groups.setdefault(key, {"collection": item.collection, "path": norm,
                                                    "kind": match["kind"], "pattern": match["pattern"],
                                                    "count": 0, "example": item.record_id})
                g["count"] += 1
            for path, value in _walk_doc(scan):
                size = len(value) if isinstance(value, (str, bytes)) else 0
                if size >= large_field_bytes:
                    norm = re.sub(r"\[\d+\]", "[]", path)
                    key = (item.collection, norm)
                    g = large_groups.setdefault(key, {"collection": item.collection, "path": norm,
                                                       "count": 0, "max_bytes": 0, "example": item.record_id})
                    g["count"] += 1
                    g["max_bytes"] = max(g["max_bytes"], size)
        secrets = sorted(secret_groups.values(), key=lambda g: (g["collection"], g["path"]))
        large = sorted(large_groups.values(), key=lambda g: (-g["max_bytes"], g["collection"]))
        # build paste-ready fix snippets per collection. Roll each secret path UP to its
        # secret-bearing container so 9 sub-paths under ...openai_api_key collapse to the
        # one container that, stripped, removes them all. Detailed paths stay in
        # secret_blocks for transparency; suggestions stay short and pasteable.
        suggestions: dict[str, dict[str, list[str]]] = {}
        for g in secrets:
            container = _secret_container(g["path"], self.config.secrets.block_fields)
            sf = suggestions.setdefault(g["collection"], {}).setdefault("secret_fields", [])
            if container not in sf:
                sf.append(container)
        for g in large:
            ig = suggestions.setdefault(g["collection"], {}).setdefault("ignore_fields", [])
            if g["path"] not in ig:
                ig.append(g["path"])
        # drop any suggested secret_field that is a child of another suggested one
        for entry in suggestions.values():
            sf = entry.get("secret_fields")
            if sf:
                entry["secret_fields"] = [p for p in sf
                                          if not any(p != q and p.startswith(q + ".") for q in sf)]
        ok = not secrets and not large and not invariant_violations
        return {
            "ok": ok,
            "scanned": scanned,
            "secret_blocks": secrets,
            "large_fields": large,
            "key_issues": invariant_violations,
            "suggestions": suggestions,
            "large_field_bytes": large_field_bytes,
        }

    def adopt(self, ref: RecordRef, *, message: str, allow_secret: bool = False) -> dict[str, Any]:
        self._authorize("adopt")
        self._ensure_atomic("adopt")
        live = self.adapter.get_record(ref.collection, ref.record_id)
        if live is None:
            raise NoSuchConfig(f"{ref.collection}:{ref.record_id}")
        coll = self.config.collection(ref.collection)
        live_oid = hash_doc(live, coll)
        head = self.adapter.get_head(ref.collection, ref.record_id)
        if head and head.get("oid") == live_oid:
            return {"state": "clean", "oid": live_oid, "seq": head.get("seq")}
        meta = {
            "bypass_detected_oid": live_oid,
            **self._secret_meta(live, coll, allow_secret=allow_secret, message=message),
        }
        entry = self._entry(
            ref,
            live,
            coll,
            message=message,
            op="adopt",
            parent_oid=head.get("oid") if head else None,
            meta=meta,
        )
        result = self.adapter.apply(
            collection=ref.collection,
            record_id=ref.record_id,
            new_doc=None,
            entry=entry,
            expected_head_oid=head.get("oid") if head else None,
            expected_live_oid=None,
            make_head=True,
        )
        return {"state": "adopted", "oid": result.oid, "seq": result.seq}

    def commit(
        self,
        ref: RecordRef,
        doc: dict[str, Any],
        *,
        message: str,
        allow_secret: bool = False,
    ) -> dict[str, Any]:
        self._authorize("commit")
        self._ensure_atomic("commit")
        coll = self.config.collection(ref.collection)
        live = self.adapter.get_record(ref.collection, ref.record_id)
        if live is None:
            raise NoSuchConfig(f"{ref.collection}:{ref.record_id}")
        head = self.adapter.get_head(ref.collection, ref.record_id)
        expected_head = head.get("oid") if head else None
        expected_live = hash_doc(live, coll)
        if head and expected_live != expected_head:
            return {"state": "changed_outside_cfgit", "live_oid": expected_live, "head_oid": expected_head}
        new_oid = hash_doc(doc, coll)
        if head and new_oid == expected_head:
            return {"state": "noop", "oid": new_oid, "seq": head.get("seq")}
        meta = self._secret_meta(doc, coll, allow_secret=allow_secret, message=message)
        entry = self._entry(ref, doc, coll, message=message, op="commit", parent_oid=expected_head, meta=meta)
        result = self.adapter.apply(
            collection=ref.collection,
            record_id=ref.record_id,
            new_doc=doc,
            entry=entry,
            expected_head_oid=expected_head,
            expected_live_oid=expected_live,
            make_head=True,
        )
        return {"state": "committed", "oid": result.oid, "seq": result.seq}

    def commit_preview(
        self,
        ref: RecordRef,
        doc: dict[str, Any],
        *,
        allow_secret: bool = False,
    ) -> dict[str, Any]:
        """Dry-run of `commit` on the main-branch path: same guard order, no write.

        Returns `changed_outside_cfgit` / `noop` exactly as `commit` would, so the operator
        sees a refusal *before* attempting the write; otherwise returns `would_commit` with the
        field-level delta. Runs the secret scan so a `SecretBlocked` surfaces here too.
        """
        self._authorize("commit")
        coll = self.config.collection(ref.collection)
        live = self.adapter.get_record(ref.collection, ref.record_id)
        if live is None:
            raise NoSuchConfig(f"{ref.collection}:{ref.record_id}")
        head = self.adapter.get_head(ref.collection, ref.record_id)
        expected_head = head.get("oid") if head else None
        expected_live = hash_doc(live, coll)
        if head and expected_live != expected_head:
            return {"state": "changed_outside_cfgit", "live_oid": expected_live, "head_oid": expected_head}
        new_oid = hash_doc(doc, coll)
        if head and new_oid == expected_head:
            return {"state": "noop", "oid": new_oid, "seq": head.get("seq")}
        # secret policy must fire in preview too, so it is not a surprise at commit time
        self._secret_meta(doc, coll, allow_secret=allow_secret, message="(dry-run)")
        changes = diff_values(strip_for_hash(live, coll), strip_for_hash(doc, coll))
        return {
            "state": "would_commit",
            "changes": changes,
            "text": format_diff(changes),
            "new_oid": new_oid,
            "head_oid": expected_head,
        }

    def commit_many(
        self,
        items: list[tuple[RecordRef, dict[str, Any]]],
        *,
        message: str,
        allow_secret: bool = False,
    ) -> dict[str, Any]:
        """Commit multiple full documents as one operator intent.

        The current adapter contract is per-record atomic (`apply()`), so this method does
        not pretend the whole batch is one database transaction. It does, however, preflight
        every target before writing anything. Existing drift, missing records, duplicate
        targets, and secret-policy failures block the entire batch up front.
        """
        self._authorize("commit")
        self._ensure_atomic("commit")
        message = _require_message(message)
        if not items:
            raise ValueError("bulk commit needs at least one item")

        seen: set[tuple[str, str]] = set()
        plans: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        total = len(items)
        for index, (ref, doc) in enumerate(items, start=1):
            key = (ref.collection, ref.record_id)
            if key in seen:
                raise ValueError(f"duplicate record in bulk commit: {ref.collection}:{ref.record_id}")
            seen.add(key)
            plan = self._commit_plan(
                ref,
                doc,
                message=message,
                allow_secret=allow_secret,
                bulk_index=index,
                bulk_count=total,
            )
            if plan["state"] in {"missing", "changed_outside_cfgit"}:
                blocked.append(_plan_result(plan))
            else:
                plans.append(plan)

        if blocked:
            return {"state": "blocked", "results": [], "failed": blocked}

        results: list[dict[str, Any]] = []
        for offset, plan in enumerate(plans):
            if plan["state"] == "noop":
                results.append(_plan_result(plan))
                continue
            try:
                result = self.adapter.apply(
                    collection=plan["ref"].collection,
                    record_id=plan["ref"].record_id,
                    new_doc=plan["doc"],
                    entry=plan["entry"],
                    expected_head_oid=plan["expected_head"],
                    expected_live_oid=plan["expected_live"],
                    make_head=True,
                )
            except Exception as exc:
                failed = [
                    {
                        "collection": plan["ref"].collection,
                        "record_id": plan["ref"].record_id,
                        "state": "failed",
                        "error": str(exc),
                        "error_type": exc.__class__.__name__,
                    }
                ]
                pending = [_plan_result(p) for p in plans[offset + 1:]]
                return {
                    "state": "partial",
                    "results": results,
                    "failed": failed,
                    "pending": pending,
                }
            results.append(
                {
                    "collection": result.collection,
                    "record_id": result.record_id,
                    "state": "committed",
                    "seq": result.seq,
                    "oid": result.oid,
                }
            )

        state = "noop" if all(item["state"] == "noop" for item in results) else "committed"
        return {"state": state, "results": results}

    def _commit_plan(
        self,
        ref: RecordRef,
        doc: dict[str, Any],
        *,
        message: str,
        allow_secret: bool,
        bulk_index: int | None = None,
        bulk_count: int | None = None,
    ) -> dict[str, Any]:
        coll = self.config.collection(ref.collection)
        live = self.adapter.get_record(ref.collection, ref.record_id)
        if live is None:
            return {"ref": ref, "state": "missing"}
        head = self.adapter.get_head(ref.collection, ref.record_id)
        expected_head = head.get("oid") if head else None
        expected_live = hash_doc(live, coll)
        if head and expected_live != expected_head:
            return {
                "ref": ref,
                "state": "changed_outside_cfgit",
                "live_oid": expected_live,
                "head_oid": expected_head,
            }
        new_oid = hash_doc(doc, coll)
        if head and new_oid == expected_head:
            return {"ref": ref, "state": "noop", "oid": new_oid, "seq": head.get("seq")}
        meta = self._secret_meta(doc, coll, allow_secret=allow_secret, message=message)
        if bulk_index is not None and bulk_count is not None:
            meta = {**meta, "bulk_commit": {"index": bulk_index, "count": bulk_count}}
        entry = self._entry(ref, doc, coll, message=message, op="commit", parent_oid=expected_head, meta=meta)
        return {
            "ref": ref,
            "doc": doc,
            "entry": entry,
            "expected_head": expected_head,
            "expected_live": expected_live,
            "state": "ready",
        }

    def _branch_commit_plan(
        self,
        branch_ref: dict[str, Any],
        ref: RecordRef,
        doc: dict[str, Any],
        *,
        message: str,
        allow_secret: bool,
        bulk_index: int | None = None,
        bulk_count: int | None = None,
    ) -> dict[str, Any]:
        coll = self.config.collection(ref.collection)
        live = self.adapter.get_record(ref.collection, ref.record_id)
        if live is None:
            return {"branch_ref": branch_ref, "ref": ref, "state": "missing"}
        head = self.adapter.get_head(ref.collection, ref.record_id)
        expected_head = head.get("oid") if head else None
        expected_seq = head.get("seq") if head else None
        expected_live = hash_doc(live, coll)
        if head and expected_live != expected_head:
            return {
                "branch_ref": branch_ref,
                "ref": ref,
                "state": "changed_outside_cfgit",
                "live_oid": expected_live,
                "head_oid": expected_head,
            }
        latest = self._branch_latest_for_record(branch_ref["name"], ref)
        parent_oid = latest.get("oid") if latest else expected_head
        new_oid = hash_doc(doc, coll)
        if new_oid == parent_oid:
            return {
                "branch_ref": branch_ref,
                "ref": ref,
                "state": "noop",
                "oid": new_oid,
                "commit_id": latest.get("id") if latest else None,
            }
        meta = {
            "identity": self.identity.history_meta(),
            "base_head_oid": expected_head,
            "base_head_seq": expected_seq,
            "base_live_oid": expected_live,
            "runtime_mutated": False,
            **self._secret_meta(doc, coll, allow_secret=allow_secret, message=message),
        }
        if bulk_index is not None and bulk_count is not None:
            meta["bulk_commit"] = {"index": bulk_index, "count": bulk_count}
        now = self.adapter.now()
        commit_id = f"bc_{uuid.uuid4().hex[:16]}"
        return {
            "branch_ref": branch_ref,
            "ref": ref,
            "state": "ready",
            "commit": {
                "type": "branch_commit",
                "id": commit_id,
                "branch": branch_ref["name"],
                "collection": ref.collection,
                "record_id": ref.record_id,
                "oid": new_oid,
                "parent_oid": parent_oid,
                "parent_commit_id": latest.get("id") if latest else None,
                "doc": stored_doc(doc, coll),
                "message": _require_message(message),
                "author": self.author,
                "created_at": now,
                "updated_at": now,
                "meta": meta,
                "runtime_mutated": False,
            },
        }

    def restore(self, ref: RecordRef, target_ref: str, *, message: str) -> dict[str, Any]:
        self._authorize("restore")
        self._ensure_atomic("restore")
        target = self.resolve_ref(ref, target_ref)
        return self._restore_one(ref, target, message=message, restored_from=target_ref)

    def restore_system_as_of(
        self,
        when: datetime,
        *,
        message: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        self._authorize("restore_system")
        if not dry_run:
            self._ensure_atomic("restore_system")
        targets: list[tuple[RecordRef, dict[str, Any]]] = []
        missing: list[dict[str, Any]] = []
        for ref in self._all_refs(include_history=True):
            rows = self.adapter.query_history(
                collection=ref.collection,
                record_id=ref.record_id,
                as_of_valid=when,
                limit=1,
                order="desc",
                with_doc=True,
            )
            if rows:
                targets.append((ref, rows[0]))
            else:
                missing.append({"collection": ref.collection, "record_id": ref.record_id, "state": "did_not_exist"})
        return self._restore_many(
            targets,
            message=message,
            dry_run=dry_run,
            source=f"as-of {when.isoformat()}",
            extra=missing,
        )

    def restore_system_tag(
        self,
        tag: str,
        *,
        message: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        self._authorize("restore_system")
        if not dry_run:
            self._ensure_atomic("restore_system")
        rows = self.adapter.query_history(tag=tag, limit=None, order="desc", with_doc=True)
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (row["collection"], row["record_id"])
            if key not in latest or int(row["seq"]) > int(latest[key]["seq"]):
                latest[key] = row
        if not latest:
            raise NoSuchConfig(f"tag not found: {tag}")
        targets = [
            (RecordRef(collection, record_id), row)
            for (collection, record_id), row in sorted(latest.items())
        ]
        tagged_keys = set(latest)
        known_keys = {(ref.collection, ref.record_id) for ref in self._all_refs(include_history=True)}
        uncovered = [
            {"collection": collection, "record_id": record_id, "state": "not_in_tag", "tag": tag}
            for collection, record_id in sorted(known_keys - tagged_keys)
        ]
        return self._restore_many(
            targets,
            message=message,
            dry_run=dry_run,
            source=f"tag:{tag}",
            extra=uncovered,
        )

    def _restore_one(
        self,
        ref: RecordRef,
        target: dict[str, Any],
        *,
        message: str,
        restored_from: str,
        seed_missing: bool = False,
    ) -> dict[str, Any]:
        coll = self.config.collection(ref.collection)
        live = self.adapter.get_record(ref.collection, ref.record_id)
        if live is None and not seed_missing:
            raise NoSuchConfig(f"{ref.collection}:{ref.record_id}")
        head = self.adapter.get_head(ref.collection, ref.record_id)
        expected_head = head.get("oid") if head else None
        expected_live = hash_doc(live, coll) if live is not None else None
        if live is not None and head and expected_live != expected_head:
            return {"state": "changed_outside_cfgit", "live_oid": expected_live, "head_oid": expected_head}
        doc = target["doc"]
        entry = self._entry(
            ref,
            doc,
            coll,
            message=message,
            op="restore",
            parent_oid=expected_head,
            meta={"restored_from": restored_from},
        )
        result = self.adapter.apply(
            collection=ref.collection,
            record_id=ref.record_id,
            new_doc=doc,
            entry=entry,
            expected_head_oid=expected_head,
            expected_live_oid=expected_live,
            make_head=True,
            seed_missing=seed_missing,
        )
        return {
            "state": "restored_deleted" if seed_missing else "restored",
            "oid": result.oid,
            "seq": result.seq,
        }

    def _restore_many(
        self,
        targets: list[tuple[RecordRef, dict[str, Any]]],
        *,
        message: str,
        dry_run: bool,
        source: str,
        extra: list[dict[str, Any]],
    ) -> dict[str, Any]:
        blocked: list[dict[str, Any]] = []
        plan: list[tuple[RecordRef, dict[str, Any], str, bool]] = []
        results: list[dict[str, Any]] = list(extra)

        for ref, target in targets:
            coll = self.config.collection(ref.collection)
            live = self.adapter.get_record(ref.collection, ref.record_id)
            head = self.adapter.get_head(ref.collection, ref.record_id)
            expected_head = head.get("oid") if head else None
            expected_live = hash_doc(live, coll) if live is not None else None
            seed_missing = live is None
            if live is not None and head and expected_live != expected_head:
                blocked.append(
                    {
                        "collection": ref.collection,
                        "record_id": ref.record_id,
                        "state": "changed_outside_cfgit",
                        "live_oid": expected_live,
                        "head_oid": expected_head,
                    }
                )
                continue
            if live is not None and expected_head == target["oid"]:
                results.append(
                    {
                        "collection": ref.collection,
                        "record_id": ref.record_id,
                        "state": "unchanged",
                        "seq": head.get("seq") if head else None,
                        "oid": expected_head,
                    }
                )
                continue
            plan.append((ref, target, f"{ref.collection}:{ref.record_id}@{target['seq']}", seed_missing))

        if blocked:
            return {"state": "blocked", "source": source, "blocked": blocked, "results": results}

        if dry_run:
            preview = [
                {
                    "collection": ref.collection,
                    "record_id": ref.record_id,
                    "state": "would_seed" if seed_missing else "would_restore",
                    "from": restored_from,
                    "oid": target["oid"],
                    "seq": target["seq"],
                }
                for ref, target, restored_from, seed_missing in plan
            ]
            return {"state": "dry_run", "source": source, "results": results + preview}

        failed: list[dict[str, Any]] = []
        for ref, target, restored_from, seed_missing in plan:
            try:
                result = self._restore_one(
                    ref,
                    target,
                    message=message,
                    restored_from=restored_from,
                    seed_missing=seed_missing,
                )
                results.append({"collection": ref.collection, "record_id": ref.record_id, **result})
            except Exception as exc:
                failed.append(
                    {
                        "collection": ref.collection,
                        "record_id": ref.record_id,
                        "state": "failed",
                        "error": str(exc),
                        "error_type": exc.__class__.__name__,
                        "from": restored_from,
                    }
                )
        if failed:
            return {
                "state": "partial",
                "source": source,
                "results": results,
                "failed": failed,
                "resume_token": {
                    "source": source,
                    "pending": failed,
                    "hint": "rerun the same restore command; records already at target are skipped",
                },
            }
        return {"state": "restored", "source": source, "results": results}

    def resolve_ref(self, ref: RecordRef, value: str) -> dict[str, Any]:
        if value in ("=HEAD", "HEAD"):
            head = self.adapter.get_head(ref.collection, ref.record_id)
            if not head:
                raise NoSuchConfig(f"no HEAD for {ref.collection}:{ref.record_id}")
            return head
        if value in ("=live", "live"):
            live = self.adapter.get_record(ref.collection, ref.record_id)
            if not live:
                raise NoSuchConfig(f"no live record for {ref.collection}:{ref.record_id}")
            coll = self.config.collection(ref.collection)
            return {"doc": live, "oid": hash_doc(live, coll), "seq": None}
        if value.startswith("@{") and value.endswith("}"):
            when = _parse_when(value[2:-1])
            rows = self.adapter.query_history(
                collection=ref.collection,
                record_id=ref.record_id,
                as_of_valid=when,
                limit=1,
                order="desc",
                with_doc=True,
            )
        elif value.startswith("@"):
            seq = int(value[1:])
            rows = self.adapter.query_history(collection=ref.collection, record_id=ref.record_id, ref=f"@{seq}", with_doc=True)
        elif value.startswith("tag:"):
            rows = self.adapter.query_history(collection=ref.collection, record_id=ref.record_id, tag=value[4:], with_doc=True)
        else:
            rows = self.adapter.query_history(collection=ref.collection, record_id=ref.record_id, ref=value, with_doc=True)
        if not rows:
            raise NoSuchConfig(_ref_not_found_message(value))
        if len(rows) > 1:
            raise ValueError(f"ambiguous ref: {value}")
        return rows[0]

    def diff(self, ref: RecordRef, a: str, b: str) -> list[dict[str, Any]]:
        coll = self.config.collection(ref.collection)
        left = strip_for_hash(self.resolve_ref(ref, a)["doc"], coll)
        right = strip_for_hash(self.resolve_ref(ref, b)["doc"], coll)
        return diff_values(left, right)

    def log(self, ref: RecordRef, *, limit: int | None = None) -> list[dict[str, Any]]:
        return self.adapter.query_history(
            collection=ref.collection,
            record_id=ref.record_id,
            limit=limit,
            order="desc",
            with_doc=False,
        )

    def recent_history(self, *, limit: int | None = 50) -> list[dict[str, Any]]:
        rows = self.adapter.query_history(limit=None, order="desc", with_doc=False)
        rows = sorted(rows, key=lambda row: (row.get("recorded_at"), row["collection"], row["record_id"], row["seq"]), reverse=True)
        return rows[:limit] if limit is not None else rows

    def tag(self, name: str) -> list[dict[str, Any]]:
        self._authorize("tag")
        tagged: list[dict[str, Any]] = []
        for ref in self._all_refs(include_history=True):
            head = self.adapter.get_head(ref.collection, ref.record_id)
            if not head:
                tagged.append(
                    {
                        "collection": ref.collection,
                        "record_id": ref.record_id,
                        "state": "skipped_no_head",
                    }
                )
                continue
            self.adapter.add_tag(collection=ref.collection, record_id=ref.record_id, seq=head["seq"], tag=name)
            tagged.append(
                {
                    "collection": ref.collection,
                    "record_id": ref.record_id,
                    "state": "tagged",
                    "oid": head["oid"],
                }
            )
        return tagged

    def _entry(
        self,
        ref: RecordRef,
        doc: dict[str, Any],
        coll: CollectionConfig,
        *,
        message: str,
        op: str,
        parent_oid: str | None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        message = _require_message(message)
        now = self.adapter.now()
        stored = stored_doc(doc, coll)
        oid = hash_doc(stored, coll)
        return {
            "collection": ref.collection,
            "record_id": ref.record_id,
            "env": self.env,
            "seq": None,
            "oid": oid,
            "parent_oid": parent_oid,
            "doc": stored,
            "message": message,
            "author": self.author,
            "recorded_at": now,
            "valid_from": now,
            "valid_to": None,
            "valid_from_estimated": False,
            "op": op,
            "git_shas": [],
            "tags": [],
            "meta": self._history_meta(meta),
        }

    def _all_refs(self, *, include_history: bool) -> list[RecordRef]:
        refs: set[tuple[str, str]] = set()
        for coll in self.config.collections:
            for record_id in self.adapter.list_record_ids(coll.name):
                refs.add((coll.name, record_id))
        if include_history:
            for row in self.adapter.query_history(limit=None, order="asc", with_doc=False):
                refs.add((row["collection"], row["record_id"]))
        return [RecordRef(collection, record_id) for collection, record_id in refs]

    def _main_doc(self, ref: RecordRef) -> dict[str, Any] | None:
        head = self.adapter.get_head(ref.collection, ref.record_id)
        if head:
            return head["doc"]
        return self.adapter.get_record(ref.collection, ref.record_id)

    def _branch_latest_by_record(self, branch: str) -> dict[RecordRef, dict[str, Any]]:
        by_record: dict[RecordRef, list[dict[str, Any]]] = {}
        rows = self.adapter.list_refs("branch_commit", branch=branch)
        for row in rows:
            by_record.setdefault(RecordRef(row["collection"], row["record_id"]), []).append(row)
        latest: dict[RecordRef, dict[str, Any]] = {}
        for ref, commits in by_record.items():
            parent_ids = {row.get("parent_commit_id") for row in commits if row.get("parent_commit_id")}
            candidates = [row for row in commits if row["id"] not in parent_ids]
            if not candidates:
                candidates = commits
            latest[ref] = sorted(candidates, key=lambda row: (row["created_at"], row["id"]))[-1]
        return latest

    def _branch_latest_for_record(self, branch: str, ref: RecordRef) -> dict[str, Any] | None:
        return self._branch_latest_by_record(branch).get(ref)

    def _authorize(self, action: str) -> None:
        authorize_mutation(self.config.envs[self.env], identity=self.identity, action=action)

    def _ensure_atomic(self, action: str) -> None:
        report = self.adapter.check_atomicity_scope()
        if report.atomic:
            return
        raise AtomicityUnavailable(
            f"{self.adapter.backend_name()} cannot safely run {action}: {report.reason}. "
            "Use a transactional deployment with runtime and cfgit history co-located."
        )

    def _ensure_branching(self) -> None:
        if not self.config.branches.enabled:
            raise BranchingDisabled(
                "branching is not enabled. Add [branches] enabled = true and run cfg init."
            )

    def _require_branch(self, name: str) -> dict[str, Any] | None:
        default = self.config.branches.default_branch
        if name == default:
            return None
        branch = self.adapter.get_ref("branch", name)
        if not branch:
            raise NoSuchConfig(f"branch not found: {name}")
        return branch

    def _secret_meta(
        self,
        doc: dict[str, Any],
        coll: CollectionConfig,
        *,
        allow_secret: bool,
        message: str,
    ) -> dict[str, Any]:
        scan_doc = stored_doc(doc, coll)
        matches = _secret_matches(scan_doc, self.config.secrets.block_fields, self.config.secrets.block_values)
        if not matches:
            return {}
        if self.config.secrets.on_match == "refuse" and not allow_secret:
            # group by normalized path so list-index/duplicate hits collapse, and
            # give the exact secret_fields lines to paste — one fix pass, not N.
            seen: dict[str, str] = {}
            for item in matches:
                norm = re.sub(r"\[\d+\]", "[]", item["path"])
                seen.setdefault(norm, item["kind"])
            paths = sorted(seen)
            shown = ", ".join(f"{p} ({seen[p]})" for p in paths[:8])
            more = f" and {len(paths) - 8} more" if len(paths) > 8 else ""
            snippet = ", ".join(f'"{p}"' for p in paths)
            raise SecretBlocked(
                f"secret-like content refused in {len(paths)} field(s): {shown}{more}. "
                f"These are stripped from history if you add them to this collection's "
                f"secret_fields:\n  secret_fields = [{snippet}]\n"
                f"Run `cfg doctor` to see every collection at once, or rerun with "
                f"--allow-secret if this is intentional."
            )
        return {
            "allow_secret": bool(allow_secret),
            "allow_secret_author": self.author if allow_secret else None,
            "allow_secret_identity": self.identity.history_meta() if allow_secret else None,
            "allow_secret_reason": _require_message(message) if allow_secret else None,
            "secret_matches": matches,
            "secret_policy": self.config.secrets.on_match,
        }

    def _history_meta(self, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        out = {"identity": self.identity.history_meta()}
        out.update(meta or {})
        return out


def _require_message(message: str) -> str:
    text = str(message or "").strip()
    if not text:
        raise ValueError("message must be non-empty")
    return text


_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


def _clean_branch_name(name: str) -> str:
    value = str(name or "").strip()
    if not value:
        raise ValueError("branch name must be non-empty")
    if value in {".", ".."} or value.endswith("/") or value.endswith("."):
        raise ValueError(f"invalid branch name: {value}")
    if ".." in value or "//" in value or "@{" in value:
        raise ValueError(f"invalid branch name: {value}")
    if not _BRANCH_NAME_RE.match(value):
        raise ValueError(f"invalid branch name: {value}")
    return value


def _secret_matches(
    doc: dict[str, Any],
    field_patterns: tuple[str, ...],
    value_patterns: tuple[str, ...],
) -> list[dict[str, str]]:
    if not field_patterns and not value_patterns:
        return []
    matches: list[dict[str, str]] = []
    compiled = [(pattern, re.compile(pattern)) for pattern in value_patterns]
    for path, value in _walk_doc(doc):
        basename = path.rsplit(".", 1)[-1].split("[", 1)[0]
        for pattern in field_patterns:
            if fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(basename, pattern):
                matches.append({"path": path, "kind": "field", "pattern": pattern})
                break
        if isinstance(value, str):
            for pattern, regex in compiled:
                if regex.search(value):
                    matches.append({"path": path, "kind": "value", "pattern": pattern})
                    break
    return matches


def _secret_container(path: str, field_patterns: tuple[str, ...]) -> str:
    """Roll a dotted path up to its shallowest secret-bearing segment, so a stripping
    suggestion covers the whole subtree. For
    `cached_schema...properties.openai_api_key.type` with a `*api_key*` pattern, returns
    `cached_schema...properties.openai_api_key` (stripping that removes all its children).
    If no segment matches a field pattern (e.g. a value-only match), returns path as-is."""
    segments = path.split(".")
    for i, seg in enumerate(segments):
        base = seg.split("[", 1)[0]
        if any(fnmatch.fnmatchcase(base, pat) for pat in field_patterns):
            return ".".join(segments[: i + 1])
    return path


def _plan_result(plan: dict[str, Any]) -> dict[str, Any]:
    ref = plan["ref"]
    result = {"collection": ref.collection, "record_id": ref.record_id, "state": plan["state"]}
    for key in ("oid", "seq", "live_oid", "head_oid"):
        if key in plan:
            result[key] = plan[key]
    return result


def _branch_plan_result(plan: dict[str, Any], *, state: str | None = None) -> dict[str, Any]:
    ref = plan["ref"]
    result = {
        "collection": ref.collection,
        "record_id": ref.record_id,
        "state": state or plan["state"],
        "runtime_mutated": False,
    }
    if plan.get("commit"):
        result["commit_id"] = plan["commit"]["id"]
        result["oid"] = plan["commit"]["oid"]
    for key in ("oid", "commit_id", "live_oid", "head_oid"):
        if key in plan and key not in result:
            result[key] = plan[key]
    return result


def _ref_not_found_message(value: str) -> str:
    if value.isdecimal():
        return f"ref not found: {value} (sequence refs use @{value}; bare values are oid prefixes)"
    return f"ref not found: {value}"


def _parse_branch_range(value: str, default_branch: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    if ".." not in raw:
        raise ValueError("branch range must look like main..<branch>")
    base, head = raw.split("..", 1)
    return _clean_branch_name(base or default_branch), _clean_branch_name(head)


def _walk_doc(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        out: list[tuple[str, Any]] = []
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.extend(_walk_doc(child, path))
        return out
    if isinstance(value, list):
        out = []
        for index, child in enumerate(value):
            out.extend(_walk_doc(child, f"{prefix}[{index}]"))
        return out
    return [(prefix, value)]


def _parse_when(raw: str) -> datetime:
    value = raw.strip()
    date_only = len(value) == 10 and value[4] == "-" and value[7] == "-"
    if date_only:
        dt = datetime.combine(datetime.fromisoformat(value).date(), time.max)
    else:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
