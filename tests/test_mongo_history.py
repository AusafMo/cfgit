from __future__ import annotations

import json

import pytest

from cfg.adapters.base import HistoryEnvMismatch
from cfg.interfaces.actions import to_json


def test_mongo_history_row_strips_storage_object_id() -> None:
    bson = pytest.importorskip("bson")
    from cfg.adapters.mongo import _history_row

    row = {
        "_id": bson.ObjectId(),
        "env": "dev",
        "collection": "demo",
        "record_id": "alpha",
        "seq": 1,
        "oid": "abc",
        "doc": {"id": "alpha"},
    }

    without_doc = _history_row(row, with_doc=False)
    with_doc = _history_row(row, with_doc=True)

    assert "_id" not in without_doc
    assert "doc" not in without_doc
    assert with_doc["doc"] == {"id": "alpha"}
    json.dumps(to_json(with_doc))


def test_mongo_query_history_reports_env_mismatch() -> None:
    pytest.importorskip("pymongo")
    from cfg.adapters.mongo import MongoAdapter

    adapter = object.__new__(MongoAdapter)
    adapter.env_name = "dev"
    adapter.history = _FakeHistory(["prod"])
    adapter.heads = _FakeHistory([])

    with pytest.raises(HistoryEnvMismatch) as caught:
        adapter.query_history(collection="demo", record_id="alpha")

    message = str(caught.value)
    assert "demo:alpha" in message
    assert "env='dev'" in message
    assert "prod" in message
    assert adapter.history.find_query == {"env": "dev", "collection": "demo", "record_id": "alpha"}


class _EmptyCursor:
    def sort(self, *_args):
        return self

    def limit(self, _limit):
        return self

    def __iter__(self):
        return iter([])


class _FakeHistory:
    def __init__(self, envs: list[str]):
        self.envs = envs
        self.find_query = None

    def find(self, query, _projection=None):
        self.find_query = query
        return _EmptyCursor()

    def distinct(self, field: str, query: dict):
        assert field == "env"
        assert query == {"collection": "demo", "record_id": "alpha"}
        return self.envs
