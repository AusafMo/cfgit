from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
from typing import Any, Protocol, runtime_checkable

from cfg.core.config import CollectionConfig
from cfg_agent.resources import paths_overlap
from cfg_agent.state import AgentStateError


@runtime_checkable
class AgentPolicyHook(Protocol):
    def check_claim(self, *, session: dict[str, Any], resource: str) -> None: ...
    def check_patch(
        self,
        *,
        session: dict[str, Any],
        record: str,
        patch_paths: list[str],
        collection: CollectionConfig,
    ) -> None: ...
    def review_required(self, *, session: dict[str, Any], record: str, patch_paths: list[str]) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class AgentRolePolicy:
    name: str
    can_claim: tuple[str, ...] = ()
    deny_paths: tuple[str, ...] = ()
    review_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class StaticAgentPolicy:
    roles: dict[str, AgentRolePolicy] = field(default_factory=dict)
    deny_paths: tuple[str, ...] = ()
    review_paths: tuple[str, ...] = ()
    require_claims: bool = True
    require_intent: bool = True
    allow_path_expansion: bool = False

    def check_claim(self, *, session: dict[str, Any], resource: str) -> None:
        role = self._role_for(session)
        patterns = role.can_claim if role else ()
        denied = (*self.deny_paths, *(role.deny_paths if role else ()))
        if denied and _matches_resource_or_path(resource, denied):
            raise AgentStateError(
                "policy_blocked",
                "agent policy denies this resource",
                {"resource": resource, "policy": "deny_paths"},
            )
        if patterns and not _matches_resource_or_path(resource, patterns):
            raise AgentStateError(
                "policy_blocked",
                "agent is not allowed to claim this resource",
                {"resource": resource, "allowed": list(patterns)},
            )

    def check_patch(
        self,
        *,
        session: dict[str, Any],
        record: str,
        patch_paths: list[str],
        collection: CollectionConfig,
    ) -> None:
        role = self._role_for(session)
        denied = (*self.deny_paths, *(role.deny_paths if role else ()))
        for path in patch_paths:
            full = f"{record}:{path}"
            if denied and _matches_resource_or_path(full, denied):
                raise AgentStateError(
                    "policy_blocked",
                    "agent policy denies this patch path",
                    {"record": record, "path": path, "policy": "deny_paths"},
                )
            for secret_path in _secret_json_paths(collection):
                if paths_overlap(secret_path, path):
                    raise AgentStateError(
                        "policy_blocked",
                        "agent patch touches a configured secret field",
                        {"record": record, "path": path, "secret_path": secret_path},
                    )

    def review_required(self, *, session: dict[str, Any], record: str, patch_paths: list[str]) -> dict[str, Any] | None:
        role = self._role_for(session)
        review_patterns = (*self.review_paths, *(role.review_paths if role else ()))
        for path in patch_paths:
            full = f"{record}:{path}"
            if review_patterns and _matches_resource_or_path(full, review_patterns):
                return {
                    "status": "needs_review",
                    "code": "human_review_required",
                    "message": "agent policy requires branch/PR review before runtime mutation",
                    "record": record,
                    "path": path,
                    "patterns": list(review_patterns),
                }
        return None

    def _role_for(self, session: dict[str, Any]) -> AgentRolePolicy | None:
        agent_id = str(session.get("agent_id") or "")
        agent_kind = str(session.get("agent_kind") or "")
        return self.roles.get(agent_id) or self.roles.get(agent_kind)


def _matches_resource_or_path(value: str, patterns: tuple[str, ...]) -> bool:
    resource = value
    path = _path_part(value)
    for pattern in patterns:
        if fnmatch.fnmatchcase(resource, pattern):
            return True
        if pattern.startswith("/") and path is not None and fnmatch.fnmatchcase(path, pattern):
            return True
    return False


def _path_part(value: str) -> str | None:
    if ":/" not in value:
        return value if value.startswith("/") else None
    return "/" + value.split(":/", 1)[1].strip("/")


def _secret_json_paths(collection: CollectionConfig) -> tuple[str, ...]:
    paths = []
    for item in collection.secret_fields:
        value = str(item).strip()
        if not value:
            continue
        if value.startswith("/"):
            paths.append(value)
        else:
            paths.append("/" + value.replace(".", "/").strip("/"))
    return tuple(paths)
