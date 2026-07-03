from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from cfg.core.config import ProjectConfig
from cfg_agent.resources import parse_resource, resources_overlap
from cfg_agent.state import AgentStateError, IdempotencyResult, _iso, _parse_time, utcnow

try:  # pragma: no cover - exercised when cfgit[mongo] is installed
    from pymongo import ASCENDING, ReturnDocument, MongoClient
    from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError
except ModuleNotFoundError as exc:  # pragma: no cover
    raise ModuleNotFoundError("install cfgit[mongo] to use MongoAgentStateAdapter") from exc


class MongoAgentStateAdapter:
    """Mongo-backed coordination state for multi-process agent runtimes.

    State is stored beside cfgit history, not in cfgit core. Lease acquisition
    uses a small env-scoped lock document so overlapping path claims cannot race.
    """

    def __init__(
        self,
        *,
        project: ProjectConfig,
        env_name: str,
        state_collection: str = "cfgit_agent_state",
        events_collection: str = "cfgit_agent_events",
    ) -> None:
        env = project.envs[env_name]
        if not env.uri and not env.history_uri:
            raise ValueError(f"missing Mongo URI for env {env_name}")
        self.env_name = env_name
        self.history_uri = env.history_uri or env.uri
        self.history_db_name = env.history_db or env.db
        self.client = MongoClient(self.history_uri)
        self.db = self.client[self.history_db_name]
        self.state = self.db[state_collection]
        self.events = self.db[events_collection]

    def init_agent_state(self) -> None:
        self.state.create_index([("env", ASCENDING), ("kind", ASCENDING), ("id", ASCENDING)], unique=True)
        self.state.create_index([("env", ASCENDING), ("kind", ASCENDING), ("status", ASCENDING)])
        self.state.create_index([("env", ASCENDING), ("kind", ASCENDING), ("resource", ASCENDING)])
        self.events.create_index([("env", ASCENDING), ("event_id", ASCENDING)], unique=True)
        self.events.create_index([("env", ASCENDING), ("recorded_at", ASCENDING)])

    def create_session(self, session: dict[str, Any]) -> dict[str, Any]:
        return self._insert_state("session", session["session_id"], session)

    def heartbeat_session(self, session_id: str, now: datetime) -> dict[str, Any]:
        session = self._require("session", session_id, "session_not_found")
        session["heartbeat_at"] = _iso(now)
        return self._put_state("session", session_id, session)

    def close_session(self, session_id: str, status: str, now: datetime) -> dict[str, Any]:
        session = self._require("session", session_id, "session_not_found")
        session["status"] = status
        session["ended_at"] = _iso(now)
        session["heartbeat_at"] = _iso(now)
        return self._put_state("session", session_id, session)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self._get_state("session", session_id)

    def list_sessions(self, status: str | None = None) -> list[dict[str, Any]]:
        return self._list_state("session", status=status, sort_field="started_at")

    def acquire_lease(self, lease: dict[str, Any], *, now: datetime) -> dict[str, Any]:
        for attempt in range(3):
            try:
                with self.client.start_session() as session:
                    with session.start_transaction():
                        self._touch_lock("leases", now, session=session)
                        self._expire_leases(now, session=session)
                        resource = parse_resource(lease["resource"])
                        conflicts = []
                        for existing in self._list_state("lease", status="active", session=session):
                            if existing.get("session_id") == lease["session_id"]:
                                continue
                            if resources_overlap(resource, existing["resource"]):
                                conflicts.append(existing)
                        if conflicts:
                            raise AgentStateError(
                                "lease_conflict",
                                "another active lease overlaps this resource",
                                {"leases": conflicts},
                            )
                        return self._insert_state("lease", lease["lease_id"], lease, session=session)
            except DuplicateKeyError as exc:
                existing = self._get_state("lease", lease["lease_id"])
                if _lease_matches(existing, lease):
                    return existing
                raise AgentStateError(
                    "lease_state_conflict",
                    "lease id already exists with a different payload",
                    {"lease_id": lease["lease_id"]},
                ) from exc
            except AgentStateError:
                raise
            except (OperationFailure, PyMongoError) as exc:
                if _has_error_label(exc, "UnknownTransactionCommitResult"):
                    existing = self._get_state("lease", lease["lease_id"])
                    if _lease_matches(existing, lease):
                        return existing
                    raise AgentStateError(
                        "lease_commit_unknown",
                        "Mongo could not confirm whether the lease transaction committed",
                        {"lease_id": lease["lease_id"], "error": str(exc)},
                    ) from exc
                if attempt < 2 and _retryable_transaction_error(exc):
                    continue
                raise AgentStateError(
                    "lease_transaction_failed",
                    "Mongo lease transaction failed",
                    {"error": str(exc)},
                ) from exc
        raise AgentStateError("lease_transaction_failed", "Mongo lease transaction failed")

    def renew_lease(self, lease_id: str, *, ttl_seconds: int, now: datetime) -> dict[str, Any]:
        updated = self.state.find_one_and_update(
            {
                "env": self.env_name,
                "kind": "lease",
                "id": lease_id,
                "status": "active",
                "expires_at": {"$gt": _iso(now)},
            },
            {"$set": {"expires_at": _iso(now + timedelta(seconds=ttl_seconds))}},
            return_document=ReturnDocument.AFTER,
        )
        if updated:
            return _strip(updated)
        lease = self._get_state("lease", lease_id)
        if lease is None:
            raise AgentStateError("lease_not_found", f"{lease_id} was not found", {"id": lease_id})
        if lease.get("status") != "active":
            raise AgentStateError("lease_not_active", "lease is not active", {"lease_id": lease_id})
        if _parse_time(lease["expires_at"]) <= now:
            self.state.update_one(
                {
                    "env": self.env_name,
                    "kind": "lease",
                    "id": lease_id,
                    "status": "active",
                    "expires_at": {"$lte": _iso(now)},
                },
                {"$set": {"status": "expired"}},
            )
            raise AgentStateError("lease_expired", "lease has expired", {"lease_id": lease_id})
        raise AgentStateError("lease_renew_conflict", "lease changed during renew", {"lease_id": lease_id})

    def release_lease(self, lease_id: str, *, now: datetime) -> dict[str, Any]:
        lease = self._require("lease", lease_id, "lease_not_found")
        if lease.get("status") == "active":
            lease["status"] = "released"
            lease["released_at"] = _iso(now)
        return self._put_state("lease", lease_id, lease)

    def get_lease(self, lease_id: str) -> dict[str, Any] | None:
        return self._get_state("lease", lease_id)

    def list_leases(self, *, active_only: bool = True, now: datetime | None = None) -> list[dict[str, Any]]:
        self._expire_leases(now or utcnow())
        return self._list_state("lease", status="active" if active_only else None, sort_field="created_at")

    def open_intent(self, intent: dict[str, Any]) -> dict[str, Any]:
        return self._insert_state("intent", intent["intent_id"], intent)

    def close_intent(self, intent_id: str, status: str, now: datetime) -> dict[str, Any]:
        intent = self._require("intent", intent_id, "intent_not_found")
        intent["status"] = status
        intent["closed_at"] = _iso(now)
        return self._put_state("intent", intent_id, intent)

    def get_intent(self, intent_id: str) -> dict[str, Any] | None:
        return self._get_state("intent", intent_id)

    def list_intents(self, status: str | None = None) -> list[dict[str, Any]]:
        return self._list_state("intent", status=status, sort_field="created_at")

    def remember_idempotency(self, key: str, payload_hash: str, result: dict[str, Any], now: datetime) -> IdempotencyResult:
        doc = {
            "kind": "idempotency",
            "idempotency_key": key,
            "payload_hash": payload_hash,
            "result": deepcopy(result),
            "created_at": _iso(now),
        }
        try:
            self._insert_state("idempotency", key, doc)
        except DuplicateKeyError:
            existing = self._get_state("idempotency", key)
            if existing is None:
                raise AgentStateError(
                    "idempotency_race",
                    "idempotency key was concurrently inserted but could not be read",
                    {"key": key},
                ) from None
            if existing.get("payload_hash") != payload_hash:
                raise AgentStateError(
                    "idempotency_conflict",
                    "idempotency key was already used with a different payload",
                    {"key": key},
                )
            return IdempotencyResult(replay=True, result=deepcopy(existing.get("result")))
        return IdempotencyResult(replay=False, result=None)

    def get_idempotency(self, key: str) -> dict[str, Any] | None:
        return self._get_state("idempotency", key)

    def open_conflict(self, conflict: dict[str, Any]) -> dict[str, Any]:
        return self._insert_state("conflict", conflict["conflict_id"], conflict)

    def resolve_conflict(self, conflict_id: str, resolution: str, now: datetime) -> dict[str, Any]:
        conflict = self._require("conflict", conflict_id, "conflict_not_found")
        conflict["status"] = "resolved"
        conflict["resolution"] = resolution
        conflict["resolved_at"] = _iso(now)
        return self._put_state("conflict", conflict_id, conflict)

    def list_conflicts(self, status: str | None = None) -> list[dict[str, Any]]:
        return self._list_state("conflict", status=status, sort_field="created_at")

    def append_event(self, event: dict[str, Any]) -> None:
        doc = {**deepcopy(event), "env": self.env_name}
        self.events.insert_one(doc)

    def list_events(self, *, since_event_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        cursor = self.events.find({"env": self.env_name}).sort([("recorded_at", ASCENDING), ("event_id", ASCENDING)])
        rows = [_strip(row) for row in cursor]
        if since_event_id:
            for index, event in enumerate(rows):
                if event.get("event_id") == since_event_id:
                    rows = rows[index + 1 :]
                    break
        return deepcopy(rows[-limit:])

    def _insert_state(
        self,
        kind: str,
        item_id: str,
        doc: dict[str, Any],
        *,
        session: Any | None = None,
    ) -> dict[str, Any]:
        item = {**deepcopy(doc), "env": self.env_name, "kind": kind, "id": item_id}
        self.state.insert_one(item, session=session)
        return _strip(item)

    def _put_state(
        self,
        kind: str,
        item_id: str,
        doc: dict[str, Any],
        *,
        session: Any | None = None,
    ) -> dict[str, Any]:
        item = {**deepcopy(doc), "env": self.env_name, "kind": kind, "id": item_id}
        self.state.replace_one(
            {"env": self.env_name, "kind": kind, "id": item_id},
            item,
            upsert=True,
            session=session,
        )
        return _strip(item)

    def _get_state(self, kind: str, item_id: str, *, session: Any | None = None) -> dict[str, Any] | None:
        row = self.state.find_one({"env": self.env_name, "kind": kind, "id": item_id}, session=session)
        return _strip(row) if row else None

    def _require(self, kind: str, item_id: str, code: str) -> dict[str, Any]:
        item = self._get_state(kind, item_id)
        if item is None:
            raise AgentStateError(code, f"{item_id} was not found", {"id": item_id})
        return item

    def _list_state(
        self,
        kind: str,
        *,
        status: str | None = None,
        sort_field: str = "created_at",
        session: Any | None = None,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"env": self.env_name, "kind": kind}
        if status is not None:
            query["status"] = status
        cursor = self.state.find(query, session=session).sort([(sort_field, -1)])
        return [_strip(row) for row in cursor]

    def _expire_leases(self, now: datetime, *, session: Any | None = None) -> None:
        self.state.update_many(
            {
                "env": self.env_name,
                "kind": "lease",
                "status": "active",
                "expires_at": {"$lte": _iso(now)},
            },
            {"$set": {"status": "expired"}},
            session=session,
        )

    def _touch_lock(self, lock_id: str, now: datetime, *, session: Any | None = None) -> None:
        self.state.update_one(
            {"env": self.env_name, "kind": "lock", "id": lock_id},
            {
                "$set": {"updated_at": _iso(now)},
                "$setOnInsert": {"env": self.env_name, "kind": "lock", "id": lock_id},
            },
            upsert=True,
            session=session,
        )


def _strip(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in {"_id", "env", "id"}}


def _lease_matches(existing: dict[str, Any] | None, lease: dict[str, Any]) -> bool:
    if existing is None:
        return False
    fields = ("lease_id", "session_id", "resource", "collection", "record_id", "path", "scope", "status")
    return all(existing.get(field) == lease.get(field) for field in fields)


def _has_error_label(exc: PyMongoError, label: str) -> bool:
    has_label = getattr(exc, "has_error_label", None)
    return bool(callable(has_label) and has_label(label))


def _retryable_transaction_error(exc: PyMongoError) -> bool:
    if _has_error_label(exc, "TransientTransactionError"):
        return True
    return "WriteConflict" in str(exc)
