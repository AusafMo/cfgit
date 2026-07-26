# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""CFG_ENV/CFG_CONFIG env-var defaults + config walk-up discovery + flag-position hint."""
from __future__ import annotations

import pytest

from cfg.cli.main import _looks_like_global_flag, _parser
from cfg.core.config import _resolve_config_path


# --- config walk-up discovery ---------------------------------------------------------------


def test_local_config_still_wins(tmp_path, monkeypatch):
    (tmp_path / ".cfg.toml").write_text("x = 1")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CFG_CONFIG", raising=False)
    assert _resolve_config_path(None) == (tmp_path / ".cfg.toml").resolve()


def test_walk_up_finds_parent_config(tmp_path, monkeypatch):
    (tmp_path / ".cfg.toml").write_text("x = 1")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    monkeypatch.delenv("CFG_CONFIG", raising=False)
    # no local .cfg.toml in sub → walk up to tmp_path
    assert _resolve_config_path(None) == (tmp_path / ".cfg.toml").resolve()


def test_cfg_config_env_honored(tmp_path, monkeypatch):
    cfg = tmp_path / "custom.cfg.toml"
    cfg.write_text("x = 1")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CFG_CONFIG", str(cfg))
    # even with a different cwd file, the explicit env var wins
    assert _resolve_config_path(None) == cfg.resolve()


def test_missing_config_raises_with_helpful_message(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CFG_CONFIG", raising=False)
    with pytest.raises(FileNotFoundError, match="parent directories"):
        _resolve_config_path(None)


# --- env-var defaults -----------------------------------------------------------------------


def test_cfg_env_default(monkeypatch):
    monkeypatch.setenv("CFG_ENV", "prod")
    args = _parser().parse_args(["status"])
    assert args.env == "prod"


def test_cfg_env_default_falls_back_to_dev(monkeypatch):
    monkeypatch.delenv("CFG_ENV", raising=False)
    args = _parser().parse_args(["status"])
    assert args.env == "dev"


def test_explicit_env_flag_beats_env_var(monkeypatch):
    monkeypatch.setenv("CFG_ENV", "prod")
    args = _parser().parse_args(["--env", "staging", "status"])
    assert args.env == "staging"


# --- flag-position hint ---------------------------------------------------------------------


def test_looks_like_global_flag():
    assert _looks_like_global_flag("--env")
    assert _looks_like_global_flag("--config-file=/x")
    assert not _looks_like_global_flag("--allow-secret")
