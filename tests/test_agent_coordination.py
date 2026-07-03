from __future__ import annotations

import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from urllib.parse import urlparse

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins" / "cfg_agent"))


def test_resource_overlap_rules_are_field_aware() -> None:
    from cfg_agent.resources import paths_overlap, resources_overlap

    assert paths_overlap("/instructions", "/instructions/body")
    assert not paths_overlap("/instructions/body", "/instructions/title")
    assert resources_overlap("agent_configs:refund:/instructions", "agent_configs:refund")
    assert resources_overlap("agent_configs:*", "agent_configs:refund:/instructions")
    assert not resources_overlap("agent_configs:refund:/instructions", "agent_configs:refund:/tools")
    assert not resources_overlap("agent_configs:refund:/instructions", "modelgarden_models:refund:/instructions")


def test_session_lifecycle_emits_events() -> None:
    from cfg_agent import AgentCoordinator, InMemoryAgentStateAdapter

    adapter = InMemoryAgentStateAdapter()
    coordinator = AgentCoordinator(adapter)

    session = coordinator.start_session(
        task="edit refund config",
        agent_id="agent.refund",
        agent_kind="codex",
        actor="agent.refund@runtime",
    )
    coordinator.heartbeat(session["session_id"])
    closed = coordinator.end_session(session["session_id"], status="completed", summary="done")

    assert closed["status"] == "completed"
    assert [event["event"] for event in coordinator.watch()] == [
        "session.started",
        "session.heartbeat",
        "session.ended",
    ]


def test_non_overlapping_field_claims_can_run_in_parallel() -> None:
    from cfg_agent import AgentCoordinator, InMemoryAgentStateAdapter

    coordinator = AgentCoordinator(InMemoryAgentStateAdapter())
    left = coordinator.start_session(task="instructions", agent_id="agent.a")
    right = coordinator.start_session(task="threshold", agent_id="agent.b")

    left_lease = coordinator.claim(
        session_id=left["session_id"],
        resource="agent_configs:refund_resolution:/instructions",
        reason="copy edit",
    )
    right_lease = coordinator.claim(
        session_id=right["session_id"],
        resource="agent_configs:refund_resolution:/automation_threshold",
        reason="threshold edit",
    )

    assert left_lease["status"] == "active"
    assert right_lease["status"] == "active"
    assert coordinator.conflicts() == []


def test_release_requires_lease_owner() -> None:
    from cfg_agent import AgentCoordinator, AgentStateError, InMemoryAgentStateAdapter

    coordinator = AgentCoordinator(InMemoryAgentStateAdapter())
    owner = coordinator.start_session(task="instructions", agent_id="agent.owner")
    other = coordinator.start_session(task="other", agent_id="agent.other")

    lease = coordinator.claim(
        session_id=owner["session_id"],
        resource="agent_configs:refund_resolution:/instructions",
        reason="copy edit",
    )

    with pytest.raises(AgentStateError) as raised:
        coordinator.release(session_id=other["session_id"], lease_id=lease["lease_id"])

    assert raised.value.code == "lease_not_owned"
    assert coordinator.adapter.get_lease(lease["lease_id"])["status"] == "active"


def test_renew_expired_lease_is_blocked() -> None:
    from cfg_agent import AgentCoordinator, AgentStateError, InMemoryAgentStateAdapter

    adapter = InMemoryAgentStateAdapter()
    coordinator = AgentCoordinator(adapter)
    session = coordinator.start_session(task="short lease", agent_id="agent.a")

    lease = coordinator.claim(
        session_id=session["session_id"],
        resource="agent_configs:refund_resolution:/instructions",
        ttl_seconds=1,
    )

    with pytest.raises(AgentStateError) as raised:
        adapter.renew_lease(
            lease["lease_id"],
            ttl_seconds=10,
            now=datetime.now(timezone.utc) + timedelta(seconds=2),
        )

    assert raised.value.code == "lease_expired"
    assert adapter.get_lease(lease["lease_id"])["status"] == "expired"


def test_mongo_renew_lease_uses_conditional_update_without_upsert() -> None:
    pytest.importorskip("pymongo")
    from cfg_agent.adapters.mongo import MongoAgentStateAdapter

    now = datetime(2026, 7, 4, tzinfo=timezone.utc)
    fake_state = _FakeMongoAgentState(
        {
            "env": "dev",
            "kind": "lease",
            "id": "lea_1",
            "lease_id": "lea_1",
            "session_id": "ses_1",
            "resource": "demo:alpha:/value",
            "status": "active",
            "expires_at": "2026-07-04T00:10:00Z",
        }
    )
    adapter = object.__new__(MongoAgentStateAdapter)
    adapter.env_name = "dev"
    adapter.state = fake_state

    renewed = adapter.renew_lease("lea_1", ttl_seconds=60, now=now)

    assert renewed["status"] == "active"
    assert fake_state.find_one_and_update_calls[0]["query"] == {
        "env": "dev",
        "kind": "lease",
        "id": "lea_1",
        "status": "active",
        "expires_at": {"$gt": "2026-07-04T00:00:00Z"},
    }
    assert "upsert" not in fake_state.find_one_and_update_calls[0]["kwargs"]
    assert fake_state.replace_one_calls == []


def test_mongo_renew_lease_marks_expired_without_replacing() -> None:
    pytest.importorskip("pymongo")
    from cfg_agent import AgentStateError
    from cfg_agent.adapters.mongo import MongoAgentStateAdapter

    now = datetime(2026, 7, 4, tzinfo=timezone.utc)
    fake_state = _FakeMongoAgentState(
        {
            "env": "dev",
            "kind": "lease",
            "id": "lea_1",
            "lease_id": "lea_1",
            "session_id": "ses_1",
            "resource": "demo:alpha:/value",
            "status": "active",
            "expires_at": "2026-07-03T23:59:59Z",
        }
    )
    adapter = object.__new__(MongoAgentStateAdapter)
    adapter.env_name = "dev"
    adapter.state = fake_state

    with pytest.raises(AgentStateError) as raised:
        adapter.renew_lease("lea_1", ttl_seconds=60, now=now)

    assert raised.value.code == "lease_expired"
    assert fake_state.doc["status"] == "expired"
    assert fake_state.replace_one_calls == []


def test_mongo_unknown_commit_is_not_retried_as_whole_transaction() -> None:
    pytest.importorskip("pymongo")
    from cfg_agent.adapters.mongo import _retryable_transaction_error

    assert not _retryable_transaction_error(_MongoLabelError("UnknownTransactionCommitResult"))
    assert _retryable_transaction_error(_MongoLabelError("TransientTransactionError"))
    assert _retryable_transaction_error(_MongoLabelError("WriteConflict"))


def test_overlapping_claim_creates_structured_conflict() -> None:
    from cfg_agent import AgentCoordinator, AgentStateError, InMemoryAgentStateAdapter

    coordinator = AgentCoordinator(InMemoryAgentStateAdapter())
    left = coordinator.start_session(task="instructions", agent_id="agent.a")
    right = coordinator.start_session(task="record", agent_id="agent.b")

    coordinator.claim(
        session_id=left["session_id"],
        resource="agent_configs:refund_resolution:/instructions",
        reason="copy edit",
    )

    with pytest.raises(AgentStateError) as raised:
        coordinator.claim(
            session_id=right["session_id"],
            resource="agent_configs:refund_resolution",
            reason="whole record rewrite",
        )

    assert raised.value.code == "lease_conflict"
    conflicts = coordinator.conflicts(status="open")
    assert len(conflicts) == 1
    assert conflicts[0]["type"] == "lease_conflict"
    assert conflicts[0]["resource"] == "agent_configs:refund_resolution"


def test_intent_tracks_planned_paths_and_can_close() -> None:
    from cfg_agent import AgentCoordinator, InMemoryAgentStateAdapter

    coordinator = AgentCoordinator(InMemoryAgentStateAdapter())
    session = coordinator.start_session(task="refund edit", agent_id="agent.refund")

    intent = coordinator.open_intent(
        session_id=session["session_id"],
        resources=["agent_configs:refund_resolution"],
        summary="Edit refund language",
        planned_paths=["/instructions"],
        expected_base={"agent_configs:refund_resolution": {"head_seq": 3}},
        idempotency_key="task-1:intent",
    )
    closed = coordinator.close_intent(
        session_id=session["session_id"],
        intent_id=intent["intent_id"],
        status="committed",
    )

    assert intent["status"] == "open"
    assert intent["planned_paths"] == ["/instructions"]
    assert closed["status"] == "committed"


def test_idempotency_replays_same_payload_and_blocks_different_payload() -> None:
    from cfg_agent import AgentCoordinator, AgentStateError, InMemoryAgentStateAdapter

    coordinator = AgentCoordinator(InMemoryAgentStateAdapter())
    first = coordinator.remember_idempotency(
        key="session-1:patch-1",
        payload={"record": "demo:a", "patch": [{"op": "replace", "path": "/x", "value": 1}]},
        result={"commit": "a@2"},
    )
    replay = coordinator.remember_idempotency(
        key="session-1:patch-1",
        payload={"record": "demo:a", "patch": [{"op": "replace", "path": "/x", "value": 1}]},
        result={"commit": "a@2"},
    )

    assert first == {"replay": False, "result": {"commit": "a@2"}}
    assert replay == {"replay": True, "result": {"commit": "a@2"}}

    with pytest.raises(AgentStateError) as raised:
        coordinator.remember_idempotency(
            key="session-1:patch-1",
            payload={"record": "demo:a", "patch": [{"op": "replace", "path": "/x", "value": 2}]},
            result={"commit": "a@3"},
        )
    assert raised.value.code == "idempotency_conflict"


def test_agent_actions_return_cfgit_style_envelopes() -> None:
    from cfg_agent.actions import AgentActions

    actions = AgentActions()
    started = actions.start_session({"task": "edit config", "agent_id": "agent.a"})
    session_id = started["data"]["session_id"]
    claimed = actions.claim(
        {
            "session_id": session_id,
            "resource": "agent_configs:refund_resolution:/instructions",
            "reason": "copy edit",
        }
    )
    intent = actions.open_intent(
        {
            "session_id": session_id,
            "resources": ["agent_configs:refund_resolution"],
            "summary": "Edit refund language",
            "planned_paths": ["/instructions"],
        }
    )

    assert started["status"] == "ok"
    assert claimed["data"]["scope"] == "field"
    assert intent["data"]["status"] == "open"


def test_agent_mcp_tools_use_agent_envelopes() -> None:
    pytest.importorskip("mcp")
    from cfg_agent import mcp as agent_mcp

    agent_mcp.reset_for_tests()
    started = agent_mcp.cfg_agent_start_session(task="edit config", agent_id="agent.a")
    session_id = started["data"]["session_id"]
    claimed = agent_mcp.cfg_agent_claim(
        session_id=session_id,
        resource="agent_configs:refund_resolution:/instructions",
        reason="copy edit",
    )
    status = agent_mcp.cfg_agent_status(session_id=session_id)

    assert started["status"] == "ok"
    assert claimed["data"]["scope"] == "field"
    assert status["data"]["session"]["session_id"] == session_id


def test_validate_patch_returns_patched_doc_and_diff() -> None:
    from cfg_agent import AgentCoordinator, InMemoryAgentStateAdapter

    engine, row = _clean_engine({"id": "alpha", "value": 1, "nested": {"mode": "old"}})
    coordinator = AgentCoordinator(InMemoryAgentStateAdapter())
    session = coordinator.start_session(task="edit value", agent_id="agent.a")
    coordinator.claim(session_id=session["session_id"], resource="demo:alpha:/value")
    intent = coordinator.open_intent(
        session_id=session["session_id"],
        resources=["demo:alpha"],
        summary="change value",
        planned_paths=["/value"],
        expected_base={"demo:alpha": {"head_seq": row["seq"], "head_oid": row["oid"]}},
    )

    result = coordinator.validate_patch(
        engine=engine,
        session_id=session["session_id"],
        record="demo:alpha",
        patch=[{"op": "replace", "path": "/value", "value": 2}],
        intent_id=intent["intent_id"],
    )

    assert result["state"] == "ok"
    assert result["patched_doc"]["value"] == 2
    assert result["changes"] == [{"path": "value", "op": "change", "before": 1, "after": 2}]
    assert coordinator.watch()[-1]["event"] == "patch.validated"


def test_validate_patch_supports_add_and_remove_ops() -> None:
    from cfg_agent import AgentCoordinator, InMemoryAgentStateAdapter

    engine, row = _clean_engine({"id": "alpha", "tools": ["search"], "old": True})
    coordinator = AgentCoordinator(InMemoryAgentStateAdapter())
    session = coordinator.start_session(task="edit tools", agent_id="agent.a")
    coordinator.claim(session_id=session["session_id"], resource="demo:alpha")
    intent = coordinator.open_intent(
        session_id=session["session_id"],
        resources=["demo:alpha"],
        summary="change tools",
        planned_paths=["/tools", "/old"],
        expected_base={"demo:alpha": {"head_seq": row["seq"], "head_oid": row["oid"]}},
    )

    result = coordinator.validate_patch(
        engine=engine,
        session_id=session["session_id"],
        record="demo:alpha",
        patch=[
            {"op": "add", "path": "/tools/-", "value": "handoff"},
            {"op": "remove", "path": "/old"},
        ],
        intent_id=intent["intent_id"],
    )

    assert result["patched_doc"] == {"id": "alpha", "tools": ["search", "handoff"]}


def test_validate_patch_requires_claim_for_each_path() -> None:
    from cfg_agent import AgentCoordinator, AgentStateError, InMemoryAgentStateAdapter

    engine, row = _clean_engine({"id": "alpha", "value": 1, "other": 1})
    coordinator = AgentCoordinator(InMemoryAgentStateAdapter())
    session = coordinator.start_session(task="edit value", agent_id="agent.a")
    coordinator.claim(session_id=session["session_id"], resource="demo:alpha:/value")
    intent = coordinator.open_intent(
        session_id=session["session_id"],
        resources=["demo:alpha"],
        summary="change value and other",
        planned_paths=["/value", "/other"],
        expected_base={"demo:alpha": {"head_seq": row["seq"], "head_oid": row["oid"]}},
    )

    with pytest.raises(AgentStateError) as raised:
        coordinator.validate_patch(
            engine=engine,
            session_id=session["session_id"],
            record="demo:alpha",
            patch=[
                {"op": "replace", "path": "/value", "value": 2},
                {"op": "replace", "path": "/other", "value": 2},
            ],
            intent_id=intent["intent_id"],
        )

    assert raised.value.code == "claim_required"
    assert raised.value.details["conflict"]["type"] == "claim_required"


def test_validate_patch_blocks_paths_outside_intent() -> None:
    from cfg_agent import AgentCoordinator, AgentStateError, InMemoryAgentStateAdapter

    engine, row = _clean_engine({"id": "alpha", "value": 1, "other": 1})
    coordinator = AgentCoordinator(InMemoryAgentStateAdapter())
    session = coordinator.start_session(task="edit value", agent_id="agent.a")
    coordinator.claim(session_id=session["session_id"], resource="demo:alpha")
    intent = coordinator.open_intent(
        session_id=session["session_id"],
        resources=["demo:alpha"],
        summary="change value",
        planned_paths=["/value"],
        expected_base={"demo:alpha": {"head_seq": row["seq"], "head_oid": row["oid"]}},
    )

    with pytest.raises(AgentStateError) as raised:
        coordinator.validate_patch(
            engine=engine,
            session_id=session["session_id"],
            record="demo:alpha",
            patch=[{"op": "replace", "path": "/other", "value": 2}],
            intent_id=intent["intent_id"],
        )

    assert raised.value.code == "intent_scope"


def test_validate_patch_blocks_stale_base() -> None:
    from cfg_agent import AgentCoordinator, AgentStateError, InMemoryAgentStateAdapter

    engine, _row = _clean_engine({"id": "alpha", "value": 1})
    coordinator = AgentCoordinator(InMemoryAgentStateAdapter())
    session = coordinator.start_session(task="edit value", agent_id="agent.a")
    coordinator.claim(session_id=session["session_id"], resource="demo:alpha:/value")
    intent = coordinator.open_intent(
        session_id=session["session_id"],
        resources=["demo:alpha"],
        summary="change value",
        planned_paths=["/value"],
        expected_base={"demo:alpha": {"head_seq": 999}},
    )

    with pytest.raises(AgentStateError) as raised:
        coordinator.validate_patch(
            engine=engine,
            session_id=session["session_id"],
            record="demo:alpha",
            patch=[{"op": "replace", "path": "/value", "value": 2}],
            intent_id=intent["intent_id"],
        )

    assert raised.value.code == "base_moved"


def test_validate_patch_blocks_live_drift() -> None:
    from cfg_agent import AgentCoordinator, AgentStateError, InMemoryAgentStateAdapter

    engine, row = _clean_engine(
        {"id": "alpha", "value": 1},
        live_doc={"id": "alpha", "value": 99},
    )
    coordinator = AgentCoordinator(InMemoryAgentStateAdapter())
    session = coordinator.start_session(task="edit value", agent_id="agent.a")
    coordinator.claim(session_id=session["session_id"], resource="demo:alpha:/value")
    intent = coordinator.open_intent(
        session_id=session["session_id"],
        resources=["demo:alpha"],
        summary="change value",
        planned_paths=["/value"],
        expected_base={"demo:alpha": {"head_seq": row["seq"], "head_oid": row["oid"]}},
    )

    with pytest.raises(AgentStateError) as raised:
        coordinator.validate_patch(
            engine=engine,
            session_id=session["session_id"],
            record="demo:alpha",
            patch=[{"op": "replace", "path": "/value", "value": 2}],
            intent_id=intent["intent_id"],
        )

    assert raised.value.code == "live_drift"
    assert raised.value.details["conflict"]["details"]["live_oid"] != row["oid"]


def test_policy_blocks_configured_secret_patch_paths() -> None:
    from cfg_agent import AgentCoordinator, AgentStateError, InMemoryAgentStateAdapter

    engine, row = _clean_engine(
        {"id": "alpha", "provider_config": {"api_key": "secret", "timeout": 30}},
        secret_fields=("provider_config.api_key",),
    )
    coordinator = AgentCoordinator(InMemoryAgentStateAdapter())
    session = coordinator.start_session(task="edit provider", agent_id="agent.a")
    coordinator.claim(session_id=session["session_id"], resource="demo:alpha")
    intent = coordinator.open_intent(
        session_id=session["session_id"],
        resources=["demo:alpha"],
        summary="change provider",
        planned_paths=["/provider_config/api_key"],
        expected_base={"demo:alpha": {"head_seq": row["seq"], "head_oid": row["oid"]}},
    )

    with pytest.raises(AgentStateError) as raised:
        coordinator.validate_patch(
            engine=engine,
            session_id=session["session_id"],
            record="demo:alpha",
            patch=[{"op": "replace", "path": "/provider_config/api_key", "value": "new-secret"}],
            intent_id=intent["intent_id"],
        )

    assert raised.value.code == "policy_blocked"
    assert raised.value.details["secret_path"] == "/provider_config/api_key"


def test_review_policy_routes_apply_to_branch_pr_without_runtime_mutation() -> None:
    from cfg.core.config import BranchesConfig
    from cfg_agent import AgentCoordinator, InMemoryAgentStateAdapter, StaticAgentPolicy

    engine, row = _clean_engine(
        {"id": "alpha", "rollout": {"traffic": 10}},
        branches=BranchesConfig(enabled=True),
    )
    coordinator = AgentCoordinator(
        InMemoryAgentStateAdapter(),
        policy=StaticAgentPolicy(review_paths=("/rollout*",)),
    )
    session = coordinator.start_session(task="change rollout", agent_id="rollout-agent")
    coordinator.claim(session_id=session["session_id"], resource="demo:alpha:/rollout")
    intent = coordinator.open_intent(
        session_id=session["session_id"],
        resources=["demo:alpha"],
        summary="increase traffic",
        planned_paths=["/rollout/traffic"],
        expected_base={"demo:alpha": {"head_seq": row["seq"], "head_oid": row["oid"]}},
    )

    result = coordinator.apply_patch(
        engine=engine,
        session_id=session["session_id"],
        record="demo:alpha",
        patch=[{"op": "replace", "path": "/rollout/traffic", "value": 25}],
        intent_id=intent["intent_id"],
        message="review rollout traffic",
        idempotency_key="review-key",
    )

    assert result["state"] == "review_requested"
    assert result["runtime_mutated"] is False
    assert result["pr"]["status"] == "open"
    assert result["pr"]["agent"]["session_id"] == session["session_id"]
    assert result["pr"]["agent"]["intent_id"] == intent["intent_id"]
    assert engine.resolve_ref(row_ref("demo", "alpha"), "=live")["doc"]["rollout"]["traffic"] == 10
    assert coordinator.status()["intents"][0]["status"] == "review_requested"
    assert [event["event"] for event in coordinator.watch()][-3:] == [
        "patch.validated",
        "patch.routed_to_pr",
        "pr.created",
    ]


def test_review_policy_fails_closed_when_branches_are_disabled() -> None:
    from cfg_agent import AgentCoordinator, AgentStateError, InMemoryAgentStateAdapter, StaticAgentPolicy

    engine, row = _clean_engine({"id": "alpha", "rollout": {"traffic": 10}})
    coordinator = AgentCoordinator(
        InMemoryAgentStateAdapter(),
        policy=StaticAgentPolicy(review_paths=("/rollout*",)),
    )
    session = coordinator.start_session(task="change rollout", agent_id="rollout-agent")
    coordinator.claim(session_id=session["session_id"], resource="demo:alpha:/rollout")
    intent = coordinator.open_intent(
        session_id=session["session_id"],
        resources=["demo:alpha"],
        summary="increase traffic",
        planned_paths=["/rollout/traffic"],
        expected_base={"demo:alpha": {"head_seq": row["seq"], "head_oid": row["oid"]}},
    )

    with pytest.raises(AgentStateError) as raised:
        coordinator.apply_patch(
            engine=engine,
            session_id=session["session_id"],
            record="demo:alpha",
            patch=[{"op": "replace", "path": "/rollout/traffic", "value": 25}],
            intent_id=intent["intent_id"],
            message="review rollout traffic",
        )

    assert raised.value.code == "review_unavailable"
    assert engine.resolve_ref(row_ref("demo", "alpha"), "=live")["doc"]["rollout"]["traffic"] == 10


def test_role_policy_limits_claim_scope() -> None:
    from cfg_agent import AgentCoordinator, AgentStateError, InMemoryAgentStateAdapter, StaticAgentPolicy
    from cfg_agent.policy import AgentRolePolicy

    policy = StaticAgentPolicy(
        roles={
            "routing-agent": AgentRolePolicy(
                name="routing-agent",
                can_claim=("modelgarden_models:*",),
            )
        }
    )
    coordinator = AgentCoordinator(InMemoryAgentStateAdapter(), policy=policy)
    session = coordinator.start_session(task="edit config", agent_id="routing-agent")

    with pytest.raises(AgentStateError) as raised:
        coordinator.claim(session_id=session["session_id"], resource="agent_configs:planner:/instructions")

    assert raised.value.code == "policy_blocked"
    assert raised.value.details["allowed"] == ["modelgarden_models:*"]


def test_agent_config_loads_memory_backend_policy_and_roles(tmp_path: pathlib.Path) -> None:
    from cfg_agent.config import load_agent_config

    cfg_file = tmp_path / ".cfg.toml"
    cfg_file.write_text(
        """
[project]
name = "agent-test"

[[collection]]
name = "demo"
id_field = "id"

[env.dev]
database = "mongo"
uri = "mongodb://localhost:27017/?replicaSet=rs0"
db = "cfgit-agent-test"

[agent]
enabled = true
state_backend = "memory"
state_collection = "agent_state_test"
events_collection = "agent_events_test"
default_lease_ttl_seconds = 12

[agent.policies]
deny_paths = ["/provider_config*"]
require_human_review_for = ["/rollout*"]

[[agent.role]]
name = "routing-agent"
can_claim = ["modelgarden_models:*"]
review_paths = ["/pricing*"]
""",
        encoding="utf-8",
    )

    cfg = load_agent_config(cfg_file)

    assert cfg.enabled is True
    assert cfg.state_backend == "memory"
    assert cfg.default_lease_ttl_seconds == 12
    assert cfg.deny_paths == ("/provider_config*",)
    assert cfg.review_paths == ("/rollout*",)
    assert cfg.roles["routing-agent"].can_claim == ("modelgarden_models:*",)
    assert cfg.roles["routing-agent"].review_paths == ("/pricing*",)


def test_agent_mcp_configured_memory_state_is_shared_across_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    pytest.importorskip("mcp")
    from cfg_agent import mcp as agent_mcp

    cfg_file = _agent_memory_config(tmp_path)
    agent_mcp.reset_for_tests()
    started = agent_mcp.cfg_agent_start_session(
        task="edit value",
        agent_id="agent.a",
        config_file=str(cfg_file),
        env="dev",
    )
    session_id = started["data"]["session_id"]
    claimed = agent_mcp.cfg_agent_claim(
        session_id=session_id,
        resource="demo:alpha:/value",
        config_file=str(cfg_file),
        env="dev",
    )
    status = agent_mcp.cfg_agent_status(session_id=session_id, config_file=str(cfg_file), env="dev")

    assert claimed["status"] == "ok"
    assert status["data"]["session"]["session_id"] == session_id
    assert status["data"]["leases"][0]["resource"] == "demo:alpha:/value"


def test_validate_patch_action_and_mcp_forward_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mcp")
    from cfg_agent import mcp as agent_mcp

    engine, row = _clean_engine({"id": "alpha", "value": 1})
    monkeypatch.setattr(agent_mcp, "make_engine", lambda ctx: engine)
    agent_mcp.reset_for_tests()
    started = agent_mcp.cfg_agent_start_session(task="edit value", agent_id="agent.a")
    session_id = started["data"]["session_id"]
    agent_mcp.cfg_agent_claim(session_id=session_id, resource="demo:alpha:/value")
    intent = agent_mcp.cfg_agent_open_intent(
        session_id=session_id,
        resources=["demo:alpha"],
        summary="change value",
        planned_paths=["/value"],
        expected_base={"demo:alpha": {"head_seq": row["seq"], "head_oid": row["oid"]}},
    )

    result = agent_mcp.cfg_agent_validate_patch(
        session_id=session_id,
        record="demo:alpha",
        patch='[{"op":"replace","path":"/value","value":2}]',
        intent_id=intent["data"]["intent_id"],
        env="dev",
    )

    assert result["status"] == "ok"
    assert result["data"]["patched_doc"]["value"] == 2


def test_apply_patch_commits_through_cfgit_core_and_closes_intent() -> None:
    from cfg_agent import AgentCoordinator, InMemoryAgentStateAdapter

    engine, row = _clean_engine({"id": "alpha", "value": 1})
    coordinator = AgentCoordinator(InMemoryAgentStateAdapter())
    session = coordinator.start_session(task="edit value", agent_id="agent.a")
    coordinator.claim(session_id=session["session_id"], resource="demo:alpha:/value")
    intent = coordinator.open_intent(
        session_id=session["session_id"],
        resources=["demo:alpha"],
        summary="change value",
        planned_paths=["/value"],
        expected_base={"demo:alpha": {"head_seq": row["seq"], "head_oid": row["oid"]}},
    )

    result = coordinator.apply_patch(
        engine=engine,
        session_id=session["session_id"],
        record="demo:alpha",
        patch=[{"op": "replace", "path": "/value", "value": 2}],
        intent_id=intent["intent_id"],
        message="agent update value",
        idempotency_key="session-1:patch-1",
    )

    assert result["state"] == "applied"
    assert result["commit"]["state"] == "committed"
    assert engine.resolve_ref(row_ref("demo", "alpha"), "=live")["doc"]["value"] == 2
    assert coordinator.status()["intents"][0]["status"] == "committed"
    assert [event["event"] for event in coordinator.watch()][-3:] == [
        "patch.validated",
        "patch.applied",
        "commit.created",
    ]


def test_apply_patch_idempotency_replays_after_head_moves() -> None:
    from cfg_agent import AgentCoordinator, InMemoryAgentStateAdapter

    engine, row = _clean_engine({"id": "alpha", "value": 1})
    coordinator = AgentCoordinator(InMemoryAgentStateAdapter())
    session = coordinator.start_session(task="edit value", agent_id="agent.a")
    coordinator.claim(session_id=session["session_id"], resource="demo:alpha:/value")
    intent = coordinator.open_intent(
        session_id=session["session_id"],
        resources=["demo:alpha"],
        summary="change value",
        planned_paths=["/value"],
        expected_base={"demo:alpha": {"head_seq": row["seq"], "head_oid": row["oid"]}},
    )

    first = coordinator.apply_patch(
        engine=engine,
        session_id=session["session_id"],
        record="demo:alpha",
        patch=[{"op": "replace", "path": "/value", "value": 2}],
        intent_id=intent["intent_id"],
        message="agent update value",
        idempotency_key="same-key",
    )
    replay = coordinator.apply_patch(
        engine=engine,
        session_id=session["session_id"],
        record="demo:alpha",
        patch=[{"op": "replace", "path": "/value", "value": 2}],
        intent_id=intent["intent_id"],
        message="agent update value",
        idempotency_key="same-key",
    )

    assert first["state"] == "applied"
    assert replay["state"] == "replayed"
    assert replay["result"]["commit"]["seq"] == first["commit"]["seq"]


def test_apply_patch_idempotency_blocks_same_key_with_different_payload() -> None:
    from cfg_agent import AgentCoordinator, AgentStateError, InMemoryAgentStateAdapter

    engine, row = _clean_engine({"id": "alpha", "value": 1})
    coordinator = AgentCoordinator(InMemoryAgentStateAdapter())
    session = coordinator.start_session(task="edit value", agent_id="agent.a")
    coordinator.claim(session_id=session["session_id"], resource="demo:alpha:/value")
    intent = coordinator.open_intent(
        session_id=session["session_id"],
        resources=["demo:alpha"],
        summary="change value",
        planned_paths=["/value"],
        expected_base={"demo:alpha": {"head_seq": row["seq"], "head_oid": row["oid"]}},
    )
    coordinator.apply_patch(
        engine=engine,
        session_id=session["session_id"],
        record="demo:alpha",
        patch=[{"op": "replace", "path": "/value", "value": 2}],
        intent_id=intent["intent_id"],
        message="agent update value",
        idempotency_key="same-key",
    )

    with pytest.raises(AgentStateError) as raised:
        coordinator.apply_patch(
            engine=engine,
            session_id=session["session_id"],
            record="demo:alpha",
            patch=[{"op": "replace", "path": "/value", "value": 3}],
            intent_id=intent["intent_id"],
            message="agent update value",
            idempotency_key="same-key",
        )

    assert raised.value.code == "idempotency_conflict"


def test_apply_patch_mcp_tool_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mcp")
    from cfg_agent import mcp as agent_mcp

    engine, row = _clean_engine({"id": "alpha", "value": 1})
    monkeypatch.setattr(agent_mcp, "make_engine", lambda ctx: engine)
    agent_mcp.reset_for_tests()
    started = agent_mcp.cfg_agent_start_session(task="edit value", agent_id="agent.a")
    session_id = started["data"]["session_id"]
    agent_mcp.cfg_agent_claim(session_id=session_id, resource="demo:alpha:/value")
    intent = agent_mcp.cfg_agent_open_intent(
        session_id=session_id,
        resources=["demo:alpha"],
        summary="change value",
        planned_paths=["/value"],
        expected_base={"demo:alpha": {"head_seq": row["seq"], "head_oid": row["oid"]}},
    )

    result = agent_mcp.cfg_agent_apply_patch(
        session_id=session_id,
        record="demo:alpha",
        patch=[{"op": "replace", "path": "/value", "value": 2}],
        intent_id=intent["data"]["intent_id"],
        message="agent update value",
        idempotency_key="mcp-key",
        env="dev",
    )

    assert result["status"] == "ok"
    assert result["data"]["state"] == "applied"
    assert engine.resolve_ref(row_ref("demo", "alpha"), "=live")["doc"]["value"] == 2


def test_mongo_agent_state_contract_when_local_uri_is_set() -> None:
    uri = os.environ.get("CFGIT_TEST_MONGO_URI") or os.environ.get("CFGIT_MONGODB_URI") or ""
    if not _is_local_uri(uri):
        pytest.skip("set CFGIT_TEST_MONGO_URI to a local/Docker Mongo URI to run this contract")
    pymongo = pytest.importorskip("pymongo")
    from cfg.core.config import CollectionConfig, EnvConfig, HistoryConfig, ProjectConfig
    from cfg_agent.adapters.mongo import MongoAgentStateAdapter

    suffix = uuid4().hex[:8]
    state_collection = f"agent_state_{suffix}"
    events_collection = f"agent_events_{suffix}"
    project = ProjectConfig(
        name="agent-mongo-test",
        path=pathlib.Path(".cfg.toml"),
        history=HistoryConfig(),
        collections=(CollectionConfig(name="demo", id_field="id"),),
        envs={"dev": EnvConfig(name="dev", database="mongo", uri=uri, db="cfgit_agent_test")},
    )
    client = pymongo.MongoClient(uri)
    try:
        adapter = MongoAgentStateAdapter(
            project=project,
            env_name="dev",
            state_collection=state_collection,
            events_collection=events_collection,
        )
        _exercise_agent_state_adapter(adapter)
    finally:
        client["cfgit_agent_test"].drop_collection(state_collection)
        client["cfgit_agent_test"].drop_collection(events_collection)


def test_postgres_agent_state_contract_when_local_uri_is_set() -> None:
    uri = os.environ.get("CFGIT_TEST_POSTGRES_URI") or os.environ.get("CFGIT_POSTGRES_URI") or ""
    if not _is_local_uri(uri):
        pytest.skip("set CFGIT_TEST_POSTGRES_URI to a local/Docker Postgres URI to run this contract")
    psycopg = pytest.importorskip("psycopg")
    from cfg.core.config import CollectionConfig, EnvConfig, HistoryConfig, ProjectConfig
    from cfg_agent.adapters.postgres import PostgresAgentStateAdapter

    suffix = uuid4().hex[:8]
    state_table = f"agent_state_{suffix}"
    events_table = f"agent_events_{suffix}"
    project = ProjectConfig(
        name="agent-postgres-test",
        path=pathlib.Path(".cfg.toml"),
        history=HistoryConfig(),
        collections=(CollectionConfig(name="demo", id_field="id"),),
        envs={"dev": EnvConfig(name="dev", database="postgres", uri=uri, db="")},
    )
    conn = psycopg.connect(uri, autocommit=True)
    try:
        adapter = PostgresAgentStateAdapter(
            project=project,
            env_name="dev",
            state_collection=state_table,
            events_collection=events_table,
        )
        _exercise_agent_state_adapter(adapter)
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{state_table}"')
            cur.execute(f'DROP TABLE IF EXISTS "{events_table}"')
        conn.close()


def _clean_engine(
    doc: dict,
    *,
    live_doc: dict | None = None,
    secret_fields: tuple[str, ...] = (),
    branches=None,
):
    from cfg.core.config import CollectionConfig
    from tests.test_engine_safety import _engine, _history_row

    coll = CollectionConfig(name="demo", id_field="id", secret_fields=secret_fields)
    row = _history_row(coll, doc, seq=1, valid_from=datetime(2026, 7, 1, tzinfo=timezone.utc))
    engine, _adapter = _engine(
        collection=coll,
        records={("demo", str(doc["id"])): live_doc or doc},
        history=[row],
        heads={("demo", str(doc["id"])): row},
        branches=branches,
    )
    return engine, row


def row_ref(collection: str, record_id: str):
    from cfg.core.engine import RecordRef

    return RecordRef(collection, record_id)


def _agent_memory_config(tmp_path: pathlib.Path) -> pathlib.Path:
    cfg_file = tmp_path / ".cfg.toml"
    cfg_file.write_text(
        """
[project]
name = "agent-mcp-test"

[[collection]]
name = "demo"
id_field = "id"

[env.dev]
database = "mongo"
uri = "mongodb://localhost:27017/?replicaSet=rs0"
db = "cfgit-agent-test"

[agent]
enabled = true
state_backend = "memory"
""",
        encoding="utf-8",
    )
    return cfg_file


def _exercise_agent_state_adapter(adapter) -> None:
    from cfg_agent import AgentCoordinator, AgentStateError

    adapter.init_agent_state()
    coordinator = AgentCoordinator(adapter)
    left = coordinator.start_session(task="left", agent_id="agent.left")
    right = coordinator.start_session(task="right", agent_id="agent.right")
    coordinator.claim(session_id=left["session_id"], resource="demo:alpha:/value")
    coordinator.claim(session_id=right["session_id"], resource="demo:alpha:/other")
    with pytest.raises(AgentStateError) as raised:
        coordinator.claim(session_id=right["session_id"], resource="demo:alpha")
    coordinator.remember_idempotency(
        key="adapter-key",
        payload={"x": 1},
        result={"ok": True},
    )
    replay = coordinator.remember_idempotency(
        key="adapter-key",
        payload={"x": 1},
        result={"ok": True},
    )

    assert raised.value.code == "lease_conflict"
    assert replay["replay"] is True
    assert coordinator.status()["sessions"][0]["status"] == "running"
    assert coordinator.watch()[-1]["event"] == "conflict.detected"


class _FakeMongoAgentState:
    def __init__(self, doc: dict):
        self.doc = dict(doc)
        self.find_one_and_update_calls: list[dict] = []
        self.replace_one_calls: list[dict] = []

    def find_one_and_update(self, query: dict, update: dict, **kwargs):
        self.find_one_and_update_calls.append({"query": query, "update": update, "kwargs": kwargs})
        if self._matches(query):
            self.doc.update(update.get("$set", {}))
            return dict(self.doc)
        return None

    def find_one(self, query: dict, session=None):
        if self._matches(query):
            return dict(self.doc)
        return None

    def update_one(self, query: dict, update: dict, session=None):
        if self._matches(query):
            self.doc.update(update.get("$set", {}))
        return None

    def replace_one(self, *args, **kwargs):
        self.replace_one_calls.append({"args": args, "kwargs": kwargs})
        return None

    def _matches(self, query: dict) -> bool:
        for key, expected in query.items():
            actual = self.doc.get(key)
            if isinstance(expected, dict):
                gt = expected.get("$gt")
                if gt is not None and not (actual > gt):
                    return False
                lte = expected.get("$lte")
                if lte is not None and not (actual <= lte):
                    return False
                continue
            if actual != expected:
                return False
        return True


class _MongoLabelError(Exception):
    def __init__(self, label: str):
        super().__init__(label)
        self.label = label

    def has_error_label(self, label: str) -> bool:
        return self.label == label


def _is_local_uri(uri: str) -> bool:
    if not uri:
        return False
    host = urlparse(uri).hostname
    return host in {"localhost", "127.0.0.1", "::1"}
