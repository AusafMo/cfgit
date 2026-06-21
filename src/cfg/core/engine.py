# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""DB-neutral cfgit engine."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
import fnmatch
import re
from typing import Any

from cfg.adapters.base import AtomicityUnavailable, NoSuchConfig, StorageAdapter
from cfg.core.authz import authorize_mutation
from cfg.core.config import CollectionConfig, ProjectConfig
from cfg.core.diff import diff_values
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
        }

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
            raise NoSuchConfig(f"ref not found: {value}")
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
            rendered = ", ".join(f"{item['path']} ({item['kind']}:{item['pattern']})" for item in matches[:8])
            more = f" and {len(matches) - 8} more" if len(matches) > 8 else ""
            raise SecretBlocked(
                f"secret-like content refused in {rendered}{more}; add the path to secret_fields "
                "or rerun with --allow-secret if this is intentional"
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
