# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""Unit tests for the self-teaching remedy table and envelope enrichment."""
from __future__ import annotations

from cfg.core import remedy
from cfg.interfaces import actions


def test_drift_state_has_actionable_remedy_with_record_substituted():
    nxt = remedy.next_for(state="changed_outside_cfgit", record="modelgarden_models:x")
    assert nxt is not None
    # the remedy names the real record, not a placeholder
    assert any("modelgarden_models:x" in cmd for cmd in nxt.commands)
    assert any(cmd.startswith("cfg adopt") for cmd in nxt.commands)


def test_missing_record_placeholder_when_no_record():
    nxt = remedy.next_for(state="changed_outside_cfgit", record=None)
    assert nxt is not None
    assert any("<collection:id>" in cmd for cmd in nxt.commands)


def test_clean_success_states_have_no_remedy():
    for state in ("committed", "adopted", "imported", "restored", "tagged", "clean", "merged", "switched"):
        assert remedy.next_for(state=state, record="c:1") is None


def test_unknown_state_returns_none():
    assert remedy.next_for(state="totally_made_up", record="c:1") is None


def test_error_class_remedies_resolve():
    for cls in ("StaleHead", "StaleLive", "NoSuchConfig", "SecretBlocked", "AtomicityUnavailable", "AmbiguousConfig"):
        assert remedy.next_for(error_class=cls, record="c:1", message="boom") is not None


def test_identity_error_default_vs_author_mismatch():
    default = remedy.next_for(error_class="IdentityError", message="prod requires authenticated identity", record=None)
    mismatch = remedy.next_for(
        error_class="IdentityError",
        message="--author bob does not match verified identity alice",
        record=None,
    )
    assert default is not None and mismatch is not None
    assert default.why != mismatch.why  # message sub-selection actually differentiates
    assert "CFGIT_IDENTITY_TOKEN" in default.remedy


def test_permission_denied_variants_differentiate():
    admin = remedy.next_for(error_class="PermissionDenied", message="x cannot run init on prod: admin permission required")
    writer = remedy.next_for(error_class="PermissionDenied", message="x cannot run commit on prod: writer permission required")
    ident = remedy.next_for(error_class="PermissionDenied", message="x cannot run commit on prod: authenticated identity required")
    assert admin and writer and ident
    assert admin.why != writer.why != ident.why


def test_render_text_includes_why_remedy_and_commands():
    nxt = remedy.next_for(state="changed_outside_cfgit", record="agent_configs:planner")
    text = remedy.render_text(nxt)
    assert nxt.why in text
    assert "→" in text
    assert "cfg adopt agent_configs:planner" in text


def test_open_mode_predicate():
    assert remedy.open_mode_on_guarded_env(needs_approval=True, identity_mode="open") is True
    assert remedy.open_mode_on_guarded_env(needs_approval=True, identity_mode="authenticated") is False
    assert remedy.open_mode_on_guarded_env(needs_approval=False, identity_mode="open") is False


# --- envelope enrichment (additive: existing keys untouched, new keys added) ----------------


def test_envelope_success_carries_state_and_null_next_for_clean():
    env = actions.envelope(lambda: ({"state": "committed", "oid": "abc", "seq": 4}, actions.EXIT_OK))
    assert env["status"] == "ok"
    assert env["code"] == actions.EXIT_OK
    assert env["data"]["state"] == "committed"
    assert env["state"] == "committed"
    assert env["next"] is None  # committed needs no remedy


def test_envelope_drift_carries_next_with_record():
    def _drift():
        return {"state": "changed_outside_cfgit", "live_oid": "a", "head_oid": "b"}, actions.EXIT_DIRTY

    env = actions.envelope(_drift, record="modelgarden_models:seedance")
    assert env["status"] == "dirty"
    assert env["state"] == "changed_outside_cfgit"
    assert env["next"] is not None
    assert any("modelgarden_models:seedance" in c for c in env["next"]["commands"])


def test_envelope_error_carries_next():
    def _boom():
        from cfg.core.engine import SecretBlocked

        raise SecretBlocked("secret-like content refused")

    env = actions.envelope(_boom, record="c:1")
    assert env["status"] == "error"
    assert env["next"] is not None
    assert "doctor" in " ".join(env["next"]["commands"]) or "doctor" in env["next"]["remedy"]


def test_envelope_keeps_existing_keys_intact():
    env = actions.envelope(lambda: ({"hello": "world"}, actions.EXIT_OK))
    # the four historical keys are unchanged; new keys are strictly additive
    assert env["status"] == "ok"
    assert env["code"] == actions.EXIT_OK
    assert env["message"] == ""
    assert env["data"] == {"hello": "world"}
    assert set(env) == {"status", "code", "message", "data", "state", "next"}
