# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""commit --dry-run: preview the delta, write nothing, same guards as a real commit."""
from __future__ import annotations

import pytest

from cfg.core.engine import RecordRef, SecretBlocked
from cfg.core.config import SecretsConfig

from test_engine_safety import _engine  # reuse shared fixture


def test_dry_run_returns_would_commit_with_delta_and_writes_nothing():
    engine, adapter = _engine(records={("demo", "alpha"): {"id": "alpha", "value": 1}})
    # first make a real commit so a HEAD exists
    engine.commit(RecordRef("demo", "alpha"), {"id": "alpha", "value": 1}, message="seed")
    history_len = len(adapter.history)

    result = engine.commit_preview(RecordRef("demo", "alpha"), {"id": "alpha", "value": 2})

    assert result["state"] == "would_commit"
    assert result["changes"]  # non-empty field-level delta
    assert "value" in result["text"]
    assert len(adapter.history) == history_len  # nothing written


def test_dry_run_reports_noop_when_identical():
    engine, adapter = _engine(records={("demo", "alpha"): {"id": "alpha", "value": 1}})
    engine.commit(RecordRef("demo", "alpha"), {"id": "alpha", "value": 1}, message="seed")
    before = len(adapter.history)

    result = engine.commit_preview(RecordRef("demo", "alpha"), {"id": "alpha", "value": 1})

    assert result["state"] == "noop"
    assert len(adapter.history) == before


def test_dry_run_reports_drift_without_writing():
    engine, adapter = _engine(records={("demo", "alpha"): {"id": "alpha", "value": 1}})
    engine.commit(RecordRef("demo", "alpha"), {"id": "alpha", "value": 1}, message="seed")
    # simulate an out-of-band write: mutate the live record behind cfgit's back
    adapter.records[("demo", "alpha")] = {"id": "alpha", "value": 99}
    before = len(adapter.history)

    result = engine.commit_preview(RecordRef("demo", "alpha"), {"id": "alpha", "value": 2})

    assert result["state"] == "changed_outside_cfgit"
    assert len(adapter.history) == before


def test_dry_run_still_enforces_secret_policy():
    engine, adapter = _engine(
        records={("demo", "alpha"): {"id": "alpha", "value": 1}},
        secrets=SecretsConfig(block_fields=("*api_key*",), block_values=("sk-[A-Za-z0-9]{6,}",)),
    )
    engine.commit(RecordRef("demo", "alpha"), {"id": "alpha", "value": 1}, message="seed")

    with pytest.raises(SecretBlocked):
        engine.commit_preview(RecordRef("demo", "alpha"), {"id": "alpha", "api_key": "sk-ABCDEF1234"})
