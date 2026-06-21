# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""Mongo StorageAdapter for cfgit."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlsplit

from cfg.adapters.base import (
    AmbiguousConfig,
    ApplyResult,
    AtomicityUnavailable,
    AtomicityReport,
    NoSuchConfig,
    ReconcileReport,
    StaleHead,
    StaleLive,
)
from cfg.core.config import ProjectConfig
from cfg.core.hashing import hash_doc

try:  # pragma: no cover - exercised only when mongo extra is installed
    from pymongo import ASCENDING, MongoClient
    from pymongo.client_session import ClientSession
    from pymongo.errors import OperationFailure
except ModuleNotFoundError as exc:  # pragma: no cover
    raise ModuleNotFoundError("install cfg-vcs[mongo] to use MongoAdapter") from exc


class MongoAdapter:
    def __init__(self, *, project: ProjectConfig, env_name: str):
        env = project.envs[env_name]
        if not env.uri:
            raise ValueError(f"missing Mongo URI for env {env_name}")
        self.project = project
        self.env_name = env_name
        self.runtime_uri = env.runtime_uri or env.uri
        self.history_uri = env.history_uri or env.uri
        self.runtime_db_name = env.runtime_db or env.db
        self.history_db_name = env.history_db or env.db
        self.client = MongoClient(self.history_uri)
        self.history_client = self.client
        self.runtime_client = self.client if self.runtime_uri == self.history_uri else MongoClient(self.runtime_uri)
        self.db = self.runtime_client[self.runtime_db_name]
        self.history_db = self.history_client[self.history_db_name]
        self.history = self.history_db[project.history.history_collection]
        self.heads = self.history_db[project.history.heads_collection]

    def get_record(self, collection: str, record_id: str) -> dict | None:
        docs = list(self.db[collection].find(self._runtime_query(collection, record_id)).limit(2))
        if len(docs) > 1:
            raise AmbiguousConfig(f"{collection}:{record_id}")
        return docs[0] if docs else None

    def put_record(self, collection: str, record_id: str, doc: dict) -> None:
        self._put_record(collection, record_id, doc, session=None)

    def seed_record(self, collection: str, record_id: str, doc: dict) -> None:
        self._seed_record(collection, record_id, doc, session=None)

    def list_record_ids(self, collection: str) -> list[str]:
        coll = self.project.collection(collection)
        values = self.db[collection].distinct(coll.id_field, coll.live_when)
        return sorted(str(v) for v in values if v is not None)

    def get_head(self, collection: str, record_id: str) -> dict | None:
        ptr = self.heads.find_one(self._head_query(collection, record_id))
        if not ptr:
            return None
        row = self.history.find_one(
            {
                "env": self.env_name,
                "collection": collection,
                "record_id": record_id,
                "seq": ptr["head_seq"],
            }
        )
        return _history_row(row, with_doc=True) if row else None

    def query_history(
        self,
        *,
        collection: str | None = None,
        record_id: str | None = None,
        ref: str | None = None,
        as_of_recorded: datetime | None = None,
        as_of_valid: datetime | None = None,
        tag: str | None = None,
        git_sha: str | None = None,
        limit: int | None = None,
        order: str = "desc",
        with_doc: bool = False,
    ) -> list[dict]:
        query: dict[str, Any] = {"env": self.env_name}
        if collection is not None:
            query["collection"] = collection
        if record_id is not None:
            query["record_id"] = record_id
        if tag is not None:
            query["tags"] = tag
        if git_sha is not None:
            query["git_shas"] = git_sha
        if as_of_recorded is not None:
            query["recorded_at"] = {"$lte": as_of_recorded}
        if as_of_valid is not None:
            query["valid_from"] = {"$lte": as_of_valid}
            query["$or"] = [{"valid_to": None}, {"valid_to": {"$gt": as_of_valid}}]
        if ref is not None:
            if ref.startswith("@"):
                query["seq"] = int(ref[1:])
            else:
                oid = ref.removeprefix("sha256:").removeprefix("#")
                query["oid"] = {"$regex": f"^{oid}"}

        projection = None if with_doc else {"doc": 0}
        direction = -1 if order == "desc" else 1
        cursor = self.history.find(query, projection).sort(
            [("collection", ASCENDING), ("record_id", ASCENDING), ("seq", direction)]
        )
        if limit is not None:
            cursor = cursor.limit(limit)
        return [_history_row(row, with_doc=with_doc) for row in cursor]

    def list_tags(self) -> list[dict]:
        pipeline = [
            {"$match": {"env": self.env_name, "tags": {"$exists": True, "$ne": []}}},
            {"$unwind": "$tags"},
            {"$group": {"_id": "$tags", "count": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]
        return [{"tag": row["_id"], "count": row["count"]} for row in self.history.aggregate(pipeline)]

    def apply(
        self,
        *,
        collection: str,
        record_id: str,
        new_doc: dict | None,
        entry: dict,
        expected_head_oid: str | None,
        expected_live_oid: str | None = None,
        make_head: bool = True,
        seed_missing: bool = False,
    ) -> ApplyResult:
        atomicity = self.check_atomicity_scope()
        if not atomicity.atomic:
            raise AtomicityUnavailable(atomicity.reason)
        for attempt in range(3):
            try:
                return self._apply_once(
                    collection=collection,
                    record_id=record_id,
                    new_doc=new_doc,
                    entry=entry,
                    expected_head_oid=expected_head_oid,
                    expected_live_oid=expected_live_oid,
                    make_head=make_head,
                    seed_missing=seed_missing,
                )
            except OperationFailure as exc:
                if attempt >= 2 or not _is_transient_transaction_error(exc):
                    raise
        raise RuntimeError("unreachable")

    def _apply_once(
        self,
        *,
        collection: str,
        record_id: str,
        new_doc: dict | None,
        entry: dict,
        expected_head_oid: str | None,
        expected_live_oid: str | None,
        make_head: bool,
        seed_missing: bool,
    ) -> ApplyResult:
        coll_cfg = self.project.collection(collection)
        with self.client.start_session() as session:
            with session.start_transaction():
                ptr = self.heads.find_one(self._head_query(collection, record_id), session=session)
                current_head = ptr.get("head_oid") if ptr else None
                if current_head != expected_head_oid:
                    raise StaleHead(current_head)

                if expected_live_oid is not None:
                    live = self._get_record(collection, record_id, session=session)
                    if live is None:
                        raise NoSuchConfig(f"{collection}:{record_id}")
                    live_oid = hash_doc(live, coll_cfg)
                    if live_oid != expected_live_oid:
                        raise StaleLive(live_oid)

                seq = int(ptr.get("head_seq", 0)) + 1 if ptr else 1
                entry = dict(entry)
                entry.update(
                    {
                        "env": self.env_name,
                        "collection": collection,
                        "record_id": record_id,
                        "seq": seq,
                    }
                )
                self.history.insert_one(entry, session=session)

                if current_head:
                    self.history.update_one(
                        {
                            "env": self.env_name,
                            "collection": collection,
                            "record_id": record_id,
                            "seq": ptr["head_seq"],
                            "valid_to": None,
                        },
                        {"$set": {"valid_to": entry["valid_from"]}},
                        session=session,
                    )

                if new_doc is not None:
                    if seed_missing:
                        if self._get_record(collection, record_id, session=session) is not None:
                            raise StaleLive(f"{collection}:{record_id} reappeared before restore")
                        self._seed_record(collection, record_id, new_doc, session=session)
                    else:
                        self._put_record(collection, record_id, new_doc, session=session)

                if make_head:
                    head_filter = self._head_query(collection, record_id)
                    if ptr:
                        head_filter = {
                            **head_filter,
                            "head_oid": expected_head_oid,
                            "head_seq": ptr["head_seq"],
                        }
                    result = self.heads.update_one(
                        head_filter,
                        {
                            "$set": {
                                "head_oid": entry["oid"],
                                "head_seq": seq,
                                "updated_at": entry["recorded_at"],
                            },
                            "$setOnInsert": {
                                "env": self.env_name,
                                "collection": collection,
                                "record_id": record_id,
                            },
                        },
                        upsert=not ptr,
                        session=session,
                    )
                    if ptr and result.matched_count != 1:
                        raise StaleHead(expected_head_oid)

        return ApplyResult(
            collection=collection,
            record_id=record_id,
            seq=seq,
            oid=entry["oid"],
            head_oid=entry["oid"],
        )

    def add_tag(self, *, collection: str, record_id: str, seq: int, tag: str) -> None:
        self.history.update_one(
            {"env": self.env_name, "collection": collection, "record_id": record_id, "seq": seq},
            {"$addToSet": {"tags": tag}},
        )

    def remove_tag(self, *, collection: str, record_id: str, seq: int, tag: str) -> None:
        self.history.update_one(
            {"env": self.env_name, "collection": collection, "record_id": record_id, "seq": seq},
            {"$pull": {"tags": tag}},
        )

    def list_pending(self) -> list[dict]:
        return list(self.history.find({"env": self.env_name, "pending": True}))

    def reconcile(self) -> ReconcileReport:
        return ReconcileReport(rolled_forward=[], rolled_back=[])

    def ensure_schema(self) -> None:
        self.history.create_index(
            [("env", ASCENDING), ("collection", ASCENDING), ("record_id", ASCENDING), ("oid", ASCENDING)],
        )
        self.history.create_index(
            [("env", ASCENDING), ("collection", ASCENDING), ("record_id", ASCENDING), ("seq", ASCENDING)],
            unique=True,
        )
        self.history.create_index([("env", ASCENDING), ("recorded_at", ASCENDING)])
        self.history.create_index([("env", ASCENDING), ("valid_from", ASCENDING)])
        self.history.create_index([("env", ASCENDING), ("valid_to", ASCENDING)])
        self.history.create_index([("tags", ASCENDING)])
        self.heads.create_index(
            [("env", ASCENDING), ("collection", ASCENDING), ("record_id", ASCENDING)],
            unique=True,
        )

    def check_runtime_invariant(self, collection: str | None = None) -> list[str]:
        names = [collection] if collection else [c.name for c in self.project.collections]
        violations: list[str] = []
        for name in names:
            coll = self.project.collection(name)
            pipeline = [
                {"$match": coll.live_when},
                {"$group": {"_id": f"${coll.id_field}", "n": {"$sum": 1}}},
                {"$match": {"_id": {"$ne": None}, "n": {"$gt": 1}}},
                {"$sort": {"_id": 1}},
            ]
            for row in self.db[name].aggregate(pipeline):
                violations.append(f"{name}:{row['_id']} ({row['n']} live records)")
        return violations

    def check_atomicity_scope(self) -> AtomicityReport:
        runtime_cluster = self._cluster_name(self.runtime_client)
        history_cluster = self._cluster_name(self.history_client)
        same_client = self.runtime_client is self.history_client
        runtime_txn = self._supports_transactions(self.runtime_client)
        history_txn = self._supports_transactions(self.history_client)
        ok = same_client and runtime_txn and history_txn
        if ok:
            reason = "ok"
        elif not same_client:
            reason = (
                "runtime and cfgit history use separate Mongo clients; v1 requires one URI/client "
                "so live writes and history writes share a transaction"
            )
        else:
            reason = "Mongo deployment is not a replica set or sharded cluster"
        return AtomicityReport(
            atomic=ok,
            runtime_cluster=runtime_cluster,
            history_cluster=history_cluster,
            reason=reason,
        )

    def backend_name(self) -> str:
        return "mongo"

    def supports_transactions(self) -> bool:
        return self._supports_transactions(self.history_client)

    def authenticated_principal(self) -> str | None:
        try:
            status = self.runtime_client.admin.command("connectionStatus")
            users = ((status.get("authInfo") or {}).get("authenticatedUsers") or [])
            if users:
                user = users[0]
                name = str(user.get("user") or "").strip()
                db = str(user.get("db") or "").strip()
                if name and db:
                    return f"{name}@{db}"
                if name:
                    return name
        except Exception:
            pass
        parsed = urlsplit(self.runtime_uri)
        username = unquote(parsed.username or "").strip()
        auth_source = ""
        if parsed.query:
            for part in parsed.query.split("&"):
                key, _, value = part.partition("=")
                if key.lower() == "authsource":
                    auth_source = unquote(value)
                    break
        if username and auth_source:
            return f"{username}@{auth_source}"
        return username or None

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _runtime_query(self, collection: str, record_id: str) -> dict[str, Any]:
        coll = self.project.collection(collection)
        return {coll.id_field: record_id, **coll.live_when}

    def _head_query(self, collection: str, record_id: str) -> dict[str, Any]:
        return {"env": self.env_name, "collection": collection, "record_id": record_id}

    def _get_record(self, collection: str, record_id: str, *, session: ClientSession | None) -> dict | None:
        docs = list(self.db[collection].find(self._runtime_query(collection, record_id), session=session).limit(2))
        if len(docs) > 1:
            raise AmbiguousConfig(f"{collection}:{record_id}")
        return docs[0] if docs else None

    def _put_record(
        self,
        collection: str,
        record_id: str,
        doc: dict,
        *,
        session: ClientSession | None,
    ) -> None:
        coll = self.project.collection(collection)
        current = self._get_record(collection, record_id, session=session)
        if current is None:
            raise NoSuchConfig(f"{collection}:{record_id}")

        effective = self._runtime_doc(collection, record_id, doc)
        for path in coll.secret_fields:
            if _get_path(effective, path) is None:
                secret_value = _get_path(current, path)
                if secret_value is not None:
                    _set_path(effective, path, secret_value)

        protected = {"_id", *coll.ignore_fields}
        set_doc = {k: v for k, v in effective.items() if k not in protected}
        unset_doc = {
            k: ""
            for k in current
            if k not in protected and k not in effective
        }
        update: dict[str, Any] = {}
        if set_doc:
            update["$set"] = set_doc
        if unset_doc:
            update["$unset"] = unset_doc
        if not update:
            return

        result = self.db[collection].update_one(
            self._runtime_query(collection, record_id),
            update,
            session=session,
        )
        if result.matched_count == 0:
            raise NoSuchConfig(f"{collection}:{record_id}")

    def _seed_record(
        self,
        collection: str,
        record_id: str,
        doc: dict,
        *,
        session: ClientSession | None,
    ) -> None:
        if self._get_record(collection, record_id, session=session) is not None:
            raise AmbiguousConfig(f"{collection}:{record_id}")
        self.db[collection].insert_one(
            self._runtime_doc(collection, record_id, doc),
            session=session,
        )

    def _runtime_doc(self, collection: str, record_id: str, doc: dict) -> dict[str, Any]:
        coll = self.project.collection(collection)
        effective = deepcopy(doc)
        effective[coll.id_field] = record_id
        for key, configured_value in coll.live_when.items():
            effective[key] = configured_value
        return effective

    def _cluster_name(self, client: MongoClient) -> str:
        hello = client.admin.command("hello")
        hosts = ",".join(sorted(str(host) for host in hello.get("hosts", [])))
        return str(hello.get("setName") or hello.get("msg") or hello.get("me") or hosts or "standalone")

    def _supports_transactions(self, client: MongoClient) -> bool:
        hello = client.admin.command("hello")
        return bool(hello.get("setName") or hello.get("msg") == "isdbgrid")


def _history_row(row: dict[str, Any], *, with_doc: bool) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "_id" and (with_doc or key != "doc")}


def _get_path(doc: dict[str, Any], dotted: str) -> Any:
    cur: Any = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set_path(doc: dict[str, Any], dotted: str, value: Any) -> None:
    cur: Any = doc
    parts = dotted.split(".")
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _is_transient_transaction_error(exc: OperationFailure) -> bool:
    has_error_label = getattr(exc, "has_error_label", None)
    if callable(has_error_label) and has_error_label("TransientTransactionError"):
        return True
    details = getattr(exc, "details", None) or {}
    return "TransientTransactionError" in details.get("errorLabels", [])
