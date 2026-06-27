from __future__ import annotations

import pytest


pytest.importorskip("mcp")


def test_mcp_identity_hash_returns_envelope() -> None:
    from cfg.mcp.server import cfg_identity_hash

    result = cfg_identity_hash("imkanyewest")

    assert result["status"] == "ok"
    assert result["code"] == 0
    assert result["data"]["sha256"].startswith("sha256:")
    assert result["data"]["fingerprint"] == result["data"]["sha256"][7:12]


def test_mcp_identity_hash_rejects_display_length_strings() -> None:
    from cfg.mcp.server import cfg_identity_hash

    result = cfg_identity_hash("abc12")

    assert result["status"] == "error"
    assert result["code"] == 1
    assert "at least" in result["message"]


def test_mcp_bulk_commit_forwards_batch_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    from cfg.mcp import server

    captured = {}

    def fake_call(name, payload, **kwargs):
        captured.update({"name": name, "payload": payload, "kwargs": kwargs})
        return {"status": "ok", "code": 0, "message": "", "data": {}}

    monkeypatch.setattr(server, "_call", fake_call)

    result = server.cfg_bulk_commit(
        [{"record": "demo:alpha", "doc": {"id": "alpha", "value": 2}}],
        message="bulk tune",
        config_file=".cfg.toml",
        env="dev",
        author="dev@example.com",
    )

    assert result["status"] == "ok"
    assert captured["name"] == "bulk_commit"
    assert captured["payload"]["message"] == "bulk tune"
    assert captured["payload"]["items"][0]["record"] == "demo:alpha"
    assert captured["kwargs"]["author"] == "dev@example.com"


def test_mcp_branch_and_pr_tools_forward_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    from cfg.mcp import server

    captured = []

    def fake_call(name, payload, **kwargs):
        captured.append({"name": name, "payload": payload, "kwargs": kwargs})
        return {"status": "ok", "code": 0, "message": "", "data": {}}

    monkeypatch.setattr(server, "_call", fake_call)

    server.cfg_branch_create("router-test", from_branch="main", message="draft", config_file=".cfg.toml")
    server.cfg_commit("demo:alpha", '{"id":"alpha","value":2}', "draft change", branch="router-test")
    server.cfg_pr_create(head="router-test", message="review draft")
    server.cfg_pr_merge(id="pr_abc", message="merge draft")

    assert [item["name"] for item in captured] == ["branch_create", "commit", "pr_create", "pr_merge"]
    assert captured[0]["payload"] == {"name": "router-test", "from_branch": "main", "message": "draft"}
    assert captured[1]["payload"]["branch"] == "router-test"
    assert captured[2]["payload"]["head"] == "router-test"
    assert captured[3]["payload"]["id"] == "pr_abc"


def test_mcp_recent_history_forwards_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from cfg.mcp import server

    captured = {}

    def fake_call(name, payload, **kwargs):
        captured.update({"name": name, "payload": payload, "kwargs": kwargs})
        return {"status": "ok", "code": 0, "message": "", "data": []}

    monkeypatch.setattr(server, "_call", fake_call)

    result = server.cfg_recent_history(limit=12, config_file=".cfg.toml", env="dev")

    assert result["status"] == "ok"
    assert captured["name"] == "recent_history"
    assert captured["payload"] == {"limit": 12}
    assert captured["kwargs"]["config_file"] == ".cfg.toml"
