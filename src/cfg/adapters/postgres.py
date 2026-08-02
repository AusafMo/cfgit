# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""Postgres StorageAdapter for cfgit.

Runtime tables use the v1 Postgres contract: an id column named by `id_field`,
optional scalar columns used by `live_when`, and a `doc jsonb` column containing
the full versioned record.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
from typing import Any

from cfg.adapters.base import (
    AmbiguousConfig,
    ApplyItem,
    ApplyResult,
    AtomicityReport,
    HistoryEnvMismatch,
    NoSuchConfig,
    ReconcileReport,
    StaleHead,
    StaleLive,
    history_env_mismatch_message,
)
from cfg.core.config import ProjectConfig
from cfg.core.hashing import hash_doc

try:  # pragma: no cover - exercised only when postgres extra is installed
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ModuleNotFoundError as exc:  # pragma: no cover
    raise ModuleNotFoundError("install cfgit[postgres] to use PostgresAdapter") from exc


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PostgresAdapter:
    def __init__(self, *, project: ProjectConfig, env_name: str):
        env = project.envs[env_name]
        if not env.uri:
            raise ValueError(f"missing Postgres URI for env {env_name}")
        self.project = project
        self.env_name = env_name
        self.conn = psycopg.connect(env.uri, autocommit=True, row_factory=dict_row)
        self.history_table_name = project.history.history_collection
        self.heads_table_name = project.history.heads_collection
        self.refs_table_name = project.branches.refs_collection
        self.history_table = _ident(project.history.history_collection)
        self.heads_table = _ident(project.history.heads_collection)
        self.refs_table = _ident(project.branches.refs_collection)

    def get_record(self, collection: str, record_id: str) -> dict | None:
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT doc FROM {_ident(collection)} WHERE {self._runtime_where(collection)} LIMIT 2",
                self._runtime_params(collection, record_id),
            )
            rows = cur.fetchall()
        if len(rows) > 1:
            raise AmbiguousConfig(f"{collection}:{record_id}")
        return dict(rows[0]["doc"]) if rows else None

    def put_record(self, collection: str, record_id: str, doc: dict) -> None:
        with self.conn.transaction():
            self._put_record(collection, record_id, doc)

    def seed_record(self, collection: str, record_id: str, doc: dict) -> None:
        if self.get_record(collection, record_id):
            raise AmbiguousConfig(f"{collection}:{record_id}")
        self._seed_record(collection, record_id, doc)

    def _seed_record(self, collection: str, record_id: str, doc: dict) -> None:
        coll = self.project.collection(collection)
        columns = [_ident(coll.id_field)]
        values: list[Any] = [record_id]
        for key, configured_value in coll.live_when.items():
            columns.append(_ident(key))
            values.append(configured_value)
        columns.append("doc")
        values.append(Jsonb(_jsonable(self._runtime_doc(collection, record_id, doc))))
        placeholders = ", ".join(["%s"] * len(values))
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {_ident(collection)} ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )

    def list_record_ids(self, collection: str) -> list[str]:
        coll = self.project.collection(collection)
        sql = (
            f"SELECT DISTINCT {_ident(coll.id_field)} AS record_id "
            f"FROM {_ident(collection)} WHERE {self._live_where(collection)} ORDER BY 1"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, self._live_params(collection))
            return [str(row["record_id"]) for row in cur.fetchall() if row["record_id"] is not None]

    def get_records(self, collection: str, record_ids: list[str]) -> dict[str, dict]:
        """Batch of get_record: one runtime query for many ids instead of one per id.

        Raises AmbiguousConfig if any id resolves to more than one live row, matching
        get_record's single-row guarantee. Ids with no live row are simply absent.
        """
        if not record_ids:
            return {}
        coll = self.project.collection(collection)
        ids = list(record_ids)
        sql = (
            f"SELECT {_ident(coll.id_field)} AS record_id, doc FROM {_ident(collection)} "
            f"WHERE {_ident(coll.id_field)} = ANY(%s) AND {self._live_where(collection)}"
        )
        params: list[Any] = [ids, *self._live_params(collection)]
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        out: dict[str, dict] = {}
        for row in rows:
            rid = str(row["record_id"])
            if rid in out:
                raise AmbiguousConfig(f"{collection}:{rid}")
            out[rid] = dict(row["doc"])
        return out

    def get_heads(self, collection: str, record_ids: list[str]) -> dict[str, dict]:
        """Batch of get_head: one history-store query for many ids instead of one per id."""
        if not record_ids:
            return {}
        ids = list(record_ids)
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT h.*
                FROM {self.history_table} h
                JOIN {self.heads_table} p
                  ON p.env = h.env
                 AND p.collection_name = h.collection_name
                 AND p.record_id = h.record_id
                 AND p.head_seq = h.seq
                WHERE p.env = %s AND p.collection_name = %s AND p.record_id = ANY(%s)
                """,
                [self.env_name, collection, ids],
            )
            rows = cur.fetchall()
        return {str(row["record_id"]): _history_row(row) for row in rows}

    def get_head(self, collection: str, record_id: str) -> dict | None:
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT h.*
                FROM {self.history_table} h
                JOIN {self.heads_table} p
                  ON p.env = h.env
                 AND p.collection_name = h.collection_name
                 AND p.record_id = h.record_id
                 AND p.head_seq = h.seq
                WHERE p.env = %s AND p.collection_name = %s AND p.record_id = %s
                """,
                [self.env_name, collection, record_id],
            )
            row = cur.fetchone()
        return _history_row(row) if row else None

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
        clauses = ["env = %s"]
        params: list[Any] = [self.env_name]
        envless_clauses: list[str] = []
        envless_params: list[Any] = []

        def add_clause(clause: str, *values: Any) -> None:
            clauses.append(clause)
            params.extend(values)
            envless_clauses.append(clause)
            envless_params.extend(values)

        if collection is not None:
            add_clause("collection_name = %s", collection)
        if record_id is not None:
            add_clause("record_id = %s", record_id)
        if tag is not None:
            add_clause("%s = ANY(tags)", tag)
        if git_sha is not None:
            add_clause("%s = ANY(git_shas)", git_sha)
        if as_of_recorded is not None:
            add_clause("recorded_at <= %s", as_of_recorded)
        if as_of_valid is not None:
            add_clause("valid_from <= %s", as_of_valid)
            add_clause("(valid_to IS NULL OR valid_to > %s)", as_of_valid)
        if ref is not None:
            if ref.startswith("@"):
                add_clause("seq = %s", int(ref[1:]))
            else:
                oid = ref.removeprefix("sha256:").removeprefix("#")
                add_clause("oid LIKE %s", f"{oid}%")

        direction = "DESC" if order == "desc" else "ASC"
        sql = (
            f"SELECT * FROM {self.history_table} "
            f"WHERE {' AND '.join(clauses)} "
            f"ORDER BY collection_name ASC, record_id ASC, seq {direction}"
        )
        if limit is not None:
            sql += " LIMIT %s"
            params.append(limit)
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        result = [_history_row(row, with_doc=with_doc) for row in rows]
        if not result and limit != 0 and collection is not None and record_id is not None:
            self._raise_env_mismatch_if_history_exists(
                collection,
                record_id,
                envless_clauses=envless_clauses,
                envless_params=envless_params,
            )
        return result

    def list_tags(self) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT tag, count(*) AS count
                FROM {self.history_table}, unnest(tags) AS tag
                WHERE env = %s
                GROUP BY tag
                ORDER BY tag
                """,
                [self.env_name],
            )
            return [dict(row) for row in cur.fetchall()]

    def put_ref(self, doc: dict) -> None:
        self._put_ref_doc(doc)

    def _put_ref_doc(self, doc: dict) -> None:
        stored = dict(doc)
        stored["env"] = self.env_name
        stored["id"] = str(stored["id"])
        stored["type"] = str(stored["type"])
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {self.refs_table}
                    (env, type, id, branch, status, created_at, updated_at, doc)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (env, type, id)
                DO UPDATE SET
                    branch = EXCLUDED.branch,
                    status = EXCLUDED.status,
                    updated_at = EXCLUDED.updated_at,
                    doc = EXCLUDED.doc
                """,
                [
                    self.env_name,
                    stored["type"],
                    stored["id"],
                    stored.get("branch") or stored.get("head_branch") or stored.get("name"),
                    stored.get("status"),
                    stored.get("created_at") or self.now(),
                    stored.get("updated_at") or stored.get("created_at") or self.now(),
                    Jsonb(_jsonable(stored)),
                ],
            )

    def get_ref(self, ref_type: str, ref_id: str) -> dict | None:
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT doc FROM {self.refs_table} WHERE env = %s AND type = %s AND id = %s",
                [self.env_name, ref_type, ref_id],
            )
            row = cur.fetchone()
        return dict(row["doc"]) if row else None

    def list_refs(self, ref_type: str, **filters) -> list[dict]:
        clauses = ["env = %s", "type = %s"]
        params: list[Any] = [self.env_name, ref_type]
        for key, value in filters.items():
            if value is None:
                continue
            clauses.append("doc ->> %s = %s")
            params.extend([key, str(value)])
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT doc FROM {self.refs_table} WHERE {' AND '.join(clauses)} ORDER BY created_at ASC, id ASC",
                params,
            )
            return [dict(row["doc"]) for row in cur.fetchall()]

    def delete_ref(self, ref_type: str, ref_id: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {self.refs_table} WHERE env = %s AND type = %s AND id = %s",
                [self.env_name, ref_type, ref_id],
            )

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
            except psycopg.Error as exc:
                if attempt >= 2 or getattr(exc, "sqlstate", None) not in {"40001", "40P01"}:
                    raise
        raise RuntimeError("unreachable")

    def apply_many(self, *, items: list[ApplyItem], put_refs: list[dict] | None = None) -> list[ApplyResult]:
        if not items:
            return []
        for attempt in range(3):
            try:
                return self._apply_many_once(items=items, put_refs=put_refs or [])
            except psycopg.Error as exc:
                if attempt >= 2 or getattr(exc, "sqlstate", None) not in {"40001", "40P01"}:
                    raise
        raise RuntimeError("unreachable")

    def _apply_many_once(self, *, items: list[ApplyItem], put_refs: list[dict]) -> list[ApplyResult]:
        with self.conn.transaction():
            results = [self._apply_item(item) for item in items]
            for doc in put_refs:
                self._put_ref_doc(doc)
        return results

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
        with self.conn.transaction():
            result = self._apply_item(
                ApplyItem(
                    collection=collection,
                    record_id=record_id,
                    new_doc=new_doc,
                    entry=entry,
                    expected_head_oid=expected_head_oid,
                    expected_live_oid=expected_live_oid,
                    make_head=make_head,
                    seed_missing=seed_missing,
                )
            )
        return result

    def _apply_item(self, item: ApplyItem) -> ApplyResult:
        collection = item.collection
        record_id = item.record_id
        coll_cfg = self.project.collection(collection)
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM {self.heads_table}
                WHERE env = %s AND collection_name = %s AND record_id = %s
                FOR UPDATE
                """,
                [self.env_name, collection, record_id],
            )
            ptr = cur.fetchone()
            current_head = ptr["head_oid"] if ptr else None
            if current_head != item.expected_head_oid:
                raise StaleHead(current_head)

            if item.expected_live_oid is not None:
                live = self._get_record_for_update(collection, record_id)
                if live is None:
                    raise NoSuchConfig(f"{collection}:{record_id}")
                live_oid = hash_doc(live, coll_cfg)
                if live_oid != item.expected_live_oid:
                    raise StaleLive(live_oid)

            seq = int(ptr["head_seq"]) + 1 if ptr else 1
            entry = dict(item.entry)
            entry.update(
                {
                    "env": self.env_name,
                    "collection": collection,
                    "record_id": record_id,
                    "seq": seq,
                }
            )
            self._insert_history(entry)

            if current_head:
                cur.execute(
                    f"""
                    UPDATE {self.history_table}
                    SET valid_to = %s
                    WHERE env = %s
                      AND collection_name = %s
                      AND record_id = %s
                      AND seq = %s
                      AND valid_to IS NULL
                    """,
                    [entry["valid_from"], self.env_name, collection, record_id, ptr["head_seq"]],
                )

            if item.new_doc is not None:
                if item.seed_missing:
                    if self._get_record_for_update(collection, record_id) is not None:
                        raise StaleLive(f"{collection}:{record_id} reappeared before restore")
                    self._seed_record(collection, record_id, item.new_doc)
                else:
                    self._put_record(collection, record_id, item.new_doc)

            if item.make_head:
                cur.execute(
                    f"""
                    INSERT INTO {self.heads_table}
                        (env, collection_name, record_id, head_oid, head_seq, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (env, collection_name, record_id)
                    DO UPDATE SET
                        head_oid = EXCLUDED.head_oid,
                        head_seq = EXCLUDED.head_seq,
                        updated_at = EXCLUDED.updated_at
                    """,
                    [
                        self.env_name,
                        collection,
                        record_id,
                        entry["oid"],
                        seq,
                        entry["recorded_at"],
                    ],
                )

        return ApplyResult(
            collection=collection,
            record_id=record_id,
            seq=seq,
            oid=entry["oid"],
            head_oid=entry["oid"],
        )

    def add_tag(self, *, collection: str, record_id: str, seq: int, tag: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {self.history_table}
                SET tags = CASE WHEN %s = ANY(tags) THEN tags ELSE array_append(tags, %s) END
                WHERE env = %s AND collection_name = %s AND record_id = %s AND seq = %s
                """,
                [tag, tag, self.env_name, collection, record_id, seq],
            )

    def remove_tag(self, *, collection: str, record_id: str, seq: int, tag: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {self.history_table}
                SET tags = array_remove(tags, %s)
                WHERE env = %s AND collection_name = %s AND record_id = %s AND seq = %s
                """,
                [tag, self.env_name, collection, record_id, seq],
            )

    def list_pending(self) -> list[dict]:
        return []

    def reconcile(self) -> ReconcileReport:
        return ReconcileReport(rolled_forward=[], rolled_back=[])

    def ensure_schema(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.history_table} (
                    env text NOT NULL,
                    collection_name text NOT NULL,
                    record_id text NOT NULL,
                    seq bigint NOT NULL,
                    oid text NOT NULL,
                    parent_oid text,
                    doc jsonb NOT NULL,
                    message text NOT NULL,
                    author text NOT NULL,
                    recorded_at timestamptz NOT NULL,
                    valid_from timestamptz NOT NULL,
                    valid_to timestamptz,
                    valid_from_estimated boolean NOT NULL DEFAULT false,
                    op text NOT NULL,
                    git_shas text[] NOT NULL DEFAULT '{{}}',
                    tags text[] NOT NULL DEFAULT '{{}}',
                    meta jsonb NOT NULL DEFAULT '{{}}',
                    PRIMARY KEY (env, collection_name, record_id, seq)
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.heads_table} (
                    env text NOT NULL,
                    collection_name text NOT NULL,
                    record_id text NOT NULL,
                    head_oid text NOT NULL,
                    head_seq bigint NOT NULL,
                    updated_at timestamptz NOT NULL,
                    PRIMARY KEY (env, collection_name, record_id)
                )
                """
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {_ident(self.history_table_name + '_oid_idx')} "
                f"ON {self.history_table} (env, collection_name, record_id, oid)"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {_ident(self.history_table_name + '_recorded_idx')} "
                f"ON {self.history_table} (env, recorded_at)"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {_ident(self.history_table_name + '_valid_from_idx')} "
                f"ON {self.history_table} (env, valid_from)"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {_ident(self.history_table_name + '_valid_to_idx')} "
                f"ON {self.history_table} (env, valid_to)"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {_ident(self.history_table_name + '_tags_idx')} "
                f"ON {self.history_table} USING GIN (tags)"
            )
            if self.project.branches.enabled:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.refs_table} (
                        env text NOT NULL,
                        type text NOT NULL,
                        id text NOT NULL,
                        branch text,
                        status text,
                        created_at timestamptz NOT NULL,
                        updated_at timestamptz NOT NULL,
                        doc jsonb NOT NULL,
                        PRIMARY KEY (env, type, id)
                    )
                    """
                )
                cur.execute(
                    f"CREATE INDEX IF NOT EXISTS {_ident(self.refs_table_name + '_branch_idx')} "
                    f"ON {self.refs_table} (env, type, branch, created_at)"
                )
                cur.execute(
                    f"CREATE INDEX IF NOT EXISTS {_ident(self.refs_table_name + '_status_idx')} "
                    f"ON {self.refs_table} (env, type, status, updated_at)"
                )

    def check_runtime_invariant(self, collection: str | None = None) -> list[str]:
        names = [collection] if collection else [c.name for c in self.project.collections]
        violations: list[str] = []
        for name in names:
            coll = self.project.collection(name)
            sql = (
                f"SELECT {_ident(coll.id_field)} AS record_id, count(*) AS n "
                f"FROM {_ident(name)} WHERE {self._live_where(name)} "
                f"GROUP BY {_ident(coll.id_field)} HAVING count(*) > 1 ORDER BY 1"
            )
            with self.conn.cursor() as cur:
                cur.execute(sql, self._live_params(name))
                for row in cur.fetchall():
                    violations.append(f"{name}:{row['record_id']} ({row['n']} live records)")
        return violations

    def check_atomicity_scope(self) -> AtomicityReport:
        return AtomicityReport(
            atomic=True,
            runtime_cluster=self._server_name(),
            history_cluster=self._server_name(),
            reason="ok",
        )

    def backend_name(self) -> str:
        return "postgres"

    def supports_transactions(self) -> bool:
        return True

    def authenticated_principal(self) -> str | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT current_user AS principal")
            row = cur.fetchone()
        principal = str(row["principal"] or "").strip()
        return principal or None

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _insert_history(self, entry: dict[str, Any]) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {self.history_table} (
                    env, collection_name, record_id, seq, oid, parent_oid, doc, message,
                    author, recorded_at, valid_from, valid_to, valid_from_estimated,
                    op, git_shas, tags, meta
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                [
                    entry["env"],
                    entry["collection"],
                    entry["record_id"],
                    entry["seq"],
                    entry["oid"],
                    entry.get("parent_oid"),
                    Jsonb(_jsonable(entry["doc"])),
                    entry["message"],
                    entry["author"],
                    entry["recorded_at"],
                    entry["valid_from"],
                    entry.get("valid_to"),
                    bool(entry.get("valid_from_estimated", False)),
                    entry["op"],
                    list(entry.get("git_shas") or []),
                    list(entry.get("tags") or []),
                    Jsonb(_jsonable(entry.get("meta") or {})),
                ],
            )

    def _get_record_for_update(self, collection: str, record_id: str) -> dict | None:
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT doc FROM {_ident(collection)} WHERE {self._runtime_where(collection)} FOR UPDATE",
                self._runtime_params(collection, record_id),
            )
            rows = cur.fetchall()
        if len(rows) > 1:
            raise AmbiguousConfig(f"{collection}:{record_id}")
        return dict(rows[0]["doc"]) if rows else None

    def _put_record(self, collection: str, record_id: str, doc: dict) -> None:
        coll = self.project.collection(collection)
        current = self._get_record_for_update(collection, record_id)
        if current is None:
            raise NoSuchConfig(f"{collection}:{record_id}")

        effective = self._runtime_doc(collection, record_id, doc)
        for path in coll.secret_fields:
            if _get_path(effective, path) is None:
                secret_value = _get_path(current, path)
                if secret_value is not None:
                    _set_path(effective, path, secret_value)

        set_parts = ["doc = %s", f"{_ident(coll.id_field)} = %s"]
        params: list[Any] = [Jsonb(_jsonable(effective)), record_id]
        for key, configured_value in coll.live_when.items():
            set_parts.append(f"{_ident(key)} = %s")
            params.append(effective.get(key, configured_value))
        params.extend(self._runtime_params(collection, record_id))

        with self.conn.cursor() as cur:
            cur.execute(
                f"UPDATE {_ident(collection)} SET {', '.join(set_parts)} "
                f"WHERE {self._runtime_where(collection)}",
                params,
            )
            if cur.rowcount == 0:
                raise NoSuchConfig(f"{collection}:{record_id}")

    def _runtime_doc(self, collection: str, record_id: str, doc: dict) -> dict[str, Any]:
        coll = self.project.collection(collection)
        effective = deepcopy(doc)
        effective[coll.id_field] = record_id
        for key, configured_value in coll.live_when.items():
            effective[key] = configured_value
        return effective

    def _runtime_where(self, collection: str) -> str:
        coll = self.project.collection(collection)
        return f"{_ident(coll.id_field)} = %s AND {self._live_where(collection)}"

    def _runtime_params(self, collection: str, record_id: str) -> list[Any]:
        return [record_id, *self._live_params(collection)]

    def _live_where(self, collection: str) -> str:
        coll = self.project.collection(collection)
        if not coll.live_when:
            return "TRUE"
        parts = []
        for key, value in coll.live_when.items():
            if value is None:
                parts.append(f"{_ident(key)} IS NULL")
            else:
                parts.append(f"{_ident(key)} = %s")
        return " AND ".join(parts)

    def _live_params(self, collection: str) -> list[Any]:
        coll = self.project.collection(collection)
        return [value for value in coll.live_when.values() if value is not None]

    def _server_name(self) -> str:
        with self.conn.cursor() as cur:
            cur.execute("SELECT inet_server_addr()::text AS addr, inet_server_port() AS port")
            row = cur.fetchone()
        return f"{row['addr']}:{row['port']}"

    def _raise_env_mismatch_if_history_exists(
        self,
        collection: str,
        record_id: str,
        *,
        envless_clauses: list[str],
        envless_params: list[Any],
    ) -> None:
        envs: set[str] = set()
        where = " AND ".join(envless_clauses)
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT env FROM {self.history_table} WHERE {where}",
                envless_params,
            )
            envs.update(str(row["env"]) for row in cur.fetchall() if row.get("env"))
            if envs and self.env_name in envs:
                return
            if envless_clauses == ["collection_name = %s", "record_id = %s"]:
                cur.execute(
                    f"""
                    SELECT DISTINCT env
                    FROM {self.heads_table}
                    WHERE collection_name = %s AND record_id = %s
                    """,
                    [collection, record_id],
                )
                envs.update(str(row["env"]) for row in cur.fetchall() if row.get("env"))
                if self.env_name in envs:
                    return
        other_envs = sorted(env for env in envs if env != self.env_name)
        if other_envs:
            raise HistoryEnvMismatch(
                history_env_mismatch_message(
                    collection=collection,
                    record_id=record_id,
                    current_env=self.env_name,
                    other_envs=other_envs,
                )
            )


def _history_row(row: dict[str, Any], *, with_doc: bool = True) -> dict[str, Any]:
    out = {
        "env": row["env"],
        "collection": row["collection_name"],
        "record_id": row["record_id"],
        "seq": row["seq"],
        "oid": row["oid"],
        "parent_oid": row["parent_oid"],
        "message": row["message"],
        "author": row["author"],
        "recorded_at": row["recorded_at"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "valid_from_estimated": row["valid_from_estimated"],
        "op": row["op"],
        "git_shas": list(row["git_shas"] or []),
        "tags": list(row["tags"] or []),
        "meta": dict(row["meta"] or {}),
    }
    if with_doc:
        out["doc"] = dict(row["doc"])
    return out


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
