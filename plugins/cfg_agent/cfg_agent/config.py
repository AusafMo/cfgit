from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from cfg.core.config import ProjectConfig, load_config
from cfg_agent.coordinator import AgentCoordinator
from cfg_agent.policy import AgentRolePolicy, StaticAgentPolicy
from cfg_agent.state import AgentStateAdapter, InMemoryAgentStateAdapter

try:  # pragma: no cover - py311+ path is normal
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


@dataclass(frozen=True)
class AgentPluginConfig:
    enabled: bool = False
    state_backend: str = "memory"
    state_collection: str = "cfgit_agent_state"
    events_collection: str = "cfgit_agent_events"
    default_lease_ttl_seconds: int = 900
    deny_paths: tuple[str, ...] = ()
    review_paths: tuple[str, ...] = ()
    require_claims: bool = True
    require_intent: bool = True
    allow_path_expansion: bool = False
    roles: dict[str, AgentRolePolicy] = field(default_factory=dict)


def load_agent_config(path: str | Path | None = None) -> AgentPluginConfig:
    cfg_path = _resolve_config_path(path)
    if cfg_path is None:
        return AgentPluginConfig()
    with cfg_path.open("rb") as f:
        raw = tomllib.load(f)
    agent_raw = dict(raw.get("agent") or {})
    policies_raw = dict(agent_raw.get("policies") or {})
    roles = {
        role.name: role
        for role in (_load_role(item) for item in agent_raw.get("role", ()))
    }
    return AgentPluginConfig(
        enabled=bool(agent_raw.get("enabled", False)),
        state_backend=str(agent_raw.get("state_backend", agent_raw.get("backend", "memory"))),
        state_collection=str(agent_raw.get("state_collection", "cfgit_agent_state")),
        events_collection=str(agent_raw.get("events_collection", "cfgit_agent_events")),
        default_lease_ttl_seconds=int(agent_raw.get("default_lease_ttl_seconds", 900)),
        deny_paths=tuple(str(item) for item in policies_raw.get("deny_paths", ())),
        review_paths=tuple(
            str(item)
            for item in policies_raw.get(
                "review_paths",
                policies_raw.get("require_human_review_for", ()),
            )
        ),
        require_claims=bool(policies_raw.get("require_claims", True)),
        require_intent=bool(policies_raw.get("require_intent", True)),
        allow_path_expansion=bool(policies_raw.get("allow_path_expansion", False)),
        roles=roles,
    )


def make_agent_coordinator(
    *,
    config_file: str | Path | None = None,
    env: str = "dev",
    project: ProjectConfig | None = None,
) -> AgentCoordinator:
    if project is None:
        project = load_config(config_file)
    agent_cfg = load_agent_config(config_file or project.path)
    adapter = make_agent_state_adapter(project=project, env=env, agent_cfg=agent_cfg)
    adapter.init_agent_state()
    policy = StaticAgentPolicy(
        roles=agent_cfg.roles,
        deny_paths=agent_cfg.deny_paths,
        review_paths=agent_cfg.review_paths,
        require_claims=agent_cfg.require_claims,
        require_intent=agent_cfg.require_intent,
        allow_path_expansion=agent_cfg.allow_path_expansion,
    )
    return AgentCoordinator(
        adapter=adapter,
        policy=policy,
        default_lease_ttl_seconds=agent_cfg.default_lease_ttl_seconds,
    )


@lru_cache(maxsize=32)
def cached_agent_coordinator(config_file: str | None, env: str) -> AgentCoordinator:
    return make_agent_coordinator(config_file=config_file, env=env)


def make_agent_state_adapter(
    *,
    project: ProjectConfig,
    env: str,
    agent_cfg: AgentPluginConfig,
) -> AgentStateAdapter:
    backend = agent_cfg.state_backend.lower()
    if not agent_cfg.enabled:
        backend = "memory"
    if backend == "auto":
        backend = project.envs[env].database.lower()
    if backend == "memory":
        return InMemoryAgentStateAdapter()
    if backend == "mongo":
        from cfg_agent.adapters.mongo import MongoAgentStateAdapter

        return MongoAgentStateAdapter(
            project=project,
            env_name=env,
            state_collection=agent_cfg.state_collection,
            events_collection=agent_cfg.events_collection,
        )
    if backend == "postgres":
        from cfg_agent.adapters.postgres import PostgresAgentStateAdapter

        return PostgresAgentStateAdapter(
            project=project,
            env_name=env,
            state_collection=agent_cfg.state_collection,
            events_collection=agent_cfg.events_collection,
        )
    raise ValueError("agent.state_backend must be memory, auto, mongo, or postgres")


def _load_role(data: dict[str, Any]) -> AgentRolePolicy:
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("[[agent.role]] entries require name")
    return AgentRolePolicy(
        name=name,
        can_claim=tuple(str(item) for item in data.get("can_claim", ())),
        deny_paths=tuple(str(item) for item in data.get("deny_paths", ())),
        review_paths=tuple(str(item) for item in data.get("review_paths", data.get("require_human_review_for", ()))),
    )


def _resolve_config_path(path: str | Path | None) -> Path | None:
    if path is not None:
        return Path(path).expanduser().resolve()
    local = Path(".cfg.toml")
    if local.exists():
        return local.resolve()
    example = Path("examples/.cfg.toml")
    if example.exists():
        return example.resolve()
    return None
