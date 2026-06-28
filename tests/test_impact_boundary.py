from __future__ import annotations

import pathlib
import re
import sys

from cfg.core.config import CollectionConfig, ConnectionsConfig, EnvConfig, HistoryConfig, ProjectConfig, load_config
from cfg.core.engine import Engine, RecordRef
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
    gemini = ImpactProviderFactory.create_provider("gemini", api_key="test-key")
    google = ImpactProviderFactory.create_provider("google", api_key="test-key")

    assert claude.provider_name == "claude"
    assert openai.provider_name == "openai"
    assert gemini.provider_name == "gemini"
    assert google.provider_name == "gemini"


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


def test_impact_payload_includes_actual_field_diff_values() -> None:
    sys.path.insert(0, str(ROOT / "plugins" / "cfg_impact"))
    try:
        from cfg_impact.overview import _overview_prompt_payload
    finally:
        sys.path.pop(0)

    payload = _overview_prompt_payload(
        {
            "record": "demo:alpha",
            "changes": [{"path": "/instructions", "op": "change", "before": "do x", "after": "do y"}],
            "affected_records": [],
        }
    )

    assert payload["field_diffs"] == [
        {"path": "/instructions", "op": "change", "before": "do x", "after": "do y"}
    ]
    assert "changes" not in payload


def test_parse_jsonish_strips_fenced_json() -> None:
    sys.path.insert(0, str(ROOT / "plugins" / "cfg_impact"))
    try:
        from cfg_impact.overview import _parse_jsonish
    finally:
        sys.path.pop(0)

    assert _parse_jsonish('```json\n{"summary":"ok"}\n```') == {"summary": "ok"}


def test_changed_string_values_reads_before_after_keys() -> None:
    sys.path.insert(0, str(ROOT / "plugins" / "cfg_impact"))
    try:
        from cfg_impact.overview import _changed_string_values
    finally:
        sys.path.pop(0)

    values = _changed_string_values(
        [{"path": "/tool", "op": "change", "before": "old_tool", "after": "new_tool"}]
    )

    assert "old_tool" in values
    assert "new_tool" in values


def test_system_map_gates_contract_and_instruction_text() -> None:
    sys.path.insert(0, str(ROOT / "plugins" / "cfg_impact"))
    try:
        from cfg_impact.overview import _system_map
    finally:
        sys.path.pop(0)

    engine = _impact_engine(
        share_with_ai=("demo:alpha",),
        extra_records={
            ("demo", "beta"): {
                "id": "beta",
                "tools": ["planner"],
                "phase_contract": "handoff must include exact citation ids",
                "instructions": "very private operating instruction",
            }
        },
    )

    gated = _system_map(engine, exclude=RecordRef("demo", "alpha"), allow={"demo:alpha"})

    assert gated["configs"][0]["record_id"] == "beta"
    assert gated["configs"][0]["text_withheld"] == "not in share_with_ai"
    assert "contract" not in gated["configs"][0]
    assert "instructions_excerpt" not in gated["configs"][0]

    allowed = _system_map(
        engine,
        exclude=RecordRef("demo", "alpha"),
        allow={"demo:alpha", "demo:beta"},
    )

    assert allowed["configs"][0]["contract"] == "handoff must include exact citation ids"
    assert allowed["configs"][0]["instructions_excerpt"] == "very private operating instruction"


def test_system_map_scopes_to_explicit_against_records_only() -> None:
    sys.path.insert(0, str(ROOT / "plugins" / "cfg_impact"))
    try:
        from cfg_impact.overview import _system_map
    finally:
        sys.path.pop(0)

    engine = _impact_engine(
        share_with_ai=("demo:gamma",),
        extra_records={
            ("demo", "beta"): {"id": "beta", "tools": ["planner"]},
            ("demo", "gamma"): {
                "id": "gamma",
                "phase_contract": "gamma consumes planner output",
                "instructions": "compare the new plan to the original user ask",
            },
        },
    )

    scoped = _system_map(
        engine,
        exclude=RecordRef("demo", "alpha"),
        allow={"demo:gamma"},
        against={"demo:gamma"},
    )

    assert scoped["scoped"] is True
    assert "other_record_ids" not in scoped
    assert [item["record_id"] for item in scoped["configs"]] == ["gamma"]
    assert scoped["configs"][0]["contract"] == "gamma consumes planner output"
    assert scoped["configs"][0]["instructions_excerpt"] == "compare the new plan to the original user ask"


def test_against_payload_parser_accepts_strings_and_lists() -> None:
    from cfg.interfaces.actions import _as_record_list

    assert _as_record_list("demo:beta,demo:gamma\nmodel:a") == ["demo:beta", "demo:gamma", "model:a"]
    assert _as_record_list(["demo:beta,demo:gamma", "model:a"]) == ["demo:beta", "demo:gamma", "model:a"]
    assert _as_record_list("") is None


def _impact_engine(
    *,
    share_with_ai: tuple[str, ...],
    extra_records: dict[tuple[str, str], dict[str, object]] | None = None,
) -> Engine:
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
    records = {("demo", "alpha"): live_doc}
    records.update(extra_records or {})
    adapter = FakeAdapter(
        project=project,
        records=records,
        history=[head],
        heads={("demo", "alpha"): head},
    )
    return Engine(project, adapter, env="dev", author="dev@example.com")
