# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""cfg export snapshot + import --from round-trip + --bulk-from --dry-run + drift nudge."""
from __future__ import annotations

import pytest

from cfg.core.engine import RecordRef
from cfg.interfaces import actions

from test_engine_safety import _engine


# --- export ---------------------------------------------------------------------------------


def test_export_dumps_live_docs_stamped_with_head():
    engine, adapter = _engine(records={
        ("demo", "a"): {"id": "a", "value": 1},
        ("demo", "b"): {"id": "b", "value": 2},
    })
    engine.commit(RecordRef("demo", "a"), {"id": "a", "value": 1}, message="seed a")

    snap = engine.export_records()
    assert snap["kind"] == "cfgit-export"
    assert snap["count"] == 2
    by_rec = {i["record"]: i for i in snap["items"]}
    # tracked record carries its head seq/oid; untracked record has null head
    assert by_rec["demo:a"]["head_seq"] is not None
    assert by_rec["demo:a"]["doc"] == {"id": "a", "value": 1}
    assert by_rec["demo:b"]["head_seq"] is None


def test_export_single_record():
    engine, _ = _engine(records={("demo", "a"): {"id": "a", "value": 1}, ("demo", "b"): {"id": "b"}})
    snap = engine.export_records(RecordRef("demo", "a"))
    assert snap["count"] == 1
    assert snap["items"][0]["record"] == "demo:a"


def test_export_writes_nothing():
    engine, adapter = _engine(records={("demo", "a"): {"id": "a", "value": 1}})
    before = len(adapter.history)
    engine.export_records()
    assert len(adapter.history) == before


# --- import --from (round-trip via drift-guarded commit) ------------------------------------


def test_export_then_import_round_trip_restores_docs():
    engine, adapter = _engine(records={("demo", "a"): {"id": "a", "value": 1}})
    engine.commit(RecordRef("demo", "a"), {"id": "a", "value": 1}, message="seed")
    snap = engine.export_records()  # capture value=1

    # mutate live away from the snapshot, adopt so there's no drift blocking the restore
    adapter.records[("demo", "a")] = {"id": "a", "value": 999}
    engine.adopt(RecordRef("demo", "a"), message="adopt drift")

    result, code = actions.import_from_file(engine, snap, message="restore from backup")
    assert result["state"] == "committed"
    # live is back to the snapshot value
    assert adapter.records[("demo", "a")]["value"] == 1


def test_import_from_accepts_bare_list_shape():
    engine, adapter = _engine(records={("demo", "a"): {"id": "a", "value": 1}})
    engine.commit(RecordRef("demo", "a"), {"id": "a", "value": 1}, message="seed")
    items = [{"record": "demo:a", "doc": {"id": "a", "value": 7}}]
    result, _ = actions.import_from_file(engine, items, message="restore list")
    assert result["state"] == "committed"
    assert adapter.records[("demo", "a")]["value"] == 7


def test_import_from_dry_run_writes_nothing():
    engine, adapter = _engine(records={("demo", "a"): {"id": "a", "value": 1}})
    engine.commit(RecordRef("demo", "a"), {"id": "a", "value": 1}, message="seed")
    snap = engine.export_records()
    adapter.records[("demo", "a")] = {"id": "a", "value": 5}
    engine.adopt(RecordRef("demo", "a"), message="adopt")
    before = len(adapter.history)

    result, _ = actions.import_from_file(engine, snap, message="preview", dry_run=True)
    assert result["state"] == "dry_run"
    assert len(adapter.history) == before


def test_import_from_rejects_garbage():
    engine, _ = _engine(records={("demo", "a"): {"id": "a"}})
    with pytest.raises(ValueError):
        actions.import_from_file(engine, {"not": "an export"}, message="x")


# --- bulk commit --dry-run ------------------------------------------------------------------


def test_bulk_commit_preview_reports_per_record_and_writes_nothing():
    engine, adapter = _engine(records={
        ("demo", "a"): {"id": "a", "value": 1},
        ("demo", "b"): {"id": "b", "value": 1},
    })
    engine.commit(RecordRef("demo", "a"), {"id": "a", "value": 1}, message="seed a")
    engine.commit(RecordRef("demo", "b"), {"id": "b", "value": 1}, message="seed b")
    before = len(adapter.history)

    items = [
        (RecordRef("demo", "a"), {"id": "a", "value": 2}),  # would_commit
        (RecordRef("demo", "b"), {"id": "b", "value": 1}),  # noop
    ]
    preview = actions.bulk_commit_preview(engine, items)
    assert preview["state"] == "dry_run"
    assert preview["summary"] == {"total": 2, "would_commit": 1, "drift": 0, "noop": 1}
    assert len(adapter.history) == before  # nothing written


def test_bulk_commit_dry_run_via_action_writes_nothing():
    engine, adapter = _engine(records={("demo", "a"): {"id": "a", "value": 1}})
    engine.commit(RecordRef("demo", "a"), {"id": "a", "value": 1}, message="seed")
    before = len(adapter.history)
    result, code = actions.bulk_commit(
        engine, [{"record": "demo:a", "doc": {"id": "a", "value": 9}}], message="x", dry_run=True
    )
    assert result["state"] == "dry_run"
    assert len(adapter.history) == before


def test_bulk_commit_preview_flags_drift_without_writing():
    engine, adapter = _engine(records={("demo", "a"): {"id": "a", "value": 1}})
    engine.commit(RecordRef("demo", "a"), {"id": "a", "value": 1}, message="seed")
    adapter.records[("demo", "a")] = {"id": "a", "value": 42}  # out-of-band drift
    before = len(adapter.history)
    preview = actions.bulk_commit_preview(engine, [(RecordRef("demo", "a"), {"id": "a", "value": 2})])
    assert preview["summary"]["drift"] == 1
    assert len(adapter.history) == before


# --- doctor adopt-all nudge -----------------------------------------------------------------


def test_drift_nudge_fires_only_above_threshold():
    # below ratio / below count → no nudge
    assert actions._drift_nudges(tracked=100, drift=4) == []
    assert actions._drift_nudges(tracked=100, drift=10) == []  # 10% < 25%
    # high drift → nudge mentioning adopt --all
    nudge = actions._drift_nudges(tracked=288, drift=201)
    assert nudge and "adopt --all" in nudge[0]
    assert "201/288" in nudge[0]
