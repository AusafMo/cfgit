# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""Shared action layer for CLI, MCP, and UI."""
from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, time, timezone
import json
from pathlib import Path
from typing import Any

from cfg.adapters.base import AtomicityUnavailable, AmbiguousConfig, NoSuchConfig, StaleHead, StaleLive
from cfg.core.authz import PermissionDenied, permission_role
from cfg.core.config import ProjectConfig, load_config
from cfg.core.diff import format_diff
from cfg.core.engine import BranchingDisabled, Engine, RecordRef, SecretBlocked
from cfg.core.identity import IdentityError, resolve_identity, resolve_self_asserted_author
from cfg.core import remedy


@dataclass(frozen=True)
class ActionContext:
    config_file: str | None = None
    env: str = "dev"
    author: str | None = None
    branch: str | None = None


EXIT_OK = 0
EXIT_ARG = 1
EXIT_DIRTY = 2
EXIT_STORAGE = 3
EXIT_FORBIDDEN = 4
EXIT_NOT_FOUND = 5
EXIT_INVARIANT = 6


def make_engine(ctx: ActionContext) -> Engine:
    project = load_config(ctx.config_file)
    return engine_for_project(project, env_name=ctx.env, author=ctx.author)


def engine_for_project(project: ProjectConfig, *, env_name: str, author: str | None) -> Engine:
    if env_name not in project.envs:
        raise ValueError(f"unknown env: {env_name}")
    env = project.envs[env_name]
    if env.database == "mongo":
        from cfg.adapters.mongo import MongoAdapter

        adapter = MongoAdapter(project=project, env_name=env_name)
    elif env.database == "postgres":
        from cfg.adapters.postgres import PostgresAdapter

        adapter = PostgresAdapter(project=project, env_name=env_name)
    else:
        raise ValueError(f"unsupported database for v1 slice: {env.database}")
    identity = resolve_identity(env, adapter, explicit_author=author)
    return Engine(project, adapter, env=env_name, identity=identity)


def envelope(fn, *args, record: str | None = None, **kwargs) -> dict[str, Any]:
    try:
        data, code = fn(*args, **kwargs)
        status = "ok" if code == EXIT_OK else "dirty" if code == EXIT_DIRTY else "error"
        env = {"status": status, "code": code, "message": "", "data": to_json(data)}
        _attach_next(env, record=record)
        return env
    except AmbiguousConfig as exc:
        return _error("bad_config", EXIT_INVARIANT, exc, record=record)
    except (StaleHead, StaleLive) as exc:
        return _error("changed_outside_cfgit", EXIT_DIRTY, exc, record=record)
    except PermissionDenied as exc:
        return _error("forbidden", EXIT_FORBIDDEN, exc, record=record)
    except IdentityError as exc:
        return _error("identity_required", EXIT_FORBIDDEN, exc, record=record)
    except AtomicityUnavailable as exc:
        return _error("atomicity_unavailable", EXIT_STORAGE, exc, record=record)
    except NoSuchConfig as exc:
        return _error("not_found", EXIT_NOT_FOUND, exc, record=record)
    except (BranchingDisabled, SecretBlocked, ValueError, FileNotFoundError, KeyError) as exc:
        return _error("error", EXIT_ARG, exc, record=record)
    except Exception as exc:
        return _error("error", EXIT_STORAGE, exc, record=record)


def whoami(engine: Engine) -> tuple[dict[str, Any], int]:
    env = engine.config.envs[engine.env]
    return {
        "author": engine.author,
        "identity": engine.identity.history_meta(),
        "identity_display": engine.identity.display,
        "env": engine.env,
        "database": env.database,
        "permission_role": permission_role(env.permissions, engine.author),
        "permission_mode": env.permissions.mode,
        "identity_mode": env.identity.mode,
        "needs_approval": env.needs_approval,
        "open_mode_warning": remedy.OPEN_MODE_WARNING
        if remedy.open_mode_on_guarded_env(needs_approval=env.needs_approval, identity_mode=env.identity.mode)
        else None,
        "config_file": str(engine.config.path),
    }, EXIT_OK


def init(engine: Engine) -> tuple[dict[str, Any], int]:
    result = engine.init()
    violations = result["invariant_violations"]
    out = plain_init(result)
    warnings = _env_warnings(engine)
    if warnings:
        out["warnings"] = warnings
    return out, EXIT_INVARIANT if violations else EXIT_OK


def status_report(engine: Engine) -> tuple[dict[str, Any], int]:
    """Situational-awareness header: which config/env/db am I on, is my identity verified, is the
    store reachable, how many records are tracked, how many drifted, and any env-shape warnings."""
    env = engine.config.envs[engine.env]
    reachable = True
    tracked = 0
    drift = 0
    try:
        rows = engine.status()
        tracked = sum(1 for r in rows if r.state != "new")
        drift = sum(1 for r in rows if r.state == "changed_outside_cfgit")
    except Exception:  # noqa: BLE001 - reachability probe, any failure means "can't reach store"
        reachable = False
    identity_meta = engine.identity.history_meta() if engine.identity else {}
    report = {
        "config_file": str(engine.config.path),
        "env": engine.env,
        "database": env.database,
        "db": env.db,
        "identity_mode": env.identity.mode,
        "authenticated": bool(identity_meta.get("authenticated")),
        "permission_mode": env.permissions.mode,
        "needs_approval": env.needs_approval,
        "reachable": reachable,
        "tracked": tracked,
        "drift": drift,
        "warnings": _env_warnings(engine),
        "nudges": _drift_nudges(tracked, drift),
    }
    return report, EXIT_OK


def _drift_nudges(tracked: int, drift: int) -> list[str]:
    """Turn a high drift ratio into an actionable suggestion. When a large share of tracked
    records drifted, every edit needs a manual adopt first and restore has no clean baseline —
    so nudge the operator to baseline the collection once with `cfg adopt --all`."""
    if drift >= 5 and tracked and drift / tracked >= 0.25:
        return [
            f"{drift}/{tracked} tracked records drifted — run `cfg adopt --all` to baseline the "
            "collection, then edits and restore work without a per-record adopt."
        ]
    return []


def _env_warnings(engine: Engine) -> list[str]:
    env = engine.config.envs[engine.env]
    warnings: list[str] = []
    if remedy.open_mode_on_guarded_env(needs_approval=env.needs_approval, identity_mode=env.identity.mode):
        warnings.append(remedy.OPEN_MODE_WARNING)
    return warnings


def status(engine: Engine, record: str | None = None) -> tuple[list[Any], int]:
    rows = engine.status(parse_record(record) if record else None)
    code = EXIT_DIRTY if any(r.state == "changed_outside_cfgit" for r in rows) else EXIT_OK
    return rows, code


def doctor(engine: Engine, record: str | None = None, *, large_field_bytes: int = 20000) -> tuple[dict[str, Any], int]:
    report = engine.doctor(
        parse_record(record) if record else None,
        large_field_bytes=large_field_bytes,
    )
    code = EXIT_OK if report.get("ok") else EXIT_DIRTY
    return report, code


def import_records(
    engine: Engine,
    record: str | None,
    *,
    all_records: bool,
    message: str,
    allow_secret: bool = False,
) -> tuple[Any, int]:
    if not all_records and not record:
        raise ValueError("import needs all_records=true or a record")
    result = engine.import_records(
        parse_record(record) if record else None,
        message=message,
        allow_secret=allow_secret,
    )
    return result, EXIT_OK


EXPORT_WARN_RECORDS = 2000
EXPORT_WARN_BYTES = 50 * 1024 * 1024  # 50 MB serialized


def export_records(engine: Engine, record: str | None = None) -> tuple[dict[str, Any], int]:
    report = engine.export_records(parse_record(record) if record else None)
    warning = _export_size_warning(report)
    if warning:
        report["warning"] = warning
    return report, EXIT_OK


def _export_size_warning(report: dict[str, Any]) -> str | None:
    """Soft warning (never a hard cap) when a snapshot is large by control-plane standards —
    it usually means the config points at a data-plane collection (events, user content, jobs),
    which cfgit is not designed to version. Triggers on record count OR serialized size."""
    count = report.get("count", 0)
    try:
        size = len(json.dumps(to_json(report)))
    except (TypeError, ValueError):
        size = 0
    reasons = []
    if count >= EXPORT_WARN_RECORDS:
        reasons.append(f"{count} records")
    if size >= EXPORT_WARN_BYTES:
        reasons.append(f"~{size // (1024 * 1024)}MB")
    if not reasons:
        return None
    return (
        f"large export ({', '.join(reasons)}). cfgit is built for control-plane collections "
        "(hundreds–low thousands of hand-curated records). If this is a data-plane collection "
        "(events, user content, jobs), it is the wrong fit — use a backup or warehouse instead."
    )


def import_from_file(
    engine: Engine,
    export_obj: Any,
    *,
    message: str,
    allow_secret: bool = False,
    dry_run: bool = False,
) -> tuple[dict[str, Any], int]:
    """Restore documents from a `cfg export` artifact by writing them back through the
    drift-guarded bulk-commit path (preflights the whole batch; applies none on any drift)."""
    items = _export_items_to_commit(export_obj)
    if dry_run:
        return bulk_commit_preview(engine, items), EXIT_OK
    result = engine.commit_many(items, message=message, allow_secret=allow_secret)
    return result, bulk_commit_exit_code(result)


def _export_items_to_commit(export_obj: Any) -> list[tuple[RecordRef, dict[str, Any]]]:
    if isinstance(export_obj, dict) and export_obj.get("kind") == "cfgit-export":
        rows = export_obj.get("items") or []
    elif isinstance(export_obj, list):
        rows = export_obj
    else:
        raise ValueError("import --from expects a cfg export artifact or a list of {record, doc}")
    out: list[tuple[RecordRef, dict[str, Any]]] = []
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or "doc" not in row:
            raise ValueError(f"export item {i} needs a doc")
        rec = row.get("record") or (
            f"{row.get('collection')}:{row.get('record_id')}" if row.get("collection") else None
        )
        if not rec:
            raise ValueError(f"export item {i} needs record or collection+record_id")
        out.append((parse_record(rec), row["doc"]))
    return out


def diff(engine: Engine, record: str, a: str = "=HEAD", b: str = "=live") -> tuple[dict[str, Any], int]:
    changes = engine.diff(parse_record(record), a, b)
    return {"changes": changes, "text": format_diff(changes)}, EXIT_OK


def commit(
    engine: Engine,
    record: str,
    doc: dict[str, Any],
    *,
    message: str,
    allow_secret: bool = False,
    branch: str | None = None,
    dry_run: bool = False,
) -> tuple[dict[str, Any], int]:
    on_branch = bool(branch and branch != engine.config.branches.default_branch)
    if dry_run:
        if on_branch:
            raise ValueError("--dry-run is only supported on the main-branch commit path")
        result = engine.commit_preview(parse_record(record), doc, allow_secret=allow_secret)
    elif on_branch:
        result = engine.branch_commit(branch, parse_record(record), doc, message=message, allow_secret=allow_secret)
    else:
        result = engine.commit(parse_record(record), doc, message=message, allow_secret=allow_secret)
    code = EXIT_DIRTY if result.get("state") == "changed_outside_cfgit" else EXIT_OK
    return result, code


def set_fields(
    engine: Engine,
    record: str,
    assignments: list[tuple[str, Any]],
    *,
    message: str,
    allow_secret: bool = False,
    dry_run: bool = False,
) -> tuple[dict[str, Any], int]:
    """Fast-path scalar edit: fetch live, apply dotted assignments, route through the SAME
    commit path (drift guard + secret scan + history) — never a raw write."""
    ref = parse_record(record)
    if not assignments:
        raise ValueError("set needs at least one field=value")
    doc = engine.build_commit_doc(ref, assignments)
    if dry_run:
        result = engine.commit_preview(ref, doc, allow_secret=allow_secret)
        return result, EXIT_OK
    result = engine.commit(ref, doc, message=message, allow_secret=allow_secret)
    code = EXIT_DIRTY if result.get("state") == "changed_outside_cfgit" else EXIT_OK
    return result, code


def parse_assignments(pairs: list[str]) -> list[tuple[str, Any]]:
    """Parse `field=value` tokens. Values are JSON-coerced (true/5/["a"] type naturally); a
    `str:` prefix forces a string literal (e.g. version=str:1.0)."""
    out: list[tuple[str, Any]] = []
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"expected field=value, got {pair!r}")
        field, raw = pair.split("=", 1)
        field = field.strip()
        if not field:
            raise ValueError(f"empty field name in {pair!r}")
        out.append((field, _coerce_value(raw)))
    return out


def _coerce_value(raw: str) -> Any:
    if raw.startswith("str:"):
        return raw[4:]
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _assignments_from_payload(value: Any) -> list[tuple[str, Any]]:
    """Accept assignments as a dict {path: typed_value} (MCP), a list of `field=value` strings
    (CLI), or a JSON string of either."""
    if value is None:
        raise ValueError("set needs assignments")
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, dict):
        return [(str(k), v) for k, v in value.items()]
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            return parse_assignments(value)
        # list of {field, value} objects
        out: list[tuple[str, Any]] = []
        for item in value:
            if not isinstance(item, dict) or "field" not in item:
                raise ValueError("each assignment needs a 'field' (and 'value')")
            out.append((str(item["field"]), item.get("value")))
        return out
    raise ValueError("assignments must be a mapping, a list, or a JSON string")


def bulk_commit(
    engine: Engine,
    items: Any,
    *,
    message: str,
    allow_secret: bool = False,
    branch: str | None = None,
    dry_run: bool = False,
) -> tuple[dict[str, Any], int]:
    parsed = _bulk_commit_items(items)
    if dry_run:
        if branch and branch != engine.config.branches.default_branch:
            raise ValueError("--dry-run is only supported on the main-branch commit path")
        return bulk_commit_preview(engine, parsed), EXIT_OK
    if branch and branch != engine.config.branches.default_branch:
        result = engine.branch_commit_many(branch, parsed, message=message, allow_secret=allow_secret)
    else:
        result = engine.commit_many(parsed, message=message, allow_secret=allow_secret)
    return result, bulk_commit_exit_code(result)


def bulk_commit_preview(
    engine: Engine, items: list[tuple[RecordRef, dict[str, Any]]]
) -> dict[str, Any]:
    """Dry-run a bulk commit: preview each record without writing. Aggregates per-record
    would_commit / noop / changed_outside_cfgit so the operator sees the whole blast radius of a
    collection-scale replace before applying it. Mirrors commit_many's per-record semantics."""
    results: list[dict[str, Any]] = []
    would_change = drift = noop = 0
    for ref, doc in items:
        try:
            preview = engine.commit_preview(ref, doc)
        except NoSuchConfig:
            results.append({"record": f"{ref.collection}:{ref.record_id}", "state": "missing"})
            continue
        state = preview.get("state")
        if state == "would_commit":
            would_change += 1
        elif state == "changed_outside_cfgit":
            drift += 1
        elif state == "noop":
            noop += 1
        results.append({"record": f"{ref.collection}:{ref.record_id}", **preview})
    return {
        "state": "dry_run",
        "summary": {"total": len(items), "would_commit": would_change, "drift": drift, "noop": noop},
        "results": results,
    }


def log(engine: Engine, record: str, *, limit: int | None = 20) -> tuple[list[dict[str, Any]], int]:
    return engine.log(parse_record(record), limit=limit), EXIT_OK


def recent_history(engine: Engine, *, limit: int | None = 50) -> tuple[list[dict[str, Any]], int]:
    return engine.recent_history(limit=limit), EXIT_OK


def show(engine: Engine, record: str, ref: str) -> tuple[dict[str, Any], int]:
    return engine.resolve_ref(parse_record(record), ref), EXIT_OK


def adopt(
    engine: Engine,
    record: str | None,
    *,
    all_records: bool,
    message: str,
    allow_secret: bool = False,
) -> tuple[Any, int]:
    if all_records:
        results = []
        for row in engine.status():
            if row.state == "changed_outside_cfgit":
                result = engine.adopt(
                    RecordRef(row.collection, row.record_id),
                    message=message,
                    allow_secret=allow_secret,
                )
                results.append({"collection": row.collection, "record_id": row.record_id, **result})
        return results, EXIT_OK
    if not record:
        raise ValueError("adopt needs all_records=true or a record")
    return engine.adopt(parse_record(record), message=message, allow_secret=allow_secret), EXIT_OK


def restore(
    engine: Engine,
    *,
    record: str | None = None,
    ref: str | None = None,
    as_of: str | None = None,
    tag: str | None = None,
    dry_run: bool = False,
    message: str,
) -> tuple[dict[str, Any], int]:
    if as_of and tag:
        raise ValueError("restore accepts only one of as_of or tag")
    if as_of:
        if record or ref:
            raise ValueError("restore as_of restores all records; omit record and ref")
        result = engine.restore_system_as_of(parse_when(as_of), message=message, dry_run=dry_run)
        code = restore_exit_code(result)
        return result, code
    if tag:
        if record or ref:
            raise ValueError("restore tag restores all records; omit record and ref")
        result = engine.restore_system_tag(tag, message=message, dry_run=dry_run)
        code = restore_exit_code(result)
        return result, code
    if dry_run:
        raise ValueError("dry_run is only supported with system restore")
    if not record or not ref:
        raise ValueError("restore needs record and ref, or as_of/tag")
    result = engine.restore(parse_record(record), ref, message=message)
    code = EXIT_DIRTY if result.get("state") == "changed_outside_cfgit" else EXIT_OK
    return result, code


def tag(engine: Engine, name: str) -> tuple[list[dict[str, Any]], int]:
    return engine.tag(name), EXIT_OK


def branch_list(engine: Engine) -> tuple[list[dict[str, Any]], int]:
    return engine.branch_list(), EXIT_OK


def branch_create(
    engine: Engine,
    name: str,
    *,
    from_branch: str = "main",
    message: str | None = None,
) -> tuple[dict[str, Any], int]:
    return engine.branch_create(name, from_branch=from_branch, message=message), EXIT_OK


def branch_delete(engine: Engine, name: str) -> tuple[dict[str, Any], int]:
    return engine.branch_delete(name), EXIT_OK


def branch_diff(engine: Engine, range_expr: str) -> tuple[dict[str, Any], int]:
    return engine.branch_diff(range_expr), EXIT_OK


def branch_log(engine: Engine, branch: str, *, limit: int | None = 20) -> tuple[list[dict[str, Any]], int]:
    return engine.branch_log(branch, limit=limit), EXIT_OK


def pr_create(engine: Engine, *, base: str, head: str, message: str) -> tuple[dict[str, Any], int]:
    return engine.pr_create(base=base, head=head, message=message), EXIT_OK


def pr_list(engine: Engine, *, status: str | None = None) -> tuple[list[dict[str, Any]], int]:
    return engine.pr_list(status=status), EXIT_OK


def pr_show(engine: Engine, pr_id: str) -> tuple[dict[str, Any], int]:
    return engine.pr_show(pr_id), EXIT_OK


def pr_close(engine: Engine, pr_id: str) -> tuple[dict[str, Any], int]:
    return engine.pr_close(pr_id), EXIT_OK


def pr_merge(engine: Engine, pr_id: str, *, message: str | None = None) -> tuple[dict[str, Any], int]:
    return engine.pr_merge(pr_id, message=message), EXIT_OK


def fsck(engine: Engine) -> tuple[dict[str, Any], int]:
    return {
        "invariant_violations": engine.adapter.check_runtime_invariant(),
        "atomicity": engine.adapter.check_atomicity_scope(),
        "reconcile": engine.adapter.reconcile(),
    }, EXIT_OK


def impact(
    engine: Engine,
    record: str,
    *,
    a: str = "=HEAD",
    b: str = "=live",
    use_llm: bool = False,
    provider: str | None = None,
    model: str | None = None,
    against: list[str] | None = None,
) -> tuple[dict[str, Any], int]:
    try:
        from cfg_impact.overview import overview
    except ModuleNotFoundError as exc:
        raise ValueError(
            "cfgit-impact plugin is not installed. Install plugins/cfg_impact or cfgit-impact."
        ) from exc
    return (
        overview(engine, record, a=a, b=b, use_llm=use_llm, provider=provider, model=model, against=against),
        EXIT_OK,
    )


def run_named_action(name: str, engine: Engine, payload: dict[str, Any] | None = None) -> tuple[Any, int]:
    payload = payload or {}
    if name == "whoami":
        return whoami(engine)
    if name == "init":
        return init(engine)
    if name == "status":
        return status(engine, _blank_to_none(payload.get("record")))
    if name == "doctor":
        return doctor(
            engine,
            _blank_to_none(payload.get("record")),
            large_field_bytes=int(payload.get("large_field_bytes") or 20000),
        )
    if name == "import":
        if payload.get("export") is not None or payload.get("from") is not None:
            return import_from_file(
                engine,
                payload.get("export") if payload.get("export") is not None else payload.get("from"),
                message=str(payload.get("message") or "import from export file"),
                allow_secret=bool(payload.get("allow_secret")),
                dry_run=bool(payload.get("dry_run")),
            )
        return import_records(
            engine,
            _blank_to_none(payload.get("record")),
            all_records=bool(payload.get("all_records") or payload.get("all")),
            message=str(payload.get("message") or "initial import"),
            allow_secret=bool(payload.get("allow_secret")),
        )
    if name == "export":
        return export_records(engine, _blank_to_none(payload.get("record")))
    if name == "diff":
        return diff(
            engine,
            _required(payload, "record"),
            str(payload.get("a") or "=HEAD"),
            str(payload.get("b") or "=live"),
        )
    if name == "commit":
        if payload.get("items") is not None:
            return bulk_commit(
                engine,
                payload.get("items"),
                message=str(payload.get("message") or "commit"),
                allow_secret=bool(payload.get("allow_secret")),
                branch=_blank_to_none(payload.get("branch")),
                dry_run=bool(payload.get("dry_run")),
            )
        return commit(
            engine,
            _required(payload, "record"),
            _doc(payload.get("doc")),
            message=str(payload.get("message") or "commit"),
            allow_secret=bool(payload.get("allow_secret")),
            branch=_blank_to_none(payload.get("branch")),
            dry_run=bool(payload.get("dry_run")),
        )
    if name == "set":
        return set_fields(
            engine,
            _required(payload, "record"),
            _assignments_from_payload(payload.get("assignments")),
            message=str(payload.get("message") or "set fields"),
            allow_secret=bool(payload.get("allow_secret")),
            dry_run=bool(payload.get("dry_run")),
        )
    if name in {"bulk_commit", "commit_many"}:
        return bulk_commit(
            engine,
            payload.get("items"),
            message=str(payload.get("message") or "bulk commit"),
            allow_secret=bool(payload.get("allow_secret")),
            branch=_blank_to_none(payload.get("branch")),
            dry_run=bool(payload.get("dry_run")),
        )
    if name == "log":
        return log(engine, _required(payload, "record"), limit=int(payload.get("limit") or 20))
    if name == "recent_history":
        return recent_history(engine, limit=int(payload.get("limit") or 50))
    if name == "show":
        return show(engine, _required(payload, "record"), str(payload.get("ref") or "HEAD"))
    if name == "adopt":
        return adopt(
            engine,
            _blank_to_none(payload.get("record")),
            all_records=bool(payload.get("all_records") or payload.get("all")),
            message=str(payload.get("message") or "adopt drift"),
            allow_secret=bool(payload.get("allow_secret")),
        )
    if name == "restore":
        return restore(
            engine,
            record=_blank_to_none(payload.get("record")),
            ref=_blank_to_none(payload.get("ref")),
            as_of=_blank_to_none(payload.get("as_of")),
            tag=_blank_to_none(payload.get("tag")),
            dry_run=bool(payload.get("dry_run")),
            message=str(payload.get("message") or "restore"),
        )
    if name == "tag":
        return tag(engine, _required(payload, "name"))
    if name == "branch_list":
        return branch_list(engine)
    if name == "branch_create":
        return branch_create(
            engine,
            _required(payload, "name"),
            from_branch=str(payload.get("from_branch") or payload.get("from") or "main"),
            message=_blank_to_none(payload.get("message")),
        )
    if name == "branch_delete":
        return branch_delete(engine, _required(payload, "name"))
    if name == "branch_diff":
        return branch_diff(engine, _required(payload, "range"))
    if name == "branch_log":
        return branch_log(engine, _required(payload, "branch"), limit=int(payload.get("limit") or 20))
    if name == "pr_create":
        return pr_create(
            engine,
            base=str(payload.get("base") or "main"),
            head=_required(payload, "head"),
            message=str(payload.get("message") or "open PR"),
        )
    if name == "pr_list":
        return pr_list(engine, status=_blank_to_none(payload.get("status")))
    if name == "pr_show":
        return pr_show(engine, _required(payload, "id"))
    if name == "pr_close":
        return pr_close(engine, _required(payload, "id"))
    if name == "pr_merge":
        return pr_merge(engine, _required(payload, "id"), message=_blank_to_none(payload.get("message")))
    if name == "fsck":
        return fsck(engine)
    if name == "impact":
        return impact(
            engine,
            _required(payload, "record"),
            a=str(payload.get("a") or "=HEAD"),
            b=str(payload.get("b") or "=live"),
            use_llm=bool(payload.get("use_llm") or payload.get("llm")),
            provider=_blank_to_none(payload.get("provider")),
            model=_blank_to_none(payload.get("model")),
            against=_as_record_list(payload.get("against")),
        )
    raise ValueError(f"unknown action: {name}")


def _as_record_list(value: Any) -> list[str] | None:
    """Parse an `against` selection from a list (MCP/JSON) or a comma/space string (form)."""
    if value is None:
        return None
    if isinstance(value, str):
        items = [part.strip() for part in value.replace("\n", ",").replace(" ", ",").split(",")]
    elif isinstance(value, (list, tuple)):
        items = [
            part.strip()
            for raw in value
            for part in str(raw).replace("\n", ",").replace(" ", ",").split(",")
        ]
    else:
        return None
    items = [part for part in items if part]
    return items or None


def parse_record(raw: str | None) -> RecordRef:
    if not raw or ":" not in raw:
        raise ValueError("record must be collection:id, for example agent_configs:agent_planner")
    collection, record_id = raw.split(":", 1)
    if not collection or not record_id:
        raise ValueError("record must be collection:id")
    return RecordRef(collection, record_id)


def parse_when(raw: str) -> datetime:
    value = raw.strip()
    date_only = len(value) == 10 and value[4] == "-" and value[7] == "-"
    if date_only:
        dt = datetime.combine(datetime.fromisoformat(value).date(), time.max)
    else:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_json_file(path: str) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("--from file must contain one JSON object")
    return data


def parse_json_doc(value: str | dict[str, Any]) -> dict[str, Any]:
    return _doc(value)


def parse_bulk_commit_items(value: Any) -> list[tuple[RecordRef, dict[str, Any]]]:
    return _bulk_commit_items(value)


def to_json(value: Any) -> Any:
    if is_dataclass(value):
        return to_json(asdict(value))
    if isinstance(value, dict):
        return {str(k): to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def plain_init(result: dict[str, Any]) -> dict[str, Any]:
    atomic = result["atomic"]
    return {
        "atomic": asdict(atomic) if is_dataclass(atomic) else atomic,
        "invariant_violations": result["invariant_violations"],
        "branches": result.get("branches"),
    }


def restore_exit_code(result: dict[str, Any]) -> int:
    if result.get("state") == "blocked":
        return EXIT_DIRTY
    if result.get("state") == "partial":
        return EXIT_STORAGE
    return EXIT_OK


def bulk_commit_exit_code(result: dict[str, Any]) -> int:
    if result.get("state") == "blocked":
        return EXIT_DIRTY
    if result.get("state") == "partial":
        return EXIT_STORAGE
    return EXIT_OK


def resolve_author(explicit: str | None = None) -> str:
    return resolve_self_asserted_author(explicit)


def _bulk_commit_items(value: Any) -> list[tuple[RecordRef, dict[str, Any]]]:
    if value is None:
        raise ValueError("bulk commit needs items")
    if isinstance(value, str):
        return _bulk_commit_items(json.loads(value))
    if isinstance(value, dict):
        if "items" in value:
            return _bulk_commit_items(value["items"])
        return [(parse_record(record), _doc(doc)) for record, doc in value.items()]
    if not isinstance(value, list):
        raise ValueError("bulk commit items must be a list, mapping, or JSON string")

    out: list[tuple[RecordRef, dict[str, Any]]] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"bulk commit item {index} must be an object")
        record = _blank_to_none(item.get("record"))
        if record is None:
            collection = _blank_to_none(item.get("collection"))
            record_id = _blank_to_none(item.get("record_id") or item.get("id"))
            if not collection or not record_id:
                raise ValueError(f"bulk commit item {index} needs record or collection+record_id")
            record = f"{collection}:{record_id}"
        if "doc" not in item:
            raise ValueError(f"bulk commit item {index} needs doc")
        out.append((parse_record(record), _doc(item.get("doc"))))
    return out


def _doc(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        data = json.loads(value)
        if isinstance(data, dict):
            return data
    raise ValueError("doc must be a JSON object")


def _required(payload: dict[str, Any], key: str) -> str:
    value = _blank_to_none(payload.get(key))
    if value is None:
        raise ValueError(f"{key} is required")
    return value


def _blank_to_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _error(status: str, code: int, exc: Exception, *, record: str | None = None) -> dict[str, Any]:
    env = {"status": status, "code": code, "message": str(exc), "data": None}
    nxt = remedy.next_for(
        status=status,
        code=code,
        error_class=exc.__class__.__name__,
        record=record,
        message=str(exc),
    )
    env["state"] = None
    env["next"] = nxt.to_json() if nxt else None
    return env


def _attach_next(env: dict[str, Any], *, record: str | None) -> None:
    """Additively attach a top-level `state` echo and a `next` remedy to a success/dirty
    envelope. Both may be None. The record for `{record}` substitution is taken from the
    result payload when present, else the caller's hint."""
    data = env.get("data")
    state = None
    payload_record = record
    if isinstance(data, dict):
        state = data.get("state")
        payload_record = _record_from(data) or record
    elif isinstance(data, list):
        # batch results (adopt --all, bulk): surface the first actionable state, if any
        for item in data:
            if isinstance(item, dict) and item.get("state") in _ACTIONABLE_LIST_STATES:
                state = item.get("state")
                payload_record = _record_from(item) or record
                break
    env["state"] = state
    nxt = remedy.next_for(status=env.get("status"), code=env.get("code"), state=state, record=payload_record)
    env["next"] = nxt.to_json() if nxt else None


_ACTIONABLE_LIST_STATES = {"changed_outside_cfgit", "missing", "failed", "blocked"}


def _record_from(item: dict[str, Any]) -> str | None:
    coll = item.get("collection")
    rid = item.get("record_id")
    if coll and rid:
        return f"{coll}:{rid}"
    return None
