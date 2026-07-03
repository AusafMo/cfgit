from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone, timedelta
import re
from typing import Any

from cfg.core.config import ProjectConfig
from cfg_agent.resources import parse_resource, resources_overlap
from cfg_agent.state import AgentStateError, IdempotencyResult, _iso, utcnow

try:  # pragma: no cover - exercised when cfgit[postgres] is installed
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ModuleNotFoundError as exc:  # pragma: no cover
    raise ModuleNotFoundError("install cfgit[postgres] to use PostgresAgentStateAdapter") from exc


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PostgresAgentStateAdapter:
    """Postgres-backed coordination state for multi-process agent runtimes."""

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
            raise ValueError(f"missing Postgres URI for env {env_name}")
        self.env_name = env_name
        self.conn = psycopg.connect(env.history_uri or env.uri, autocommit=True, row_factory=dict_row)
        self.state_table_name = state_collection
        self.events_table_name = events_collection
        self.state_table = _ident(state_collection)
        self.events_table = _ident(events_collection)

    def init_agent_state(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.state_table} (
                    env text NOT NULL,
                    kind text NOT NULL,
                    id text NOT NULL,
                    status text,
                    resource text,
                    updated_at timestamptz NOT NULL,
                    doc jsonb NOT NULL,
                    PRIMARY KEY (env, kind, id)
                )
                """
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {_ident(self.state_table_name + '_status_idx')} "
                f"ON {self.state_table} (env, kind, status)"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {_ident(self.state_table_name + '_resource_idx')} "
                f"ON {self.state_table} (env, kind, resource)"
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.events_table} (
                    env text NOT NULL,
                    event_id text NOT NULL,
                    event text NOT NULL,
                    session_id text,
                    actor text,
                    resource text,
                    recorded_at timestamptz NOT NULL,
                    doc jsonb NOT NULL,
                    PRIMARY KEY (env, event_id)
                )
                """
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {_ident(self.events_table_name + '_recorded_idx')} "
                f"ON {self.events_table} (env, recorded_at)"
            )

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
        with self.conn.transaction():
            self._touch_lock("leases", now)
            self._expire_leases(now)
            resource = parse_resource(lease["resource"])
            conflicts = []
            for existing in self._list_state("lease", status="active"):
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
            return self._insert_state("lease", lease["lease_id"], lease)

    def renew_lease(self, lease_id: str, *, ttl_seconds: int, now: datetime) -> dict[str, Any]:
        lease = self._require("lease", lease_id, "lease_not_found")
        if lease.get("status") != "active":
            raise AgentStateError("lease_not_active", "lease is not active", {"lease_id": lease_id})
        lease["expires_at"] = _iso(now + timedelta(seconds=ttl_seconds))
        return self._put_state("lease", lease_id, lease)

    def release_lease(self, lease_id: str, *, now: datetime) -> dict[str, Any]:
        lease = self._require("lease", lease_id, "lease_not_found")
        if lease.get("status") == "active":
            lease["status"] = "released"
            lease["released_at"] = _iso(now)
        return self._put_state("lease", lease_id, lease)

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
        existing = self._get_state("idempotency", key)
        if existing:
            if existing.get("payload_hash") != payload_hash:
                raise AgentStateError(
                    "idempotency_conflict",
                    "idempotency key was already used with a different payload",
                    {"key": key},
                )
            return IdempotencyResult(replay=True, result=deepcopy(existing.get("result")))
        self._insert_state(
            "idempotency",
            key,
            {
                "kind": "idempotency",
                "idempotency_key": key,
                "payload_hash": payload_hash,
                "result": deepcopy(result),
                "created_at": _iso(now),
            },
        )
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
        doc = deepcopy(event)
        recorded_at = _parse_recorded_at(doc["recorded_at"])
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {self.events_table}
                    (env, event_id, event, session_id, actor, resource, recorded_at, doc)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    self.env_name,
                    doc["event_id"],
                    doc["event"],
                    doc.get("session_id"),
                    doc.get("actor"),
                    doc.get("resource"),
                    recorded_at,
                    Jsonb(_jsonable(doc)),
                ],
            )

    def list_events(self, *, since_event_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT doc FROM {self.events_table} WHERE env = %s ORDER BY recorded_at ASC, event_id ASC",
                [self.env_name],
            )
            rows = [dict(row["doc"]) for row in cur.fetchall()]
        if since_event_id:
            for index, event in enumerate(rows):
                if event.get("event_id") == since_event_id:
                    rows = rows[index + 1 :]
                    break
        return deepcopy(rows[-limit:])

    def _insert_state(self, kind: str, item_id: str, doc: dict[str, Any]) -> dict[str, Any]:
        if self._get_state(kind, item_id) is not None:
            raise AgentStateError("state_conflict", f"{kind} already exists", {"id": item_id})
        return self._put_state(kind, item_id, doc)

    def _put_state(self, kind: str, item_id: str, doc: dict[str, Any]) -> dict[str, Any]:
        item = deepcopy(doc)
        status = item.get("status")
        resource = item.get("resource")
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {self.state_table} (env, kind, id, status, resource, updated_at, doc)
                VALUES (%s, %s, %s, %s, %s, now(), %s)
                ON CONFLICT (env, kind, id)
                DO UPDATE SET
                    status = EXCLUDED.status,
                    resource = EXCLUDED.resource,
                    updated_at = EXCLUDED.updated_at,
                    doc = EXCLUDED.doc
                """,
                [self.env_name, kind, item_id, status, resource, Jsonb(_jsonable(item))],
            )
        return item

    def _get_state(self, kind: str, item_id: str) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT doc FROM {self.state_table} WHERE env = %s AND kind = %s AND id = %s",
                [self.env_name, kind, item_id],
            )
            row = cur.fetchone()
        return dict(row["doc"]) if row else None

    def _require(self, kind: str, item_id: str, code: str) -> dict[str, Any]:
        item = self._get_state(kind, item_id)
        if item is None:
            raise AgentStateError(code, f"{item_id} was not found", {"id": item_id})
        return item

    def _list_state(self, kind: str, *, status: str | None = None, sort_field: str = "created_at") -> list[dict[str, Any]]:
        clauses = ["env = %s", "kind = %s"]
        params: list[Any] = [self.env_name, kind]
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT doc FROM {self.state_table} WHERE {' AND '.join(clauses)} ORDER BY doc ->> %s DESC",
                [*params, sort_field],
            )
            return [dict(row["doc"]) for row in cur.fetchall()]

    def _expire_leases(self, now: datetime) -> None:
        now_iso = _iso(now)
        for lease in self._list_state("lease", status="active"):
            if str(lease.get("expires_at") or "") <= now_iso:
                lease["status"] = "expired"
                self._put_state("lease", lease["lease_id"], lease)

    def _touch_lock(self, lock_id: str, now: datetime) -> None:
        self._put_state("lock", lock_id, {"kind": "lock", "id": lock_id, "updated_at": _iso(now)})
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT doc FROM {self.state_table} WHERE env = %s AND kind = 'lock' AND id = %s FOR UPDATE",
                [self.env_name, lock_id],
            )
            cur.fetchone()


def _ident(value: str) -> str:
    if not _IDENT_RE.match(value):
        raise ValueError(f"unsafe SQL identifier: {value}")
    return f'"{value}"'


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return value


def _parse_recorded_at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
