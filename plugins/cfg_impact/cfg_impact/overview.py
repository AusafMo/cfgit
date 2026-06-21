# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""Whole-system impact overview for cfgit diffs."""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from cfg.core.engine import Engine, RecordRef
from cfg.core.hashing import strip_for_hash
from cfg.interfaces.actions import parse_record, to_json
from cfg_impact.providers.factory import ImpactProviderFactory


RISKY_TOKENS = {
    "enabled", "active", "jobrouter", "provider", "model", "fallback",
    "retry", "timeout", "pricing", "price", "cost", "tool", "tools",
    "skill", "skills", "contract", "schema", "instructions", "prompt",
}

HIGH_TOKENS = {
    "enabled", "active", "jobrouter", "provider", "model", "fallback",
    "contract", "schema", "tool", "tools",
}

_CONSENT_LOGGED: set[tuple[str, tuple[str, ...]]] = set()


def deterministic_overview(
    engine: Engine,
    record: str,
    *,
    a: str = "=HEAD",
    b: str = "=live",
) -> dict[str, Any]:
    ref = parse_record(record)
    coll = engine.config.collection(ref.collection)
    changes = engine.diff(ref, a, b)
    left = strip_for_hash(engine.resolve_ref(ref, a)["doc"], coll)
    right = strip_for_hash(engine.resolve_ref(ref, b)["doc"], coll)
    paths = [str(change.get("path", "")) for change in changes]
    changed_values = _changed_string_values(changes)
    affected = _find_affected_records(engine, source=ref, values=[ref.record_id, *changed_values])
    declared_links = _declared_links(engine, paths)
    categories = _categories(paths)
    risk_level = _risk_level(paths, changes, affected)
    summary = _deterministic_summary(record, changes, categories, affected, declared_links, risk_level)

    return {
        "record": record,
        "from": a,
        "to": b,
        "risk_level": risk_level,
        "summary": summary,
        "categories": categories,
        "changed_paths": paths,
        "change_count": len(changes),
        "declared_links_changed": declared_links,
        "affected_records": affected,
        "rollback_note": "Use restore on this record or restore system by tag/as-of if this change already shipped.",
        "unknowns": _unknowns(left, right, affected),
        "changes": changes,
    }


async def overview_with_optional_llm(
    engine: Engine,
    record: str,
    *,
    a: str = "=HEAD",
    b: str = "=live",
    provider: str | None = None,
    model: str | None = None,
    use_llm: bool = False,
) -> dict[str, Any]:
    overview_data = deterministic_overview(engine, record, a=a, b=b)
    if not use_llm:
        overview_data["llm"] = {"enabled": False}
        return overview_data

    allowed, reason = _llm_allowed(engine, record)
    provider_name = provider or engine.config.connections.ai_provider
    if not allowed:
        overview_data["llm"] = {
            "enabled": False,
            "blocked": True,
            "provider": provider_name,
            "reason": reason,
        }
        return overview_data

    llm = ImpactProviderFactory.create_provider(provider_name, model=model)
    _log_llm_consent(llm.provider_name, [record])
    result = await llm.narrate(
        _overview_prompt_payload(overview_data),
        json_dumps=lambda payload: json.dumps(payload, indent=2, sort_keys=True),
        temperature=0.1,
        max_tokens=900,
    )
    parsed = _parse_jsonish(result.get("content", ""))
    overview_data["llm"] = {
        "enabled": True,
        "provider": llm.provider_name,
        "model": result.get("model") or llm.model,
        "usage": result.get("usage") or {},
        "overview": parsed or {"summary": result.get("content", "")},
    }
    return overview_data


def overview(
    engine: Engine,
    record: str,
    *,
    a: str = "=HEAD",
    b: str = "=live",
    provider: str | None = None,
    model: str | None = None,
    use_llm: bool = False,
) -> dict[str, Any]:
    return asyncio.run(
        overview_with_optional_llm(
            engine,
            record,
            a=a,
            b=b,
            provider=provider,
            model=model,
            use_llm=use_llm,
        )
    )


def _find_affected_records(
    engine: Engine,
    *,
    source: RecordRef,
    values: list[str],
) -> list[dict[str, Any]]:
    needles = [value for value in values if isinstance(value, str) and len(value) >= 3]
    affected: list[dict[str, Any]] = []
    for row in engine.status():
        if row.collection == source.collection and row.record_id == source.record_id:
            continue
        try:
            doc = engine.resolve_ref(RecordRef(row.collection, row.record_id), "=live")["doc"]
        except Exception:
            continue
        text = json.dumps(to_json(doc), sort_keys=True)
        matches = sorted({needle for needle in needles if needle in text})
        if matches:
            affected.append(
                {
                    "collection": row.collection,
                    "record_id": row.record_id,
                    "state": row.state,
                    "matched_values": matches[:8],
                }
            )
    return affected


def _changed_string_values(changes: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for change in changes:
        for key in ("old", "new"):
            value = change.get(key)
            if isinstance(value, str) and 3 <= len(value) <= 120:
                values.append(value)
            elif isinstance(value, list):
                values.extend(
                    str(item)
                    for item in value
                    if isinstance(item, str) and 3 <= len(item) <= 120
                )
    return values


def _categories(paths: list[str]) -> list[str]:
    out: set[str] = set()
    for path in paths:
        lowered = path.lower()
        if any(token in lowered for token in ("instructions", "prompt", "system")):
            out.add("agent_behavior")
        if any(token in lowered for token in ("tool", "skill", "contract", "schema")):
            out.add("interface_or_contract")
        if any(token in lowered for token in ("provider", "model", "fallback")):
            out.add("model_routing")
        if any(token in lowered for token in ("enabled", "active", "jobrouter", "retry", "timeout")):
            out.add("runtime_routing")
        if any(token in lowered for token in ("pricing", "price", "cost")):
            out.add("cost")
    return sorted(out) or ["data_change"]


def _declared_links(engine: Engine, paths: list[str]) -> list[dict[str, Any]]:
    links = getattr(engine.config.connections, "links", ())
    changed: list[dict[str, Any]] = []
    for link in links:
        field = str(link.get("field", ""))
        if not field:
            continue
        field_path = f"/{field}".lower()
        if any(path.lower() == field_path or path.lower().startswith(field_path + "/") for path in paths):
            changed.append({"field": field, "means": link.get("means", "")})
    return changed


def _risk_level(paths: list[str], changes: list[dict[str, Any]], affected: list[dict[str, Any]]) -> str:
    lowered_paths = " ".join(paths).lower()
    if any(token in lowered_paths for token in HIGH_TOKENS) or len(affected) >= 3:
        return "high"
    if any(token in lowered_paths for token in RISKY_TOKENS) or len(changes) >= 5 or affected:
        return "medium"
    return "low"


def _deterministic_summary(
    record: str,
    changes: list[dict[str, Any]],
    categories: list[str],
    affected: list[dict[str, Any]],
    declared_links: list[dict[str, Any]],
    risk_level: str,
) -> str:
    category_text = ", ".join(categories)
    affected_text = f"{len(affected)} related record(s)" if affected else "no related records found by static scan"
    link_text = (
        f" Declared connection fields changed: {', '.join(item['field'] for item in declared_links)}."
        if declared_links
        else ""
    )
    return (
        f"{record} changes {len(changes)} path(s), mainly {category_text}. "
        f"Static scan found {affected_text}.{link_text} Risk is {risk_level}."
    )


def _unknowns(left: dict[str, Any], right: dict[str, Any], affected: list[dict[str, Any]]) -> list[str]:
    unknowns = []
    if not affected:
        unknowns.append("Static reference scan may miss dynamic references built in application code.")
    if left == right:
        unknowns.append("No effective diff after ignored and secret fields were stripped.")
    return unknowns


def _overview_prompt_payload(overview_data: dict[str, Any]) -> dict[str, Any]:
    payload = {key: value for key, value in overview_data.items() if key != "changes"}
    payload["affected_records"] = [
        {
            "collection": item.get("collection"),
            "record_id": item.get("record_id"),
            "state": item.get("state"),
            "match_count": len(item.get("matched_values") or []),
        }
        for item in payload.get("affected_records", [])
    ]
    return payload


def _llm_allowed(engine: Engine, record: str) -> tuple[bool, str]:
    allow = set(engine.config.connections.share_with_ai)
    ref = parse_record(record)
    if "*" in allow or record in allow or ref.record_id in allow or f"{ref.collection}:*" in allow:
        return True, "allowed by connections.share_with_ai"
    return (
        False,
        f"{record} is not listed in [connections].share_with_ai; returning local structure only",
    )


def _log_llm_consent(provider_name: str, records: list[str]) -> None:
    key = (provider_name, tuple(sorted(records)))
    if key in _CONSENT_LOGGED:
        return
    _CONSENT_LOGGED.add(key)
    print(
        f"cfg-impact: sending redacted structural diff for {', '.join(records)} to {provider_name}",
        file=sys.stderr,
    )


def _parse_jsonish(text: str) -> dict[str, Any] | None:
    raw = text.strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
