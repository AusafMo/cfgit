from __future__ import annotations

from pathlib import Path
import json
import os
from urllib.parse import urlparse
import uuid

import pytest

from cfg.core.config import CollectionConfig, EnvConfig, HistoryConfig, ProjectConfig
from cfg.core.engine import Engine, RecordRef
from cfg.interfaces.actions import to_json


def test_live_mongo_history_contract_when_local_uri_is_set() -> None:
    uri = _local_test_uri(os.environ.get("CFGIT_TEST_MONGO_URI") or os.environ.get("DEV_MONGODB_URI"))
    if not uri:
        pytest.skip("set CFGIT_TEST_MONGO_URI or local DEV_MONGODB_URI to run live Mongo contract")
    pymongo = pytest.importorskip("pymongo")

    suffix = uuid.uuid4().hex[:10]
    db_name = f"cfgit_contract_{suffix}"
    collection = "widgets"
    project = ProjectConfig(
        name="mongo-contract",
        path=Path("/tmp/.cfg.toml"),
        history=HistoryConfig(
            history_collection=f"history_{suffix}",
            heads_collection=f"heads_{suffix}",
        ),
        collections=(
            CollectionConfig(
                name=collection,
                id_field="id",
                live_when={"active": True},
                ignore_fields=("_id",),
            ),
        ),
        envs={"dev": EnvConfig(name="dev", database="mongo", uri=uri, db=db_name)},
    )
    client = pymongo.MongoClient(uri)
    try:
        client[db_name][collection].insert_one({"id": "alpha", "active": True, "value": 1})
        engine = _engine(project)
        engine.init()
        engine.import_records(RecordRef(collection, "alpha"), message="initial import")

        assert engine.status(RecordRef(collection, "alpha"))[0].state == "clean"
        rows = engine.log(RecordRef(collection, "alpha"))
        assert rows and "_id" not in rows[0] and "doc" not in rows[0]
        json.dumps(to_json(rows))

        client[db_name][collection].delete_one({"id": "alpha", "active": True})
        restored = engine.restore_system_as_of(_future_when(), message="restore deleted")
        assert any(item.get("state") == "restored_deleted" for item in restored["results"])
    finally:
        client.drop_database(db_name)


def test_live_postgres_history_contract_when_local_uri_is_set() -> None:
    uri = _local_test_uri(os.environ.get("CFGIT_TEST_POSTGRES_URI") or os.environ.get("CFGIT_POSTGRES_URI"))
    if not uri:
        pytest.skip("set CFGIT_TEST_POSTGRES_URI or local CFGIT_POSTGRES_URI to run live Postgres contract")
    psycopg = pytest.importorskip("psycopg")
    json_types = pytest.importorskip("psycopg.types.json")

    suffix = uuid.uuid4().hex[:10]
    table = f"widgets_{suffix}"
    history = f"history_{suffix}"
    heads = f"heads_{suffix}"
    project = ProjectConfig(
        name="postgres-contract",
        path=Path("/tmp/.cfg.toml"),
        history=HistoryConfig(history_collection=history, heads_collection=heads),
        collections=(
            CollectionConfig(name=table, id_field="id", live_when={"active": True}),
        ),
        envs={"dev": EnvConfig(name="dev", database="postgres", uri=uri, db="")},
    )
    conn = psycopg.connect(uri, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE TABLE "{table}" (id text PRIMARY KEY, active boolean NOT NULL, doc jsonb NOT NULL)')
            cur.execute(
                f'INSERT INTO "{table}" (id, active, doc) VALUES (%s, %s, %s)',
                ["alpha", True, json_types.Jsonb({"id": "alpha", "active": True, "value": 1})],
            )
        engine = _engine(project)
        engine.init()
        engine.import_records(RecordRef(table, "alpha"), message="initial import")

        assert engine.status(RecordRef(table, "alpha"))[0].state == "clean"
        rows = engine.log(RecordRef(table, "alpha"))
        assert rows and "doc" not in rows[0]
        json.dumps(to_json(rows))

        with conn.cursor() as cur:
            cur.execute(f'DELETE FROM "{table}" WHERE id = %s AND active = %s', ["alpha", True])
        restored = engine.restore_system_as_of(_future_when(), message="restore deleted")
        assert any(item.get("state") == "restored_deleted" for item in restored["results"])
    finally:
        with conn.cursor() as cur:
            for name in (table, history, heads):
                cur.execute(f'DROP TABLE IF EXISTS "{name}"')
        conn.close()


def _engine(project: ProjectConfig) -> Engine:
    from cfg.interfaces.actions import engine_for_project

    return engine_for_project(project, env_name="dev", author="contract@example.com")


def _future_when():
    from datetime import datetime, timezone

    return datetime(2100, 1, 1, tzinfo=timezone.utc)


def _local_test_uri(uri: str | None) -> str | None:
    if not uri:
        return None
    parsed = urlparse(uri)
    host = parsed.hostname
    if host in {"localhost", "127.0.0.1", "::1"}:
        return uri
    return None
