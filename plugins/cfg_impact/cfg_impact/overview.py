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
    against: list[str] | None = None,
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
    # cross-config context. If the caller selected records (`against`), reason against
    # ONLY those; otherwise auto-build from the whole system. Text always gated by allowlist.
    ref = parse_record(record)
    allow = set(engine.config.connections.share_with_ai)
    against_set = {a.strip() for a in against if a and a.strip()} if against else None
    system_map = _system_map(engine, exclude=ref, allow=allow, against=against_set)
    shared = [c["record_id"] for c in system_map.get("configs", []) if "instructions_excerpt" in c or "contract" in c]
    _log_llm_consent(llm.provider_name, [record, *shared])
    payload = _overview_prompt_payload(overview_data)
    payload["system"] = system_map
    result = await llm.narrate(
        payload,
        json_dumps=lambda payload: json.dumps(payload, indent=2, sort_keys=True),
        temperature=0.1,
        max_tokens=1100,
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
    against: list[str] | None = None,
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
            against=against,
        )
    )


_ROLE_FIELDS = ("agent_type", "role", "phase", "category", "description", "display_name")
_CONTRACT_FIELDS = ("phase_contract", "contract", "output_schema", "custom_input_schema")
_SYS_TEXT_CAP = 600    # per-field text excerpt sent in the system map
_SYS_MAX_CONFIGS = 40  # cap how many sibling configs we send


def _excerpt(value: Any, cap: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    s = " ".join(value.split())
    return s[:cap] + ("…" if len(s) > cap else "")


def _config_card(doc: dict[str, Any], *, with_text: bool) -> dict[str, Any]:
    card: dict[str, Any] = {}
    for f in _ROLE_FIELDS:
        if doc.get(f):
            card[f] = doc[f]
            break
    tools = doc.get("tools")
    if isinstance(tools, list) and tools:
        card["tools"] = tools[:12]
    skills = doc.get("skills")
    if isinstance(skills, list) and skills:
        card["skills"] = skills[:12]
    if with_text:
        for f in _CONTRACT_FIELDS:
            ex = _excerpt(doc.get(f), _SYS_TEXT_CAP)
            if ex:
                card["contract"] = ex
                break
    if doc.get("model"):
        card["model"] = doc["model"]
    if with_text:
        instr = doc.get("instructions") or doc.get("prompt")
        ex = _excerpt(instr, _SYS_TEXT_CAP)
        if ex:
            card["instructions_excerpt"] = ex
    return card


def _system_map(
    engine: Engine,
    *,
    exclude: RecordRef,
    allow: set[str],
    against: set[str] | None = None,
) -> dict[str, Any]:
    """Compact view of the OTHER live configs so the model reasons cross-system.
    A config's text (instructions/contract) is included only if that config is in
    the share_with_ai allowlist; otherwise just id + collection are sent.

    If `against` is given, ONLY records the user explicitly selected are included
    (matched by 'collection:record_id' or bare 'record_id'), and the non-rich tail
    is suppressed: the user scoped the context, so we send exactly that set."""

    def _allowed(coll: str, rid: str) -> bool:
        return "*" in allow or rid in allow or f"{coll}:{rid}" in allow or f"{coll}:*" in allow

    def _selected(coll: str, rid: str) -> bool:
        return against is None or f"{coll}:{rid}" in against or rid in against

    scoped = against is not None
    out: list[dict[str, Any]] = []
    for row in engine.status():
        if row.collection == exclude.collection and row.record_id == exclude.record_id:
            continue
        if not _selected(row.collection, row.record_id):
            continue
        entry: dict[str, Any] = {"collection": row.collection, "record_id": row.record_id, "state": row.state}
        with_text = _allowed(row.collection, row.record_id)
        card: dict[str, Any] = {}
        try:
            doc = engine.resolve_ref(RecordRef(row.collection, row.record_id), "=live")["doc"]
            coll = engine.config.collection(row.collection)
            doc = strip_for_hash(doc, coll)  # drop ignored/secret fields before egress
            card = _config_card(doc, with_text=with_text)
        except Exception:
            pass
        # When the user explicitly selected this record, always include it (that's the
        # whole point of selecting). Otherwise (auto mode) only include rich cards that
        # carry reasoning-relevant content, to avoid noise and needless egress.
        rich = any(k in card for k in ("instructions_excerpt", "contract", "tools", "skills"))
        if scoped or rich:
            entry.update(card)
            if not with_text:
                entry["text_withheld"] = "not in share_with_ai"
            out.append(entry)
        if not scoped and len(out) >= _SYS_MAX_CONFIGS:
            break
    result: dict[str, Any] = {"configs": out, "scoped": scoped}
    if not scoped:
        # auto mode: append a compact tail of the remaining record ids so the model
        # knows what else exists, without sending their bodies
        result["other_record_ids"] = [
            f"{r.collection}:{r.record_id}"
            for r in engine.status()
            if not (r.collection == exclude.collection and r.record_id == exclude.record_id)
        ][:200]
    return result


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
        for key in ("before", "after", "old", "new"):
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


_DIFF_FIELD_CAP = 6000  # per-side char cap so the prompt stays bounded


def _truncate(value: Any, cap: int = _DIFF_FIELD_CAP) -> Any:
    if isinstance(value, str) and len(value) > cap:
        return value[:cap] + f"\n…[truncated {len(value) - cap} chars]"
    return value


def _overview_prompt_payload(overview_data: dict[str, Any]) -> dict[str, Any]:
    payload = {key: value for key, value in overview_data.items() if key != "changes"}
    # Include the ACTUAL field-level before/after so the model can read what changed,
    # not just which fields changed. The diff is already secret-stripped upstream
    # (engine.diff -> strip_for_hash), so no secret/ignored content reaches here.
    payload["field_diffs"] = [
        {
            "path": change.get("path"),
            "op": change.get("op"),
            "before": _truncate(change.get("before")),
            "after": _truncate(change.get("after")),
        }
        for change in overview_data.get("changes", [])
    ]
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
        f"cfgit-impact: sending redacted structural diff for {', '.join(records)} to {provider_name}",
        file=sys.stderr,
    )


def _parse_jsonish(text: str) -> dict[str, Any] | None:
    raw = text.strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = raw[3:].strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
        fence_end = raw.rfind("```")
        if fence_end != -1:
            raw = raw[:fence_end].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
