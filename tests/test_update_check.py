# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""Update-check engine: throttle, snooze, kill switch, fail-silent, version compare."""
from __future__ import annotations

import json

import pytest

from cfg import update


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Point the state file at a temp dir, clear the kill switch, and stub the GitHub notes
    fetch so tests never hit the network by default."""
    monkeypatch.setattr(update, "STATE_DIR", tmp_path)
    monkeypatch.setattr(update, "STATE_FILE", tmp_path / "update-check.json")
    monkeypatch.delenv(update._DISABLE_ENV, raising=False)
    monkeypatch.setattr(update, "_fetch_release_notes", lambda: "  • new stuff")


def _fixed_latest(value):
    return lambda: value


# --- version compare ------------------------------------------------------------------------


def test_gt_numeric_and_equal():
    assert update._gt("0.4.0", "0.3.0") is True
    assert update._gt("0.10.0", "0.9.0") is True     # not string-compared
    assert update._gt("1.0.0", "0.9.9") is True
    assert update._gt("0.3.0", "0.3.0") is False
    assert update._gt("0.3.0", "0.4.0") is False
    assert update._gt(None, "0.3.0") is False


# --- core check behavior --------------------------------------------------------------------


def test_update_available_produces_message(monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda: "0.3.0")
    monkeypatch.setattr(update, "_fetch_latest", _fixed_latest("0.4.0"))
    st = update.check(now=1000.0)
    assert st.update_available is True
    assert st.latest == "0.4.0"
    assert st.message and "0.4.0" in st.message and "pip install -U cfgit" in st.message


def test_no_update_no_message(monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda: "0.4.0")
    monkeypatch.setattr(update, "_fetch_latest", _fixed_latest("0.4.0"))
    st = update.check(now=1000.0)
    assert st.update_available is False
    assert st.message is None


def test_kill_switch_disables_without_network(monkeypatch):
    monkeypatch.setenv(update._DISABLE_ENV, "1")
    called = {"n": 0}
    monkeypatch.setattr(update, "_fetch_latest", lambda: called.__setitem__("n", called["n"] + 1))
    st = update.check(now=1000.0)
    assert st.disabled is True
    assert st.message is None
    assert called["n"] == 0  # never hit the network


def test_throttle_skips_refetch_within_a_day(monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda: "0.3.0")
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return "0.4.0"

    monkeypatch.setattr(update, "_fetch_latest", counting)
    update.check(now=1000.0)                     # first: fetches
    update.check(now=1000.0 + 3600)              # +1h: throttled, no fetch
    assert calls["n"] == 1


def test_force_bypasses_throttle(monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda: "0.3.0")
    calls = {"n": 0}
    monkeypatch.setattr(update, "_fetch_latest", lambda: (calls.__setitem__("n", calls["n"] + 1), "0.4.0")[1])
    update.check(now=1000.0)
    update.check(now=1000.0 + 3600, force=True)
    assert calls["n"] == 2


def test_snooze_silences_until_it_lapses(monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda: "0.3.0")
    monkeypatch.setattr(update, "_fetch_latest", _fixed_latest("0.4.0"))
    update.check(now=1000.0)                      # populates latest
    update.snooze(30, now=1000.0)
    st = update.check(now=1000.0 + 5 * 24 * 3600) # 5 days later, still snoozed
    assert st.snoozed is True
    assert st.message is None
    later = update.check(now=1000.0 + 40 * 24 * 3600)  # 40 days later, lapsed
    assert later.snoozed is False


def test_network_failure_is_silent(monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda: "0.3.0")
    monkeypatch.setattr(update, "_fetch_latest", _fixed_latest(None))
    st = update.check(now=1000.0)
    assert st.checked is False
    assert st.message is None
    assert st.update_available is False


def test_state_io_tolerates_garbage_file(monkeypatch):
    update.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    update.STATE_FILE.write_text("not json{{{", encoding="utf-8")
    monkeypatch.setattr(update, "installed_version", lambda: "0.3.0")
    monkeypatch.setattr(update, "_fetch_latest", _fixed_latest("0.4.0"))
    st = update.check(now=1000.0)               # must not raise
    assert st.update_available is True


def test_snooze_persists_to_state_file(monkeypatch):
    update.snooze(30, now=1000.0)
    saved = json.loads(update.STATE_FILE.read_text())
    assert saved["snooze_until"] == 1000.0 + 30 * 24 * 3600


# --- what's-new release notes ---------------------------------------------------------------


def test_message_includes_whats_new_notes(monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda: "0.3.0")
    monkeypatch.setattr(update, "_fetch_latest", _fixed_latest("0.4.0"))
    monkeypatch.setattr(update, "_fetch_release_notes", lambda: "  • cfg export\n  • bulk --dry-run")
    st = update.check(now=1000.0)
    assert st.notes and "cfg export" in st.notes
    assert "What's new:" in st.message and "cfg export" in st.message
    assert st.notes_url == update.RELEASES_PAGE


def test_notes_fetch_failure_degrades_to_plain_nudge(monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda: "0.3.0")
    monkeypatch.setattr(update, "_fetch_latest", _fixed_latest("0.4.0"))
    monkeypatch.setattr(update, "_fetch_release_notes", lambda: None)
    st = update.check(now=1000.0)
    assert st.update_available is True
    assert st.notes is None
    assert "What's new" not in st.message  # plain one-liner still works
    assert "pip install -U cfgit" in st.message


def test_excerpt_caps_lines_and_strips_headers():
    body = "## Highlights\n\n- one\n- two\n- three\n- four\n- five\n- six\n- seven"
    out = update._excerpt(body)
    assert out.count("\n") + 1 == update._NOTES_MAX_LINES  # capped
    assert "Highlights" not in out
