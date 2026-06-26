# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""cfg CLI."""
from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import datetime, time, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

from cfg.adapters.base import AtomicityUnavailable, AmbiguousConfig, NoSuchConfig, StaleHead, StaleLive
from cfg.core.authz import PermissionDenied, permission_role
from cfg.core.config import ProjectConfig, load_config
from cfg.core.diff import format_diff
from cfg.core.engine import BranchingDisabled, Engine, RecordRef, SecretBlocked
from cfg.core.identity import IdentityError, hash_token, resolve_identity


EXIT_OK = 0
EXIT_ARG = 1
EXIT_DIRTY = 2
EXIT_STORAGE = 3
EXIT_FORBIDDEN = 4
EXIT_NOT_FOUND = 5
EXIT_INVARIANT = 6


def main(argv: list[str] | None = None) -> int:
    _load_dotenv(Path(".env"))
    parser = _parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in raw_argv
    raw_argv = [item for item in raw_argv if item != "--json"]
    explicit_ui_port = _has_option(raw_argv, "--port")
    args = parser.parse_args(raw_argv)
    args.json = bool(args.json or json_mode)
    if args.cmd == "ui":
        from cfg.ui.server import run_ui

        return run_ui(
            config_file=args.config_file,
            env=args.env,
            author=args.author,
            host=args.host,
            port=args.port,
            open_browser=not args.no_open,
            allow_port_fallback=not explicit_ui_port,
        )
    if args.cmd == "identity-hash":
        try:
            token = _identity_hash_input(args)
            hashed = hash_token(token)
            _emit({"sha256": hashed, "fingerprint": hashed[7:12]}, json_mode=args.json)
            return EXIT_OK
        except ValueError as exc:
            _emit_error("error", str(exc), args)
            return EXIT_ARG
    try:
        project = load_config(args.config_file)
        engine = _engine(project, args.env, author=args.author)
        result, code = _dispatch(engine, args)
        _emit(result, json_mode=args.json)
        return code
    except AmbiguousConfig as exc:
        _emit_error("bad_config", str(exc), args)
        return EXIT_INVARIANT
    except (StaleHead, StaleLive) as exc:
        _emit_error("changed_outside_cfgit", str(exc), args)
        return EXIT_DIRTY
    except PermissionDenied as exc:
        _emit_error("forbidden", str(exc), args)
        return EXIT_FORBIDDEN
    except IdentityError as exc:
        _emit_error("identity_required", str(exc), args)
        return EXIT_FORBIDDEN
    except AtomicityUnavailable as exc:
        _emit_error("atomicity_unavailable", str(exc), args)
        return EXIT_STORAGE
    except NoSuchConfig as exc:
        _emit_error("not_found", str(exc), args)
        return EXIT_NOT_FOUND
    except (BranchingDisabled, SecretBlocked, ValueError, FileNotFoundError, KeyError) as exc:
        _emit_error("error", str(exc), args)
        return EXIT_ARG
    except Exception as exc:  # pragma: no cover - final CLI guard
        _emit_error("error", str(exc), args)
        return EXIT_STORAGE


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cfg")
    parser.add_argument("--config-file", default=None)
    parser.add_argument("--env", default="dev")
    parser.add_argument("--author", default=None)
    parser.add_argument("--branch", default=None)
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    sub.add_parser("whoami")

    p_branch = sub.add_parser("branch")
    branch_sub = p_branch.add_subparsers(dest="branch_cmd", required=True)
    branch_sub.add_parser("list")
    p_branch_create = branch_sub.add_parser("create")
    p_branch_create.add_argument("name")
    p_branch_create.add_argument("--from", dest="from_branch", default="main")
    p_branch_create.add_argument("-m", "--message", default=None)
    p_branch_delete = branch_sub.add_parser("delete")
    p_branch_delete.add_argument("name")

    p_switch = sub.add_parser("switch")
    p_switch.add_argument("name")

    p_pr = sub.add_parser("pr")
    pr_sub = p_pr.add_subparsers(dest="pr_cmd", required=True)
    p_pr_create = pr_sub.add_parser("create")
    p_pr_create.add_argument("--base", default="main")
    p_pr_create.add_argument("--head", required=True)
    p_pr_create.add_argument("-m", "--message", required=True)
    p_pr_list = pr_sub.add_parser("list")
    p_pr_list.add_argument("--status", default=None)
    p_pr_show = pr_sub.add_parser("show")
    p_pr_show.add_argument("id")
    p_pr_close = pr_sub.add_parser("close")
    p_pr_close.add_argument("id")
    p_pr_merge = pr_sub.add_parser("merge")
    p_pr_merge.add_argument("id")
    p_pr_merge.add_argument("-m", "--message", default=None)

    p_import = sub.add_parser("import")
    p_import.add_argument("record", nargs="?")
    p_import.add_argument("--all", action="store_true")
    p_import.add_argument("-m", "--message", default="initial import")
    p_import.add_argument("--allow-secret", action="store_true")

    p_doctor = sub.add_parser("doctor")
    p_doctor.add_argument("record", nargs="?")
    p_doctor.add_argument("--large-field-bytes", type=int, default=20000,
                          help="flag string fields at or above this size (default 20000)")

    p_status = sub.add_parser("status")
    p_status.add_argument("record", nargs="?")

    p_diff = sub.add_parser("diff")
    p_diff.add_argument("record")
    p_diff.add_argument("a", nargs="?", default="=HEAD")
    p_diff.add_argument("b", nargs="?", default="=live")

    p_impact = sub.add_parser("impact")
    p_impact.add_argument("record")
    p_impact.add_argument("a", nargs="?", default="=HEAD")
    p_impact.add_argument("b", nargs="?", default="=live")
    p_impact.add_argument("--llm", action="store_true")
    p_impact.add_argument("--provider")
    p_impact.add_argument("--model")
    p_impact.add_argument(
        "--against",
        action="append",
        metavar="RECORD",
        help="reason the change against these records only (repeat, or comma-separate). "
        "Without it, the whole system is used.",
    )

    p_commit = sub.add_parser("commit")
    p_commit.add_argument("record", nargs="?")
    p_commit.add_argument("--from", dest="from_file")
    p_commit.add_argument(
        "--bulk-from",
        dest="bulk_from_file",
        help="JSON file containing a list/map of record+doc items to commit as one batch intent",
    )
    p_commit.add_argument("-m", "--message", required=True)
    p_commit.add_argument("--allow-secret", action="store_true")

    p_log = sub.add_parser("log")
    p_log.add_argument("record", nargs="?")
    p_log.add_argument("-n", "--limit", type=int, default=20)

    p_show = sub.add_parser("show")
    p_show.add_argument("record")
    p_show.add_argument("ref")

    p_adopt = sub.add_parser("adopt")
    p_adopt.add_argument("record", nargs="?")
    p_adopt.add_argument("--all", action="store_true")
    p_adopt.add_argument("-m", "--message", required=True)
    p_adopt.add_argument("--allow-secret", action="store_true")

    p_restore = sub.add_parser("restore")
    p_restore.add_argument("record", nargs="?")
    p_restore.add_argument("ref", nargs="?")
    p_restore.add_argument("--as-of", dest="as_of")
    p_restore.add_argument("--tag", dest="tag")
    p_restore.add_argument("--dry-run", action="store_true")
    p_restore.add_argument("-m", "--message", required=True)

    p_tag = sub.add_parser("tag")
    p_tag.add_argument("name")

    sub.add_parser("fsck")

    p_identity_hash = sub.add_parser("identity-hash")
    p_identity_hash.add_argument("token", nargs="?")
    p_identity_hash.add_argument("--stdin", action="store_true")

    p_ui = sub.add_parser("ui")
    p_ui.add_argument("--host", default="127.0.0.1")
    p_ui.add_argument("--port", type=int, default=8765)
    p_ui.add_argument("--no-open", action="store_true")
    return parser


def _has_option(argv: list[str], name: str) -> bool:
    return any(item == name or item.startswith(f"{name}=") for item in argv)


def _dispatch(engine: Engine, args: argparse.Namespace) -> tuple[Any, int]:
    if args.cmd == "init":
        result = engine.init()
        violations = result["invariant_violations"]
        return _plain_init(result), EXIT_INVARIANT if violations else EXIT_OK

    if args.cmd == "whoami":
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
        }, EXIT_OK

    if args.cmd == "branch":
        if args.branch_cmd == "list":
            return engine.branch_list(), EXIT_OK
        if args.branch_cmd == "create":
            return engine.branch_create(args.name, from_branch=args.from_branch, message=args.message), EXIT_OK
        if args.branch_cmd == "delete":
            return engine.branch_delete(args.name), EXIT_OK
        raise ValueError(f"unknown branch command: {args.branch_cmd}")

    if args.cmd == "switch":
        result = engine.branch_current(args.name)
        _write_state(engine.config, engine.env, result["branch"])
        return {**result, "state": "switched"}, EXIT_OK

    if args.cmd == "pr":
        if args.pr_cmd == "create":
            return engine.pr_create(base=args.base, head=args.head, message=args.message), EXIT_OK
        if args.pr_cmd == "list":
            return engine.pr_list(status=args.status), EXIT_OK
        if args.pr_cmd == "show":
            return engine.pr_show(args.id), EXIT_OK
        if args.pr_cmd == "close":
            return engine.pr_close(args.id), EXIT_OK
        if args.pr_cmd == "merge":
            return engine.pr_merge(args.id, message=args.message), EXIT_OK
        raise ValueError(f"unknown PR command: {args.pr_cmd}")

    if args.cmd == "import":
        if not args.all and not args.record:
            raise ValueError("import needs --all or a record")
        result = engine.import_records(
            _parse_record(args.record) if args.record else None,
            message=args.message,
            allow_secret=args.allow_secret,
        )
        return result, EXIT_OK

    if args.cmd == "status":
        rows = engine.status(_parse_record(args.record) if args.record else None)
        code = EXIT_DIRTY if any(r.state == "changed_outside_cfgit" for r in rows) else EXIT_OK
        return rows, code

    if args.cmd == "doctor":
        report = engine.doctor(
            _parse_record(args.record) if args.record else None,
            large_field_bytes=args.large_field_bytes,
        )
        report["text"] = _format_doctor(report)
        code = EXIT_OK if report["ok"] else EXIT_DIRTY
        return report, code

    if args.cmd == "diff":
        if ".." in args.record and ":" not in args.record:
            return engine.branch_diff(args.record), EXIT_OK
        changes = engine.diff(_parse_record(args.record), args.a, args.b)
        return {"changes": changes, "text": format_diff(changes)}, EXIT_OK

    if args.cmd == "impact":
        from cfg.interfaces.actions import impact

        against = None
        if args.against:
            against = [
                part.strip()
                for entry in args.against
                for part in str(entry).split(",")
                if part.strip()
            ] or None
        return impact(
            engine,
            args.record,
            a=args.a,
            b=args.b,
            use_llm=args.llm,
            provider=args.provider,
            model=args.model,
            against=against,
        )

    if args.cmd == "commit":
        branch = _active_branch(engine.config, engine.env, args)
        if args.bulk_from_file:
            if args.record or args.from_file:
                raise ValueError("bulk commit uses --bulk-from without record or --from")
            from cfg.interfaces.actions import bulk_commit_exit_code, parse_bulk_commit_items

            items = parse_bulk_commit_items(_load_json_any(args.bulk_from_file))
            if branch != engine.config.branches.default_branch:
                result = engine.branch_commit_many(
                    branch,
                    items,
                    message=args.message,
                    allow_secret=args.allow_secret,
                )
            else:
                result = engine.commit_many(
                    items,
                    message=args.message,
                    allow_secret=args.allow_secret,
                )
            return result, bulk_commit_exit_code(result)
        if not args.record or not args.from_file:
            raise ValueError("commit needs record and --from, or --bulk-from")
        doc = _load_json_file(args.from_file)
        if branch != engine.config.branches.default_branch:
            result = engine.branch_commit(
                branch,
                _parse_record(args.record),
                doc,
                message=args.message,
                allow_secret=args.allow_secret,
            )
        else:
            result = engine.commit(
                _parse_record(args.record),
                doc,
                message=args.message,
                allow_secret=args.allow_secret,
            )
        code = EXIT_DIRTY if result.get("state") == "changed_outside_cfgit" else EXIT_OK
        return result, code

    if args.cmd == "log":
        branch = _active_branch(engine.config, engine.env, args)
        if branch != engine.config.branches.default_branch:
            if args.record:
                raise ValueError("branch log does not take a record in v1")
            return engine.branch_log(branch, limit=args.limit), EXIT_OK
        if not args.record:
            raise ValueError("log needs a record on main, or switch/select a branch")
        return engine.log(_parse_record(args.record), limit=args.limit), EXIT_OK

    if args.cmd == "show":
        return engine.resolve_ref(_parse_record(args.record), args.ref), EXIT_OK

    if args.cmd == "adopt":
        if args.all:
            results = []
            for row in engine.status():
                if row.state == "changed_outside_cfgit":
                    result = engine.adopt(
                        RecordRef(row.collection, row.record_id),
                        message=args.message,
                        allow_secret=args.allow_secret,
                    )
                    results.append({"collection": row.collection, "record_id": row.record_id, **result})
            return results, EXIT_OK
        if not args.record:
            raise ValueError("adopt needs --all or a record")
        return engine.adopt(
            _parse_record(args.record),
            message=args.message,
            allow_secret=args.allow_secret,
        ), EXIT_OK

    if args.cmd == "restore":
        if args.as_of and args.tag:
            raise ValueError("restore accepts only one of --as-of or --tag")
        if args.as_of:
            if args.record or args.ref:
                raise ValueError("restore --as-of restores all records; omit record and ref")
            result = engine.restore_system_as_of(
                _parse_when(args.as_of),
                message=args.message,
                dry_run=args.dry_run,
            )
            code = _restore_exit_code(result)
            return result, code
        if args.tag:
            if args.record or args.ref:
                raise ValueError("restore --tag restores all records; omit record and ref")
            result = engine.restore_system_tag(args.tag, message=args.message, dry_run=args.dry_run)
            code = _restore_exit_code(result)
            return result, code
        if args.dry_run:
            raise ValueError("--dry-run is only supported with system restore")
        if not args.record or not args.ref:
            raise ValueError("restore needs record and ref, or --as-of/--tag")
        result = engine.restore(_parse_record(args.record), args.ref, message=args.message)
        code = EXIT_DIRTY if result.get("state") == "changed_outside_cfgit" else EXIT_OK
        return result, code

    if args.cmd == "tag":
        return engine.tag(args.name), EXIT_OK

    if args.cmd == "fsck":
        return {
            "invariant_violations": engine.adapter.check_runtime_invariant(),
            "atomicity": engine.adapter.check_atomicity_scope(),
            "reconcile": engine.adapter.reconcile(),
        }, EXIT_OK

    raise ValueError(f"unknown command: {args.cmd}")


def _engine(project: ProjectConfig, env_name: str, *, author: str | None) -> Engine:
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


def _active_branch(project: ProjectConfig, env: str, args: argparse.Namespace) -> str:
    if args.branch:
        return str(args.branch)
    if not project.branches.enabled:
        return project.branches.default_branch
    state = _read_state(project)
    if state.get("env") == env and state.get("branch"):
        return str(state["branch"])
    return project.branches.default_branch


def _state_path(project: ProjectConfig) -> Path:
    return project.path.parent / ".cfgit" / "state.json"


def _read_state(project: ProjectConfig) -> dict[str, Any]:
    path = _state_path(project)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(project: ProjectConfig, env: str, branch: str) -> None:
    path = _state_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": env, "branch": branch}, indent=2, sort_keys=True), encoding="utf-8")


def _parse_record(raw: str | None) -> RecordRef:
    if not raw or ":" not in raw:
        raise ValueError("record must be collection:id, for example agent_configs:agent_planner")
    collection, record_id = raw.split(":", 1)
    if not collection or not record_id:
        raise ValueError("record must be collection:id")
    return RecordRef(collection, record_id)


def _load_json_file(path: str) -> dict[str, Any]:
    data = _load_json_any(path)
    if not isinstance(data, dict):
        raise ValueError("--from file must contain one JSON object")
    return data


def _load_json_any(path: str) -> Any:
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def _parse_when(raw: str) -> datetime:
    value = raw.strip()
    date_only = len(value) == 10 and value[4] == "-" and value[7] == "-"
    if date_only:
        dt = datetime.combine(datetime.fromisoformat(value).date(), time.max)
    else:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_doctor(report: dict[str, Any]) -> str:
    lines: list[str] = []
    n = report.get("scanned", 0)
    if report.get("ok"):
        lines.append(f"doctor: {n} live record(s) scanned — no blockers. Safe to import.")
        return "\n".join(lines)
    sb = report.get("secret_blocks", [])
    lf = report.get("large_fields", [])
    ki = report.get("key_issues", [])
    lines.append(
        f"doctor: {n} live record(s) scanned — {len(sb)} secret block(s), "
        f"{len(lf)} large field(s), {len(ki)} key issue(s)."
    )
    if ki:
        lines.append("")
        lines.append("Key / live-rule issues (fix id_field or live_when before import):")
        for issue in ki:
            lines.append(f"  {issue}")
    if sb:
        has_value = any(g["kind"] == "value" for g in sb)
        lines.append("")
        lines.append("Secret-deny matches (would refuse import). Two ways to resolve each:")
        lines.append("  - secret_fields = strip the value from history (use when the field is NOT")
        lines.append("    needed in the record, or is schema structure).")
        lines.append("  - import/commit --allow-secret = STORE the real value in history (use when")
        lines.append("    the key must stay in the record so restore writes it back; value is then")
        lines.append("    in cfgit history in plaintext).")
        for g in sb:
            tag = "real value" if g["kind"] == "value" else "field name"
            lines.append(f"  {g['collection']}: {g['path']}  [{tag}: {g['pattern']}]  "
                         f"x{g['count']} (e.g. {g['example']})")
        if has_value:
            lines.append("  ! at least one match is a REAL secret VALUE — if that key must live in")
            lines.append("    the record, keep it OUT of secret_fields and import with --allow-secret.")
    if lf:
        lines.append("")
        kb = report.get("large_field_bytes", 0) // 1000
        lines.append(f"Large fields (>= {kb}KB; consider ignore_fields to keep diffs readable):")
        for g in lf:
            lines.append(f"  {g['collection']}: {g['path']}  up to {g['max_bytes']//1000}KB  x{g['count']}")
    sug = report.get("suggestions", {})
    if sug:
        lines.append("")
        lines.append("Paste-ready fixes (per collection in .cfg.toml):")
        for coll in sorted(sug):
            entry = sug[coll]
            lines.append(f"  # [[collection]] name = \"{coll}\"")
            if entry.get("secret_fields"):
                joined = ", ".join(f'"{p}"' for p in sorted(set(entry["secret_fields"])))
                lines.append(f"  secret_fields = [{joined}]")
            if entry.get("ignore_fields"):
                joined = ", ".join(f'"{p}"' for p in sorted(set(entry["ignore_fields"])))
                lines.append(f"  ignore_fields = [{joined}]")
    return "\n".join(lines)


def _emit(value: Any, *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(_to_json(value), indent=2, sort_keys=True))
        return
    if isinstance(value, list):
        for item in value:
            print(_format_item(item))
        return
    if isinstance(value, dict) and "text" in value and ("changes" in value or "secret_blocks" in value):
        print(value["text"])
        return
    print(_format_item(value))


def _emit_error(status: str, message: str, args: argparse.Namespace) -> None:
    if getattr(args, "json", False):
        print(json.dumps({"status": status, "message": message}, indent=2), file=sys.stderr)
    else:
        print(f"{status}: {message}", file=sys.stderr)


def _format_item(value: Any) -> str:
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        if {"collection", "record_id", "state"} <= set(value):
            return f"{value['collection']}:{value['record_id']} {value['state']}"
        if {"collection", "record_id", "seq", "oid"} <= set(value):
            return f"{value['collection']}:{value['record_id']} @{value['seq']} {str(value['oid'])[:12]}"
        return json.dumps(_to_json(value), sort_keys=True)
    return str(value)


def _to_json(value: Any) -> Any:
    if is_dataclass(value):
        return _to_json(asdict(value))
    if isinstance(value, dict):
        return {str(k): _to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _plain_init(result: dict[str, Any]) -> dict[str, Any]:
    atomic = result["atomic"]
    return {
        "atomic": asdict(atomic) if is_dataclass(atomic) else atomic,
        "invariant_violations": result["invariant_violations"],
        "branches": result.get("branches"),
    }


def _restore_exit_code(result: dict[str, Any]) -> int:
    if result.get("state") == "blocked":
        return EXIT_DIRTY
    if result.get("state") == "partial":
        return EXIT_STORAGE
    return EXIT_OK


def _identity_hash_input(args: argparse.Namespace) -> str:
    if args.stdin:
        token = sys.stdin.read().strip()
    else:
        token = args.token or ""
    if not token:
        raise ValueError("identity-hash needs a token argument or --stdin")
    return token


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


if __name__ == "__main__":
    raise SystemExit(main())
