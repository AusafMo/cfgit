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
