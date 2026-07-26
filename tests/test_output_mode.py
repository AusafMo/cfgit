# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""CFG_OUTPUT=auto TTY-aware output-mode resolution."""
from __future__ import annotations

from cfg.cli.main import _resolve_json_mode


def test_explicit_json_always_wins(monkeypatch):
    monkeypatch.setenv("CFG_OUTPUT", "human")
    assert _resolve_json_mode(explicit_json=True, isatty=True) is True


def test_cfg_output_json_forces_json(monkeypatch):
    monkeypatch.setenv("CFG_OUTPUT", "json")
    assert _resolve_json_mode(explicit_json=False, isatty=True) is True


def test_cfg_output_human_forces_human_even_when_piped(monkeypatch):
    monkeypatch.setenv("CFG_OUTPUT", "human")
    assert _resolve_json_mode(explicit_json=False, isatty=False) is False


def test_auto_is_human_on_tty(monkeypatch):
    monkeypatch.delenv("CFG_OUTPUT", raising=False)
    assert _resolve_json_mode(explicit_json=False, isatty=True) is False


def test_auto_is_json_when_piped(monkeypatch):
    monkeypatch.delenv("CFG_OUTPUT", raising=False)
    # piped/non-TTY → JSON, so existing scripts that pipe cfg output keep machine output
    assert _resolve_json_mode(explicit_json=False, isatty=False) is True
