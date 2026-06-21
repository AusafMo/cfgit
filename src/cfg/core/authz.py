# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""DB-neutral mutation authorization for cfgit."""
from __future__ import annotations

import fnmatch

from cfg.core.config import EnvConfig, PermissionConfig
from cfg.core.identity import Identity, self_asserted_identity


class PermissionDenied(Exception):
    """The resolved author is not allowed to perform this mutation."""


def authorize_mutation(
    env: EnvConfig,
    *,
    action: str,
    identity: Identity | None = None,
    author: str | None = None,
) -> None:
    resolved = identity or self_asserted_identity(author or "", cfg=env.identity)
    if env.identity.mode in {"authenticated", "enforced"} and not resolved.authenticated:
        raise PermissionDenied(
            f"{resolved.author} cannot run {action} on {env.name}: "
            f"{env.identity.mode} identity required"
        )

    role = permission_role(env.permissions, resolved.author)
    admin_only = _matches(action, env.permissions.admin_actions)

    if admin_only and role != "admin":
        raise PermissionDenied(
            f"{resolved.author} cannot run {action} on {env.name}: admin permission required"
        )

    if env.permissions.mode == "restricted" and role not in {"admin", "writer"}:
        raise PermissionDenied(
            f"{resolved.author} cannot run {action} on {env.name}: writer permission required"
        )


def permission_role(policy: PermissionConfig, author: str) -> str:
    if _matches(author, policy.admins):
        return "admin"
    if _matches(author, policy.writers):
        return "writer"
    return "open" if policy.mode == "open" else "none"


def _matches(value: str, patterns: tuple[str, ...]) -> bool:
    raw = value.strip()
    lowered = raw.lower()
    for pattern in patterns:
        candidate = pattern.strip()
        if fnmatch.fnmatchcase(raw, candidate) or fnmatch.fnmatchcase(lowered, candidate.lower()):
            return True
    return False
