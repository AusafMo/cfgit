from __future__ import annotations

import json

import pytest

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
