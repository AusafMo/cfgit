# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""cfg set: dotted-path scalar edits that route through the drift-guarded commit path."""
from __future__ import annotations

import pytest

from cfg.core.config import SecretsConfig
from cfg.core.engine import RecordRef, SecretBlocked, _set_dotted
from cfg.interfaces import actions

from test_engine_safety import _engine


# --- _set_dotted unit tests -----------------------------------------------------------------


def test_set_dotted_nested_creates_intermediate():
    doc = {"id": "a"}
    _set_dotted(doc, "retry.max", 3)
    assert doc == {"id": "a", "retry": {"max": 3}}


def test_set_dotted_list_index():
    doc = {"tags": ["x", "y", "z"]}
    _set_dotted(doc, "tags[1]", "Y")
    assert doc["tags"] == ["x", "Y", "z"]


def test_set_dotted_list_index_out_of_range_raises():
    doc = {"tags": ["x"]}
    with pytest.raises(ValueError, match="out of range"):
        _set_dotted(doc, "tags[5]", "boom")


def test_set_dotted_on_non_object_raises():
    doc = {"n": 1}
    with pytest.raises(ValueError):
        _set_dotted(doc, "n.deep", 2)


# --- value coercion -------------------------------------------------------------------------


def test_parse_assignments_json_coercion_and_str_escape():
    got = dict(actions.parse_assignments(["enabled=true", "n=5", "tags=[\"a\",\"b\"]", "ver=str:1.0"]))
    assert got["enabled"] is True
    assert got["n"] == 5
    assert got["tags"] == ["a", "b"]
    assert got["ver"] == "1.0"  # str: forced string, not float


def test_parse_assignments_bare_string_falls_back():
    got = dict(actions.parse_assignments(["model=gpt-4o"]))
    assert got["model"] == "gpt-4o"


# --- routes through commit (the safety property) --------------------------------------------


def test_set_writes_via_commit_and_updates_history():
    engine, adapter = _engine(records={("demo", "alpha"): {"id": "alpha", "value": 1}})
    engine.commit(RecordRef("demo", "alpha"), {"id": "alpha", "value": 1}, message="seed")
    before = len(adapter.history)

    result, code = actions.set_fields(
        engine, "demo:alpha", [("value", 2)], message="bump value"
    )
    assert result["state"] == "committed"
    assert len(adapter.history) == before + 1


def test_set_refuses_on_drift_like_a_real_commit():
    engine, adapter = _engine(records={("demo", "alpha"): {"id": "alpha", "value": 1}})
    engine.commit(RecordRef("demo", "alpha"), {"id": "alpha", "value": 1}, message="seed")
    adapter.records[("demo", "alpha")] = {"id": "alpha", "value": 99}  # out-of-band write
    before = len(adapter.history)

    result, code = actions.set_fields(engine, "demo:alpha", [("value", 2)], message="bump")
    assert result["state"] == "changed_outside_cfgit"
    assert code == actions.EXIT_DIRTY
    assert len(adapter.history) == before  # nothing written — inherited clobber guard


def test_set_dry_run_previews_without_writing():
    engine, adapter = _engine(records={("demo", "alpha"): {"id": "alpha", "value": 1}})
    engine.commit(RecordRef("demo", "alpha"), {"id": "alpha", "value": 1}, message="seed")
    before = len(adapter.history)

    result, code = actions.set_fields(engine, "demo:alpha", [("value", 2)], message="(dry-run)", dry_run=True)
    assert result["state"] == "would_commit"
    assert len(adapter.history) == before


def test_set_secret_policy_still_applies():
    engine, adapter = _engine(
        records={("demo", "alpha"): {"id": "alpha", "value": 1}},
        secrets=SecretsConfig(block_fields=("*api_key*",), block_values=("sk-[A-Za-z0-9]{6,}",)),
    )
    engine.commit(RecordRef("demo", "alpha"), {"id": "alpha", "value": 1}, message="seed")

    with pytest.raises(SecretBlocked):
        actions.set_fields(engine, "demo:alpha", [("api_key", "sk-ABCDEF1234")], message="leak")


def test_assignments_from_payload_accepts_dict_and_list():
    from_dict = actions._assignments_from_payload({"a": 1, "b.c": True})
    assert ("a", 1) in from_dict and ("b.c", True) in from_dict
    from_list = actions._assignments_from_payload(["x=1"])
    assert from_list == [("x", 1)]
