from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from cfg_agent.resources import parse_resource, resources_overlap


class AgentStateError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class IdempotencyResult:
    replay: bool
    result: dict[str, Any] | None


@runtime_checkable
class AgentStateAdapter(Protocol):
    def init_agent_state(self) -> None: ...
    def create_session(self, session: dict[str, Any]) -> dict[str, Any]: ...
    def heartbeat_session(self, session_id: str, now: datetime) -> dict[str, Any]: ...
    def close_session(self, session_id: str, status: str, now: datetime) -> dict[str, Any]: ...
    def get_session(self, session_id: str) -> dict[str, Any] | None: ...
    def list_sessions(self, status: str | None = None) -> list[dict[str, Any]]: ...
    def acquire_lease(self, lease: dict[str, Any], *, now: datetime) -> dict[str, Any]: ...
    def renew_lease(self, lease_id: str, *, ttl_seconds: int, now: datetime) -> dict[str, Any]: ...
    def release_lease(self, lease_id: str, *, now: datetime) -> dict[str, Any]: ...
    def list_leases(self, *, active_only: bool = True, now: datetime | None = None) -> list[dict[str, Any]]: ...
    def open_intent(self, intent: dict[str, Any]) -> dict[str, Any]: ...
    def close_intent(self, intent_id: str, status: str, now: datetime) -> dict[str, Any]: ...
    def get_intent(self, intent_id: str) -> dict[str, Any] | None: ...
    def list_intents(self, status: str | None = None) -> list[dict[str, Any]]: ...
    def remember_idempotency(self, key: str, payload_hash: str, result: dict[str, Any], now: datetime) -> IdempotencyResult: ...
    def get_idempotency(self, key: str) -> dict[str, Any] | None: ...
    def open_conflict(self, conflict: dict[str, Any]) -> dict[str, Any]: ...
    def resolve_conflict(self, conflict_id: str, resolution: str, now: datetime) -> dict[str, Any]: ...
    def list_conflicts(self, status: str | None = None) -> list[dict[str, Any]]: ...
    def append_event(self, event: dict[str, Any]) -> None: ...
    def list_events(self, *, since_event_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]: ...


class InMemoryAgentStateAdapter:
    """Reference state adapter for tests and local MCP sessions.

    The production adapters will store the same logical state in Mongo/Postgres.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self.sessions: dict[str, dict[str, Any]] = {}
        self.leases: dict[str, dict[str, Any]] = {}
        self.intents: dict[str, dict[str, Any]] = {}
        self.conflicts: dict[str, dict[str, Any]] = {}
        self.idempotency: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []

    def init_agent_state(self) -> None:
        return None

    def create_session(self, session: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            item = deepcopy(session)
            self.sessions[item["session_id"]] = item
            return deepcopy(item)

    def heartbeat_session(self, session_id: str, now: datetime) -> dict[str, Any]:
        with self._lock:
            session = self._require(self.sessions, session_id, "session_not_found")
            session["heartbeat_at"] = _iso(now)
            return deepcopy(session)

    def close_session(self, session_id: str, status: str, now: datetime) -> dict[str, Any]:
        with self._lock:
            session = self._require(self.sessions, session_id, "session_not_found")
            session["status"] = status
            session["ended_at"] = _iso(now)
            session["heartbeat_at"] = _iso(now)
            return deepcopy(session)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            return deepcopy(self.sessions.get(session_id))

    def list_sessions(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self.sessions.values())
            if status:
                rows = [row for row in rows if row.get("status") == status]
            return deepcopy(sorted(rows, key=lambda row: row["started_at"], reverse=True))

    def acquire_lease(self, lease: dict[str, Any], *, now: datetime) -> dict[str, Any]:
        with self._lock:
            self._expire_leases(now)
            resource = parse_resource(lease["resource"])
            conflicts = []
            for existing in self.leases.values():
                if existing.get("status") != "active":
                    continue
                if existing.get("session_id") == lease["session_id"]:
                    continue
                if resources_overlap(resource, existing["resource"]):
                    conflicts.append(deepcopy(existing))
            if conflicts:
                raise AgentStateError(
                    "lease_conflict",
                    "another active lease overlaps this resource",
                    {"leases": conflicts},
                )
            item = deepcopy(lease)
            self.leases[item["lease_id"]] = item
            return deepcopy(item)

    def renew_lease(self, lease_id: str, *, ttl_seconds: int, now: datetime) -> dict[str, Any]:
        with self._lock:
            lease = self._require(self.leases, lease_id, "lease_not_found")
            if lease.get("status") != "active":
                raise AgentStateError("lease_not_active", "lease is not active", {"lease_id": lease_id})
            lease["expires_at"] = _iso(now + timedelta(seconds=ttl_seconds))
            return deepcopy(lease)

    def release_lease(self, lease_id: str, *, now: datetime) -> dict[str, Any]:
        with self._lock:
            lease = self._require(self.leases, lease_id, "lease_not_found")
            if lease.get("status") == "active":
                lease["status"] = "released"
                lease["released_at"] = _iso(now)
            return deepcopy(lease)

    def list_leases(self, *, active_only: bool = True, now: datetime | None = None) -> list[dict[str, Any]]:
        with self._lock:
            self._expire_leases(now or utcnow())
            rows = list(self.leases.values())
            if active_only:
                rows = [row for row in rows if row.get("status") == "active"]
            return deepcopy(sorted(rows, key=lambda row: row["created_at"], reverse=True))

    def open_intent(self, intent: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            item = deepcopy(intent)
            self.intents[item["intent_id"]] = item
            return deepcopy(item)

    def close_intent(self, intent_id: str, status: str, now: datetime) -> dict[str, Any]:
        with self._lock:
            intent = self._require(self.intents, intent_id, "intent_not_found")
            intent["status"] = status
            intent["closed_at"] = _iso(now)
            return deepcopy(intent)

    def get_intent(self, intent_id: str) -> dict[str, Any] | None:
        with self._lock:
            return deepcopy(self.intents.get(intent_id))

    def list_intents(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self.intents.values())
            if status:
                rows = [row for row in rows if row.get("status") == status]
            return deepcopy(sorted(rows, key=lambda row: row["created_at"], reverse=True))

    def remember_idempotency(self, key: str, payload_hash: str, result: dict[str, Any], now: datetime) -> IdempotencyResult:
        with self._lock:
            existing = self.idempotency.get(key)
            if existing:
                if existing.get("payload_hash") != payload_hash:
                    raise AgentStateError(
                        "idempotency_conflict",
                        "idempotency key was already used with a different payload",
                        {"key": key},
                    )
                return IdempotencyResult(replay=True, result=deepcopy(existing.get("result")))
            self.idempotency[key] = {
                "kind": "idempotency",
                "idempotency_key": key,
                "payload_hash": payload_hash,
                "result": deepcopy(result),
                "created_at": _iso(now),
            }
            return IdempotencyResult(replay=False, result=None)

    def get_idempotency(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            return deepcopy(self.idempotency.get(key))

    def open_conflict(self, conflict: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            item = deepcopy(conflict)
            self.conflicts[item["conflict_id"]] = item
            return deepcopy(item)

    def resolve_conflict(self, conflict_id: str, resolution: str, now: datetime) -> dict[str, Any]:
        with self._lock:
            conflict = self._require(self.conflicts, conflict_id, "conflict_not_found")
            conflict["status"] = "resolved"
            conflict["resolution"] = resolution
            conflict["resolved_at"] = _iso(now)
            return deepcopy(conflict)

    def list_conflicts(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self.conflicts.values())
            if status:
                rows = [row for row in rows if row.get("status") == status]
            return deepcopy(sorted(rows, key=lambda row: row["created_at"], reverse=True))

    def append_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            self.events.append(deepcopy(event))

    def list_events(self, *, since_event_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.events
            if since_event_id:
                for index, event in enumerate(rows):
                    if event.get("event_id") == since_event_id:
                        rows = rows[index + 1 :]
                        break
            return deepcopy(rows[-limit:])

    def _expire_leases(self, now: datetime) -> None:
        for lease in self.leases.values():
            if lease.get("status") != "active":
                continue
            expires_at = _parse_time(lease["expires_at"])
            if expires_at <= now:
                lease["status"] = "expired"

    @staticmethod
    def _require(items: dict[str, dict[str, Any]], key: str, code: str) -> dict[str, Any]:
        item = items.get(key)
        if item is None:
            raise AgentStateError(code, f"{key} was not found", {"id": key})
        return item


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
