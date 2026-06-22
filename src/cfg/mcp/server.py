# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""MCP server for cfgit."""
from __future__ import annotations

from typing import Any

from cfg.core.identity import MIN_TOKEN_LENGTH, hash_token
from cfg.interfaces import actions
from cfg.interfaces.actions import ActionContext

try:  # pragma: no cover - exercised when cfg-vcs[mcp] is installed
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # pragma: no cover
    FastMCP = None  # type: ignore[assignment]


def _mcp() -> Any:
    if FastMCP is None:
        raise ModuleNotFoundError("install cfg-vcs[mcp] to run the cfgit MCP server")
    return FastMCP("cfgit")


mcp = _mcp()


@mcp.tool()
def cfg_whoami(config_file: str | None = None, env: str = "dev", author: str | None = None) -> dict[str, Any]:
    return _call("whoami", {}, config_file=config_file, env=env, author=author)


@mcp.tool()
def cfg_init(config_file: str | None = None, env: str = "dev", author: str | None = None) -> dict[str, Any]:
    return _call("init", {}, config_file=config_file, env=env, author=author)


@mcp.tool()
def cfg_status(
    record: str | None = None,
    config_file: str | None = None,
    env: str = "dev",
    author: str | None = None,
) -> dict[str, Any]:
    return _call("status", {"record": record}, config_file=config_file, env=env, author=author)


@mcp.tool()
def cfg_doctor(
    record: str | None = None,
    large_field_bytes: int = 20000,
    config_file: str | None = None,
    env: str = "dev",
    author: str | None = None,
) -> dict[str, Any]:
    """Read-only preflight before import/commit: reports secret-deny matches,
    oversized fields, and key issues per collection, with paste-ready secret_fields
    / ignore_fields snippets. Writes nothing."""
    return _call(
        "doctor",
        {"record": record, "large_field_bytes": large_field_bytes},
        config_file=config_file,
        env=env,
        author=author,
    )


@mcp.tool()
def cfg_import(
    record: str | None = None,
    all_records: bool = False,
    message: str = "initial import",
    allow_secret: bool = False,
    config_file: str | None = None,
    env: str = "dev",
    author: str | None = None,
) -> dict[str, Any]:
    return _call(
        "import",
        {
            "record": record,
            "all_records": all_records,
            "message": message,
            "allow_secret": allow_secret,
        },
        config_file=config_file,
        env=env,
        author=author,
    )


@mcp.tool()
def cfg_diff(
    record: str,
    a: str = "=HEAD",
    b: str = "=live",
    config_file: str | None = None,
    env: str = "dev",
    author: str | None = None,
) -> dict[str, Any]:
    return _call(
        "diff",
        {"record": record, "a": a, "b": b},
        config_file=config_file,
        env=env,
        author=author,
    )


@mcp.tool()
def cfg_impact(
    record: str,
    a: str = "=HEAD",
    b: str = "=live",
    use_llm: bool = False,
    provider: str | None = None,
    model: str | None = None,
    against: list[str] | str | None = None,
    config_file: str | None = None,
    env: str = "dev",
    author: str | None = None,
) -> dict[str, Any]:
    return _call(
        "impact",
        {
            "record": record,
            "a": a,
            "b": b,
            "use_llm": use_llm,
            "provider": provider,
            "model": model,
            "against": against,
        },
        config_file=config_file,
        env=env,
        author=author,
    )


@mcp.tool()
def cfg_commit(
    record: str,
    doc_json: str,
    message: str,
    allow_secret: bool = False,
    config_file: str | None = None,
    env: str = "dev",
    author: str | None = None,
) -> dict[str, Any]:
    return _call(
        "commit",
        {"record": record, "doc": doc_json, "message": message, "allow_secret": allow_secret},
        config_file=config_file,
        env=env,
        author=author,
    )


@mcp.tool()
def cfg_bulk_commit(
    items: list[dict[str, Any]] | dict[str, Any] | str,
    message: str,
    allow_secret: bool = False,
    config_file: str | None = None,
    env: str = "dev",
    author: str | None = None,
) -> dict[str, Any]:
    """Commit multiple full documents as one batch intent.

    `items` may be either:
    [{"record":"collection:id","doc":{...}}, ...]
    {"collection:id": {...}, ...}, or a JSON string in either shape.
    """
    return _call(
        "bulk_commit",
        {"items": items, "message": message, "allow_secret": allow_secret},
        config_file=config_file,
        env=env,
        author=author,
    )


@mcp.tool()
def cfg_log(
    record: str,
    limit: int = 20,
    config_file: str | None = None,
    env: str = "dev",
    author: str | None = None,
) -> dict[str, Any]:
    return _call(
        "log",
        {"record": record, "limit": limit},
        config_file=config_file,
        env=env,
        author=author,
    )


@mcp.tool()
def cfg_show(
    record: str,
    ref: str = "HEAD",
    config_file: str | None = None,
    env: str = "dev",
    author: str | None = None,
) -> dict[str, Any]:
    return _call(
        "show",
        {"record": record, "ref": ref},
        config_file=config_file,
        env=env,
        author=author,
    )


@mcp.tool()
def cfg_adopt(
    record: str | None = None,
    all_records: bool = False,
    message: str = "adopt drift",
    allow_secret: bool = False,
    config_file: str | None = None,
    env: str = "dev",
    author: str | None = None,
) -> dict[str, Any]:
    return _call(
        "adopt",
        {
            "record": record,
            "all_records": all_records,
            "message": message,
            "allow_secret": allow_secret,
        },
        config_file=config_file,
        env=env,
        author=author,
    )


@mcp.tool()
def cfg_restore(
    record: str | None = None,
    ref: str | None = None,
    as_of: str | None = None,
    tag: str | None = None,
    dry_run: bool = False,
    message: str = "restore",
    config_file: str | None = None,
    env: str = "dev",
    author: str | None = None,
) -> dict[str, Any]:
    return _call(
        "restore",
        {
            "record": record,
            "ref": ref,
            "as_of": as_of,
            "tag": tag,
            "dry_run": dry_run,
            "message": message,
        },
        config_file=config_file,
        env=env,
        author=author,
    )


@mcp.tool()
def cfg_tag(
    name: str,
    config_file: str | None = None,
    env: str = "dev",
    author: str | None = None,
) -> dict[str, Any]:
    return _call("tag", {"name": name}, config_file=config_file, env=env, author=author)


@mcp.tool()
def cfg_fsck(config_file: str | None = None, env: str = "dev", author: str | None = None) -> dict[str, Any]:
    return _call("fsck", {}, config_file=config_file, env=env, author=author)


@mcp.tool()
def cfg_identity_hash(token: str) -> dict[str, Any]:
    """Hash a private identity token for .cfg.toml setup.

    Prefer the local CLI for real human secrets:
    `printf '%s' '<token>' | cfg identity-hash --stdin`.
    """
    raw = token.strip()
    if len(raw) < MIN_TOKEN_LENGTH:
        return {
            "status": "error",
            "code": actions.EXIT_ARG,
            "message": f"identity token must be at least {MIN_TOKEN_LENGTH} characters",
            "data": None,
        }
    hashed = hash_token(raw)
    return {
        "status": "ok",
        "code": actions.EXIT_OK,
        "message": "",
        "data": {
            "sha256": hashed,
            "fingerprint": hashed[7:12],
            "warning": "the raw token passed to this MCP tool may be visible to the MCP client; prefer local CLI for real secrets",
        },
    }


def _call(
    name: str,
    payload: dict[str, Any],
    *,
    config_file: str | None,
    env: str,
    author: str | None,
) -> dict[str, Any]:
    return actions.envelope(_call_inner, name, payload, config_file, env, author)


def _call_inner(
    name: str,
    payload: dict[str, Any],
    config_file: str | None,
    env: str,
    author: str | None,
) -> tuple[Any, int]:
    engine = actions.make_engine(ActionContext(config_file=config_file, env=env, author=author))
    return actions.run_named_action(name, engine, payload)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
