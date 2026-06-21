from __future__ import annotations

import pathlib
import re
import sys

from cfg.core.config import CollectionConfig, ConnectionsConfig, EnvConfig, HistoryConfig, ProjectConfig, load_config
from cfg.core.engine import Engine
from cfg.core.hashing import hash_doc

from tests.test_engine_safety import FakeAdapter


ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_llm_provider_code_lives_only_in_cfg_impact_plugin() -> None:
    assert not (ROOT / "src" / "cfg" / "llm").exists()
    assert not (ROOT / "src" / "cfg" / "impact").exists()

    forbidden = [
        r"\bimport\s+anthropic\b",
        r"\bfrom\s+anthropic\b",
        r"\bimport\s+openai\b",
        r"\bfrom\s+openai\b",
        r"\bimport\s+httpx\b",
        r"\bfrom\s+httpx\b",
        r"\bcfg\.llm\b",
        r"\bLLMProviderFactory\b",
    ]
    offenders: list[str] = []
    for path in (ROOT / "src" / "cfg").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            if re.search(pattern, text):
                offenders.append(f"{path.relative_to(ROOT)} matches {pattern}")
    assert offenders == []


def test_connections_ai_provider_loads_from_cfg_toml() -> None:
    project = load_config(ROOT / "examples" / ".cfg.toml")

    assert project.connections.ai_provider == "claude"
    assert project.connections.enabled is False


def test_impact_provider_factory_is_plugin_local() -> None:
    sys.path.insert(0, str(ROOT / "plugins" / "cfg_impact"))
    try:
        from cfg_impact.providers.factory import ImpactProviderFactory
    finally:
        sys.path.pop(0)

    claude = ImpactProviderFactory.create_provider("claude", api_key="test-key")
    openai = ImpactProviderFactory.create_provider("openai", api_key="test-key")

    assert claude.provider_name == "claude"
    assert openai.provider_name == "openai"


def test_llm_impact_requires_share_with_ai_allowlist() -> None:
    sys.path.insert(0, str(ROOT / "plugins" / "cfg_impact"))
    try:
        from cfg_impact.overview import overview
    finally:
        sys.path.pop(0)

    engine = _impact_engine(share_with_ai=())

    result = overview(engine, "demo:alpha", use_llm=True)

    assert result["llm"]["enabled"] is False
    assert result["llm"]["blocked"] is True
    assert "share_with_ai" in result["llm"]["reason"]


def test_llm_prompt_payload_never_contains_raw_changes() -> None:
    sys.path.insert(0, str(ROOT / "plugins" / "cfg_impact"))
    try:
        from cfg_impact.overview import _overview_prompt_payload
    finally:
        sys.path.pop(0)

    payload = _overview_prompt_payload(
        {
            "record": "demo:alpha",
            "changes": [{"path": "/api_key", "new": "sk-secret"}],
            "affected_records": [
                {"collection": "demo", "record_id": "beta", "state": "clean", "matched_values": ["sk-secret"]}
            ],
        }
    )

    assert "changes" not in payload
    assert payload["affected_records"] == [
        {"collection": "demo", "record_id": "beta", "state": "clean", "match_count": 1}
    ]


def _impact_engine(*, share_with_ai: tuple[str, ...]) -> Engine:
    coll = CollectionConfig(name="demo", id_field="id", secret_fields=("api_key",))
    head_doc = {"id": "alpha", "value": "old", "api_key": "sk-secret"}
    live_doc = {"id": "alpha", "value": "new", "api_key": "sk-secret"}
    oid = hash_doc(head_doc, coll)
    head = {
        "env": "dev",
        "collection": "demo",
        "record_id": "alpha",
        "seq": 1,
        "oid": oid,
        "parent_oid": None,
        "doc": {"id": "alpha", "value": "old"},
        "message": "seed",
        "author": "dev@example.com",
        "recorded_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        "valid_from": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        "valid_to": None,
        "valid_from_estimated": False,
        "op": "import",
        "git_shas": [],
        "tags": [],
        "meta": {},
    }
    project = ProjectConfig(
        name="impact-test",
        path=ROOT / ".cfg.toml",
        history=HistoryConfig(),
        collections=(coll,),
        envs={"dev": EnvConfig(name="dev", database="fake", uri="", db="test")},
        connections=ConnectionsConfig(ai_provider="openai", share_with_ai=share_with_ai),
    )
    adapter = FakeAdapter(
        project=project,
        records={("demo", "alpha"): live_doc},
        history=[head],
        heads={("demo", "alpha"): head},
    )
    return Engine(project, adapter, env="dev", author="dev@example.com")
