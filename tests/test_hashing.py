from __future__ import annotations

from cfg.core.config import CollectionConfig
from cfg.core.hashing import hash_doc, stored_doc, strip_for_hash


def test_hash_strips_ignored_and_secret_fields() -> None:
    coll = CollectionConfig(
        name="demo",
        id_field="config_id",
        ignore_fields=("updated_at",),
        ignore_patterns=("backup_*",),
        secret_fields=("provider_config.api_key",),
    )
    live = {
        "config_id": "alpha",
        "value": 1,
        "updated_at": "later",
        "backup_2026": "old",
        "provider_config": {"model": "x", "api_key": "secret"},
    }
    stored = stored_doc(live, coll)

    assert stored == {
        "config_id": "alpha",
        "value": 1,
        "updated_at": "later",
        "backup_2026": "old",
        "provider_config": {"model": "x"},
    }
    assert strip_for_hash(live, coll) == {
        "config_id": "alpha",
        "value": 1,
        "provider_config": {"model": "x"},
    }
    assert hash_doc(live, coll) == hash_doc(stored, coll)


def test_hash_treats_null_as_missing_and_int_float_as_equal() -> None:
    coll = CollectionConfig(name="demo", id_field="id")

    assert hash_doc({"id": "a", "value": 1, "empty": None}, coll) == hash_doc(
        {"id": "a", "value": 1.0},
        coll,
    )
