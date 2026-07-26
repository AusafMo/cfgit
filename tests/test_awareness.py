# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""doctor --status awareness header + the open-mode-on-guarded-env warning."""
from __future__ import annotations

from cfg.core.config import IdentityConfig
from cfg.interfaces import actions

from test_engine_safety import _engine


def _guarded_engine(mode: str, needs_approval: bool):
    engine, adapter = _engine(
        records={("demo", "alpha"): {"id": "alpha", "value": 1}},
        identity_config=IdentityConfig(mode=mode),
    )
    # flip needs_approval on the constructed env (fixture builds env 'dev')
    env = engine.config.envs["dev"]
    object.__setattr__(env, "needs_approval", needs_approval)
    return engine, adapter


def test_status_report_shape_and_counts():
    engine, adapter = _guarded_engine("open", False)
    report, code = actions.status_report(engine)
    assert code == actions.EXIT_OK
    for key in ("config_file", "env", "database", "identity_mode", "authenticated", "reachable", "tracked", "drift"):
        assert key in report
    assert report["reachable"] is True


def test_open_mode_warning_fires_only_on_guarded_open_env():
    guarded, _ = _guarded_engine("open", True)
    report, _ = actions.status_report(guarded)
    assert report["warnings"], "needs_approval + open should warn"
    assert "unaudited" in report["warnings"][0].lower()

    safe, _ = _guarded_engine("authenticated", True)
    assert actions.status_report(safe)[0]["warnings"] == []

    dev, _ = _guarded_engine("open", False)
    assert actions.status_report(dev)[0]["warnings"] == []


def test_init_carries_open_mode_warning():
    guarded, _ = _guarded_engine("open", True)
    out, _ = actions.init(guarded)
    assert out.get("warnings")


def test_whoami_exposes_open_mode_warning_for_ui():
    guarded, _ = _guarded_engine("open", True)
    who, _ = actions.whoami(guarded)
    assert who["needs_approval"] is True
    assert who["open_mode_warning"]  # non-null so the UI can surface it

    safe, _ = _guarded_engine("authenticated", True)
    assert actions.whoami(safe)[0]["open_mode_warning"] is None
