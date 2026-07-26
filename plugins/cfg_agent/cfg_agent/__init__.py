"""Agent coordination primitives for cfgit."""

from cfg_agent.coordinator import AgentCoordinator
from cfg_agent.policy import AgentPolicyHook, StaticAgentPolicy
from cfg_agent.resources import ResourceRef, parse_resource, paths_overlap, resources_overlap
from cfg_agent.state import AgentStateAdapter, AgentStateError, InMemoryAgentStateAdapter

__all__ = [
    "AgentCoordinator",
    "AgentPolicyHook",
    "AgentStateAdapter",
    "AgentStateError",
    "InMemoryAgentStateAdapter",
    "ResourceRef",
    "StaticAgentPolicy",
    "parse_resource",
    "paths_overlap",
    "resources_overlap",
]
