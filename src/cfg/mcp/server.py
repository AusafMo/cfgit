# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""MCP server for cfgit."""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from cfg.core.identity import MIN_TOKEN_LENGTH, hash_token
from cfg.interfaces import actions
from cfg.interfaces.actions import ActionContext

try:  # pragma: no cover - exercised when cfgit[mcp] is installed
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # pragma: no cover
    FastMCP = None  # type: ignore[assignment]


def _mcp_missing_message() -> str:
    """Actionable install hint. `cfg-mcp` is commonly launched by an MCP client (e.g. via a
    one-click deeplink) against a cfgit that was installed WITHOUT the mcp extra. For a pipx
    install, `pip install cfgit[mcp]` is the wrong fix (it won't touch the pipx venv), so lead
    with `pipx inject` when we detect we're running from one."""
    import sys

    base = (
        "The cfgit MCP server needs the 'mcp' package, which is not installed in this "
        "environment. cfgit was installed without the [mcp] extra."
    )
    if "/pipx/venvs/" in sys.prefix or "/pipx/venvs/" in sys.executable:
        return (
            f"{base}\nFix (pipx): pipx inject cfgit 'mcp>=1.0,<2'\n"
            "or reinstall with the extra: pipx install 'cfgit[mcp]' --force"
        )
    return f"{base}\nFix: pip install 'cfgit[mcp]'  (mcp 2.0 is not yet supported; the extra pins mcp<2)"


def _mcp() -> Any:
    if FastMCP is None:
        raise ModuleNotFoundError(_mcp_missing_message())
    return FastMCP("cfgit")


mcp = _mcp()


@mcp.tool()
def cfg_whoami(
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """Report the resolved identity, env, database, permission role, and identity mode."""
    return _call("whoami", {}, config_file=config_file, env=env, author=author)


@mcp.tool()
def cfg_init(
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """Initialize cfgit history/heads (and branch refs if enabled) for the configured env."""
    return _call("init", {}, config_file=config_file, env=env, author=author)


@mcp.tool()
def cfg_status(
    record: Annotated[str | None, Field(description="Optional collection:id to scope to one record; omit for all tracked records.")] = None,
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """Report drift: which live records changed outside cfgit vs their recorded head."""
    return _call("status", {"record": record}, config_file=config_file, env=env, author=author)


@mcp.tool()
def cfg_doctor(
    record: Annotated[str | None, Field(description="Optional collection:id to scope to one record; omit for all tracked records.")] = None,
    large_field_bytes: Annotated[int, Field(description="Byte threshold above which a field is flagged as oversized.")] = 20000,
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
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
    record: Annotated[str | None, Field(description="Record address as collection:id, e.g. agent_configs:refund_resolution.")] = None,
    all_records: Annotated[bool, Field(description="Import every configured collection instead of a single record.")] = False,
    from_export: Annotated[dict[str, Any] | list[dict[str, Any]] | None, Field(description="A cfg_export artifact (dict or list) to restore documents from, via the drift-guarded bulk path.")] = None,
    dry_run: Annotated[bool, Field(description="Preview the change and return the would-be result without writing anything.")] = False,
    message: Annotated[str, Field(description="Human-readable commit/operation message.")] = "initial import",
    allow_secret: Annotated[bool, Field(description="Allow committing values that match secret-deny rules (logged). Off by default.")] = False,
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """Start tracking live records (record or all_records), or restore documents from a
    `cfg_export` artifact via from_export (writes them back through the drift-guarded bulk-commit
    path; set dry_run=true to preview)."""
    return _call(
        "import",
        {
            "record": record,
            "all_records": all_records,
            "export": from_export,
            "dry_run": dry_run,
            "message": message,
            "allow_secret": allow_secret,
        },
        config_file=config_file,
        env=env,
        author=author,
    )


@mcp.tool()
def cfg_export(
    record: Annotated[str | None, Field(description="Optional collection:id to export one record; omit to export every configured collection.")] = None,
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """Read-only snapshot of live documents into a portable, re-importable artifact. Pass a
    record for one, or omit it to export every configured collection. Each item is stamped with
    its cfgit head seq/oid. Feed the result back to cfg_import(from_export=...) to restore."""
    return _call("export", {"record": record}, config_file=config_file, env=env, author=author)


@mcp.tool()
def cfg_diff(
    record: Annotated[str, Field(description="Record address as collection:id, e.g. agent_configs:refund_resolution.")],
    a: Annotated[str, Field(description="Left ref (default =HEAD). Refs: =HEAD, =live, @<seq>, or an oid prefix.")] = "=HEAD",
    b: Annotated[str, Field(description="Right ref (default =live). Refs: =HEAD, =live, @<seq>, or an oid prefix.")] = "=live",
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """Show the field-level diff of a record between two refs."""
    return _call(
        "diff",
        {"record": record, "a": a, "b": b},
        config_file=config_file,
        env=env,
        author=author,
    )


@mcp.tool()
def cfg_impact(
    record: Annotated[str, Field(description="Record address as collection:id, e.g. agent_configs:refund_resolution.")],
    a: Annotated[str, Field(description="Left ref (default =HEAD). Refs: =HEAD, =live, @<seq>, or an oid prefix.")] = "=HEAD",
    b: Annotated[str, Field(description="Right ref (default =live). Refs: =HEAD, =live, @<seq>, or an oid prefix.")] = "=live",
    use_llm: Annotated[bool, Field(description="Add LLM narration of the change (requires the cfgit-impact plugin + provider key).")] = False,
    provider: Annotated[str | None, Field(description="Override the AI provider (claude, openai, gemini); defaults to [connections].ai_provider.")] = None,
    model: Annotated[str | None, Field(description="Override the provider model id.")] = None,
    against: Annotated[list[str] | str | None, Field(description="Scope narration to these related records (collection:id, repeatable or comma-separated) instead of the whole system.")] = None,
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """Analyze the system impact of a record change; add use_llm for provider-backed narration."""
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
    record: Annotated[str, Field(description="Record address as collection:id, e.g. agent_configs:refund_resolution.")],
    doc_json: Annotated[str, Field(description="The full document to commit, as a JSON object or JSON string.")],
    message: Annotated[str, Field(description="Human-readable commit/operation message.")] = "",
    allow_secret: Annotated[bool, Field(description="Allow committing values that match secret-deny rules (logged). Off by default.")] = False,
    branch: Annotated[str | None, Field(description="Commit to this branch instead of main (branch commit; does not mutate runtime).")] = None,
    dry_run: Annotated[bool, Field(description="Preview the change and return the would-be result without writing anything.")] = False,
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """Commit a full document. Set dry_run=true to preview the field-level delta vs live and
    get back state="would_commit" without writing (main-branch, single record)."""
    return _call(
        "commit",
        {
            "record": record,
            "doc": doc_json,
            "message": message,
            "allow_secret": allow_secret,
            "branch": branch,
            "dry_run": dry_run,
        },
        config_file=config_file,
        env=env,
        author=author,
    )


@mcp.tool()
def cfg_bulk_commit(
    items: Annotated[
        list[dict[str, Any]] | dict[str, Any] | str,
        Field(description="Records to commit as one batch: a list of {record, doc}, a {collection:id: doc} map, or a JSON string of either."),
    ],
    message: Annotated[str, Field(description="Human-readable commit/operation message.")] = "",
    allow_secret: Annotated[bool, Field(description="Allow committing values that match secret-deny rules (logged). Off by default.")] = False,
    branch: Annotated[str | None, Field(description="Commit to this branch instead of main.")] = None,
    dry_run: Annotated[bool, Field(description="Preview the change and return the would-be result without writing anything.")] = False,
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """Commit multiple full documents as one batch intent.

    `items` may be either:
    [{"record":"collection:id","doc":{...}}, ...]
    {"collection:id": {...}, ...}, or a JSON string in either shape.

    Set dry_run=true to preview every record's delta (would_commit / noop / changed_outside_cfgit)
    without writing — recommended before a collection-scale replace.
    """
    return _call(
        "bulk_commit",
        {"items": items, "message": message, "allow_secret": allow_secret, "branch": branch, "dry_run": dry_run},
        config_file=config_file,
        env=env,
        author=author,
    )


@mcp.tool()
def cfg_set(
    record: Annotated[str, Field(description="Record address as collection:id, e.g. agent_configs:refund_resolution.")],
    assignments: Annotated[
        dict[str, Any] | list[dict[str, Any]] | str,
        Field(
            description=(
                "Scalar edits as a dotted-path→value map (e.g. {\"enabled\": true, \"retry.max\": 3}),"
                " a list of such maps, or a JSON string."
            )
        ),
    ],
    message: Annotated[str, Field(description="Human-readable commit/operation message.")] = "",
    allow_secret: Annotated[bool, Field(description="Allow committing values that match secret-deny rules (logged). Off by default.")] = False,
    dry_run: Annotated[bool, Field(description="Preview the change and return the would-be result without writing anything.")] = False,
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """Edit scalar fields of a record in place. `assignments` is a mapping of dotted-path →
    value (e.g. {"enabled": true, "retry.max": 3}). Routes through the drift-guarded commit
    path — never a raw write. Set dry_run=true to preview without writing."""
    return _call(
        "set",
        {
            "record": record,
            "assignments": assignments,
            "message": message,
            "allow_secret": allow_secret,
            "dry_run": dry_run,
        },
        config_file=config_file,
        env=env,
        author=author,
    )


@mcp.tool()
def cfg_log(
    record: Annotated[str, Field(description="Record address as collection:id, e.g. agent_configs:refund_resolution.")],
    limit: Annotated[int, Field(description="Max number of history entries to return.")] = 20,
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """List the commit history of a record, newest first."""
    return _call(
        "log",
        {"record": record, "limit": limit},
        config_file=config_file,
        env=env,
        author=author,
    )


@mcp.tool()
def cfg_recent_history(
    limit: Annotated[int, Field(description="Max number of recent commits to return across all records.")] = 50,
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """Return recent cfgit history entries across all configured records."""
    return _call(
        "recent_history",
        {"limit": limit},
        config_file=config_file,
        env=env,
        author=author,
    )


@mcp.tool()
def cfg_show(
    record: Annotated[str, Field(description="Record address as collection:id, e.g. agent_configs:refund_resolution.")],
    ref: Annotated[str, Field(description="Which version to show: =HEAD, =live, @<seq>, or an oid prefix.")] = "HEAD",
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """Show a record's stored document at a given ref."""
    return _call(
        "show",
        {"record": record, "ref": ref},
        config_file=config_file,
        env=env,
        author=author,
    )


@mcp.tool()
def cfg_adopt(
    record: Annotated[str | None, Field(description="Record address as collection:id, e.g. agent_configs:refund_resolution.")] = None,
    all_records: Annotated[bool, Field(description="Adopt drift for every tracked record instead of a single one.")] = False,
    message: Annotated[str, Field(description="Human-readable commit/operation message.")] = "adopt drift",
    allow_secret: Annotated[bool, Field(description="Allow committing values that match secret-deny rules (logged). Off by default.")] = False,
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """Fold an out-of-band live change into cfgit history (record or all_records)."""
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
    record: Annotated[
        str | None,
        Field(description="Optional collection:id to restore one record; omit for a system-wide restore."),
    ] = None,
    ref: Annotated[
        str | None,
        Field(description="Restore this record to this ref (=HEAD, @<seq>, or an oid prefix)."),
    ] = None,
    as_of: Annotated[
        str | None,
        Field(description="Restore the system to its state at this ISO-8601 timestamp."),
    ] = None,
    tag: Annotated[
        str | None,
        Field(description="Restore the system to the version labelled with this tag."),
    ] = None,
    dry_run: Annotated[
        bool,
        Field(description="Preview the change and return the would-be result without writing anything."),
    ] = False,
    message: Annotated[
        str,
        Field(description="Human-readable commit/operation message."),
    ] = "restore",
    config_file: Annotated[
        str | None,
        Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd."),
    ] = None,
    env: Annotated[
        str,
        Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database."),
    ] = "dev",
    author: Annotated[
        str | None,
        Field(
            description="Author attribution hint. Ignored/rejected in authenticated/enforced identity"
            " modes where a verified identity is required."
        ),
    ] = None,
) -> dict[str, Any]:
    """Restore a record (or the whole system) to a prior version by ref, tag, or point in time."""
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
    name: Annotated[str, Field(description="The tag name to apply.")],
    config_file: Annotated[
        str | None,
        Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd."),
    ] = None,
    env: Annotated[
        str,
        Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database."),
    ] = "dev",
    author: Annotated[
        str | None,
        Field(
            description="Author attribution hint. Ignored/rejected in authenticated/enforced identity"
            " modes where a verified identity is required."
        ),
    ] = None,
) -> dict[str, Any]:
    """Label the current system state with a named tag for later restore."""
    return _call("tag", {"name": name}, config_file=config_file, env=env, author=author)


@mcp.tool()
def cfg_branch_list(
    config_file: Annotated[
        str | None,
        Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd."),
    ] = None,
    env: Annotated[
        str,
        Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database."),
    ] = "dev",
    author: Annotated[
        str | None,
        Field(
            description="Author attribution hint. Ignored/rejected in authenticated/enforced identity"
            " modes where a verified identity is required."
        ),
    ] = None,
) -> dict[str, Any]:
    """List cfgit branches (requires [branches] enabled)."""
    return _call("branch_list", {}, config_file=config_file, env=env, author=author)


@mcp.tool()
def cfg_branch_create(
    name: Annotated[str, Field(description="Name of the new branch.")],
    from_branch: Annotated[
        str,
        Field(description="Branch to fork from (defaults to the default branch, usually main)."),
    ] = "main",
    message: Annotated[
        str | None,
        Field(description="Human-readable commit/operation message."),
    ] = None,
    config_file: Annotated[
        str | None,
        Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd."),
    ] = None,
    env: Annotated[
        str,
        Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database."),
    ] = "dev",
    author: Annotated[
        str | None,
        Field(
            description="Author attribution hint. Ignored/rejected in authenticated/enforced identity"
            " modes where a verified identity is required."
        ),
    ] = None,
) -> dict[str, Any]:
    """Create a draft branch (writes branch metadata only; does not mutate runtime)."""
    return _call(
        "branch_create",
        {"name": name, "from_branch": from_branch, "message": message},
        config_file=config_file,
        env=env,
        author=author,
    )


@mcp.tool()
def cfg_branch_delete(
    name: Annotated[str, Field(description="Name of the branch to delete.")],
    config_file: Annotated[
        str | None,
        Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd."),
    ] = None,
    env: Annotated[
        str,
        Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database."),
    ] = "dev",
    author: Annotated[
        str | None,
        Field(
            description="Author attribution hint. Ignored/rejected in authenticated/enforced identity"
            " modes where a verified identity is required."
        ),
    ] = None,
) -> dict[str, Any]:
    """Delete a draft branch and its branch commits."""
    return _call("branch_delete", {"name": name}, config_file=config_file, env=env, author=author)


@mcp.tool()
def cfg_branch_diff(
    range: Annotated[str, Field(description="Branch comparison range, e.g. 'main..feature' or a single branch name to compare against the default branch.")],
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """Compare a branch against another branch (or main) at the record level."""
    return _call("branch_diff", {"range": range}, config_file=config_file, env=env, author=author)


@mcp.tool()
def cfg_branch_log(
    branch: Annotated[str, Field(description="Branch name to show history for.")],
    limit: Annotated[int, Field(description="Max number of commits to return.")] = 20,
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """List the commit history of a branch."""
    return _call("branch_log", {"branch": branch, "limit": limit}, config_file=config_file, env=env, author=author)


@mcp.tool()
def cfg_pr_create(
    head: Annotated[str, Field(description="The source (head) branch containing the draft commits.")],
    message: Annotated[str, Field(description="Human-readable commit/operation message.")],
    base: Annotated[str, Field(description="The target (base) branch to merge into (defaults to the default branch, usually main).")] = "main",
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """Open a cfgit pull request from a head branch into a base branch."""
    return _call(
        "pr_create",
        {"base": base, "head": head, "message": message},
        config_file=config_file,
        env=env,
        author=author,
    )


@mcp.tool()
def cfg_pr_list(
    status: Annotated[str | None, Field(description="Filter by PR status, e.g. 'open' or 'closed'; omit for all.")] = None,
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """List cfgit pull requests."""
    return _call("pr_list", {"status": status}, config_file=config_file, env=env, author=author)


@mcp.tool()
def cfg_pr_show(
    id: Annotated[str, Field(description="The pull request id (e.g. pr_abc123).")],
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """Show a cfgit pull request: its branches, records, and per-record diff."""
    return _call("pr_show", {"id": id}, config_file=config_file, env=env, author=author)


@mcp.tool()
def cfg_pr_close(
    id: Annotated[str, Field(description="The pull request id (e.g. pr_abc123).")],
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """Close a cfgit pull request without merging."""
    return _call("pr_close", {"id": id}, config_file=config_file, env=env, author=author)


@mcp.tool()
def cfg_pr_merge(
    id: Annotated[str, Field(description="The pull request id to merge.")],
    message: Annotated[str | None, Field(description="Human-readable commit/operation message.")] = None,
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """Merge an open PR through the shared action layer.

    Multi-record PRs use the adapter batch-atomic merge path. If that guarantee
    is unavailable, the envelope returns `atomicity_unavailable` and runtime is
    left unchanged.
    """
    return _call("pr_merge", {"id": id, "message": message}, config_file=config_file, env=env, author=author)


@mcp.tool()
def cfg_fsck(
    config_file: Annotated[str | None, Field(description="Path to .cfg.toml. Omit to auto-discover by walking up from the cwd.")] = None,
    env: Annotated[str, Field(description="Environment name from .cfg.toml (e.g. dev, prod). Selects the target database.")] = "dev",
    author: Annotated[str | None, Field(description="Author attribution hint. Ignored/rejected in authenticated/enforced identity modes where a verified identity is required.")] = None,
) -> dict[str, Any]:
    """Check cfgit history/heads integrity and report any inconsistencies."""
    return _call("fsck", {}, config_file=config_file, env=env, author=author)


@mcp.tool()
def cfg_identity_hash(
    token: Annotated[
        str,
        Field(
            description="Raw identity token string to hash into a sha256 identity;"
            f" must be at least {MIN_TOKEN_LENGTH} characters."
        ),
    ],
) -> dict[str, Any]:
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


@mcp.tool()
def cfg_check_update(
    snooze_days: Annotated[
        int | None,
        Field(description="Snooze the update reminder for this many days (e.g. 30). Omit to check now."),
    ] = None,
) -> dict[str, Any]:
    """Check PyPI for a newer cfgit release. Call this at the start of a cfgit session; if
    `data.update_available` is true and not `data.snoozed`, tell the user the new version and
    offer to upgrade (`pip install -U cfgit`) or to snooze. NEVER upgrade for them.

    Pass `snooze_days` (e.g. 30) to record a "don't ask again for N days" snooze when the user
    asks to be reminded later. Best-effort and fail-silent; honors CFGIT_NO_UPDATE_CHECK.
    """
    from cfg import update

    if snooze_days is not None:
        return {"status": "ok", "code": actions.EXIT_OK, "message": "", "data": update.snooze(snooze_days)}
    return {"status": "ok", "code": actions.EXIT_OK, "message": "", "data": update.check(force=True).to_json()}


def _call(
    name: str,
    payload: dict[str, Any],
    *,
    config_file: str | None,
    env: str,
    author: str | None,
) -> dict[str, Any]:
    record = payload.get("record") if isinstance(payload, dict) else None
    return actions.envelope(_call_inner, name, payload, config_file, env, author, record=record)


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
