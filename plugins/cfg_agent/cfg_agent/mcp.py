from __future__ import annotations

import json
from typing import Any

from cfg.interfaces.actions import ActionContext, make_engine
from cfg_agent.actions import AgentActions
from cfg_agent.config import cached_agent_coordinator

try:  # pragma: no cover - exercised when cfgit[mcp] is installed
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # pragma: no cover
    FastMCP = None  # type: ignore[assignment]


def _mcp() -> Any:
    if FastMCP is None:
        raise ModuleNotFoundError("install cfgit[mcp] to run the cfgit-agent MCP server")
    return FastMCP("cfgit-agent")


mcp = _mcp()
actions = AgentActions()


def _actions(config_file: str | None = None, env: str = "dev") -> AgentActions:
    if config_file is None:
        return actions
    return AgentActions(cached_agent_coordinator(config_file, env))


@mcp.tool()
def cfg_agent_start_session(
    task: str,
    agent_id: str = "agent",
    agent_kind: str = "custom",
    actor: str | None = None,
    tool_client: str = "mcp",
    metadata: dict[str, Any] | None = None,
    config_file: str | None = None,
    env: str = "dev",
) -> dict[str, Any]:
    return _actions(config_file, env).start_session(
        {
            "task": task,
            "agent_id": agent_id,
            "agent_kind": agent_kind,
            "actor": actor,
            "tool_client": tool_client,
            "metadata": metadata or {},
        }
    )


@mcp.tool()
def cfg_agent_heartbeat(session_id: str, config_file: str | None = None, env: str = "dev") -> dict[str, Any]:
    return _actions(config_file, env).heartbeat({"session_id": session_id})


@mcp.tool()
def cfg_agent_end_session(
    session_id: str,
    status: str = "completed",
    summary: str | None = None,
    config_file: str | None = None,
    env: str = "dev",
) -> dict[str, Any]:
    return _actions(config_file, env).end_session({"session_id": session_id, "status": status, "summary": summary})


@mcp.tool()
def cfg_agent_claim(
    session_id: str,
    resource: str,
    ttl_seconds: int | None = None,
    reason: str = "",
    config_file: str | None = None,
    env: str = "dev",
) -> dict[str, Any]:
    return _actions(config_file, env).claim(
        {
            "session_id": session_id,
            "resource": resource,
            "ttl_seconds": ttl_seconds,
            "reason": reason,
        }
    )


@mcp.tool()
def cfg_agent_release(
    session_id: str,
    lease_id: str,
    config_file: str | None = None,
    env: str = "dev",
) -> dict[str, Any]:
    return _actions(config_file, env).release({"session_id": session_id, "lease_id": lease_id})


@mcp.tool()
def cfg_agent_renew(
    session_id: str,
    lease_id: str,
    ttl_seconds: int | None = None,
    config_file: str | None = None,
    env: str = "dev",
) -> dict[str, Any]:
    """Extend an active lease this session owns (keep a claim alive for long-running work)."""
    return _actions(config_file, env).renew(
        {"session_id": session_id, "lease_id": lease_id, "ttl_seconds": ttl_seconds}
    )


@mcp.tool()
def cfg_agent_open_intent(
    session_id: str,
    resources: list[str],
    summary: str,
    planned_paths: list[str],
    risk_level: str = "medium",
    expected_base: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    config_file: str | None = None,
    env: str = "dev",
) -> dict[str, Any]:
    return _actions(config_file, env).open_intent(
        {
            "session_id": session_id,
            "resources": resources,
            "summary": summary,
            "planned_paths": planned_paths,
            "risk_level": risk_level,
            "expected_base": expected_base or {},
            "idempotency_key": idempotency_key,
        }
    )


@mcp.tool()
def cfg_agent_close_intent(
    session_id: str,
    intent_id: str,
    status: str,
    config_file: str | None = None,
    env: str = "dev",
) -> dict[str, Any]:
    return _actions(config_file, env).close_intent({"session_id": session_id, "intent_id": intent_id, "status": status})


@mcp.tool()
def cfg_agent_validate_patch(
    session_id: str,
    record: str,
    patch: list[dict[str, Any]] | str,
    intent_id: str,
    base: dict[str, Any] | None = None,
    allow_live_drift: bool = False,
    config_file: str | None = None,
    env: str = "dev",
    author: str | None = None,
) -> dict[str, Any]:
    engine = make_engine(ActionContext(config_file=config_file, env=env, author=author))
    parsed_patch = json.loads(patch) if isinstance(patch, str) else patch
    return _actions(config_file, env).validate_patch(
        {
            "engine": engine,
            "session_id": session_id,
            "record": record,
            "patch": parsed_patch,
            "intent_id": intent_id,
            "base": base,
            "allow_live_drift": allow_live_drift,
        }
    )


@mcp.tool()
def cfg_agent_apply_patch(
    session_id: str,
    record: str,
    patch: list[dict[str, Any]] | str,
    intent_id: str,
    message: str,
    base: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    allow_live_drift: bool = False,
    config_file: str | None = None,
    env: str = "dev",
    author: str | None = None,
) -> dict[str, Any]:
    engine = make_engine(ActionContext(config_file=config_file, env=env, author=author))
    parsed_patch = json.loads(patch) if isinstance(patch, str) else patch
    return _actions(config_file, env).apply_patch(
        {
            "engine": engine,
            "session_id": session_id,
            "record": record,
            "patch": parsed_patch,
            "intent_id": intent_id,
            "message": message,
            "base": base,
            "idempotency_key": idempotency_key,
            "allow_live_drift": allow_live_drift,
        }
    )


@mcp.tool()
def cfg_agent_status(
    session_id: str | None = None,
    config_file: str | None = None,
    env: str = "dev",
) -> dict[str, Any]:
    return _actions(config_file, env).status({"session_id": session_id})


@mcp.tool()
def cfg_agent_conflicts(
    status: str | None = None,
    config_file: str | None = None,
    env: str = "dev",
) -> dict[str, Any]:
    return _actions(config_file, env).conflicts({"status": status})


@mcp.tool()
def cfg_agent_watch(
    since_event_id: str | None = None,
    limit: int = 100,
    config_file: str | None = None,
    env: str = "dev",
) -> dict[str, Any]:
    return _actions(config_file, env).watch({"since_event_id": since_event_id, "limit": limit})


def reset_for_tests() -> None:
    global actions
    cached_agent_coordinator.cache_clear()
    actions = AgentActions()
