from __future__ import annotations

import pytest

from cfg.core.authz import PermissionDenied, authorize_mutation, permission_role
from cfg.core.config import EnvConfig, PermissionConfig


def test_open_permissions_allow_any_author() -> None:
    env = EnvConfig(name="dev", database="mongo", uri="", db="demo")

    authorize_mutation(env, author="alice@example.com", action="commit")

    assert permission_role(env.permissions, "alice@example.com") == "open"


def test_restricted_permissions_allow_writers_and_admins() -> None:
    env = EnvConfig(
        name="dev",
        database="mongo",
        uri="",
        db="demo",
        permissions=PermissionConfig(
            mode="restricted",
            admins=("admin@example.com",),
            writers=("*.team@example.com",),
        ),
    )

    authorize_mutation(env, author="admin@example.com", action="commit")
    authorize_mutation(env, author="bob.team@example.com", action="commit")

    assert permission_role(env.permissions, "admin@example.com") == "admin"
    assert permission_role(env.permissions, "bob.team@example.com") == "writer"


def test_restricted_permissions_reject_unknown_author() -> None:
    env = EnvConfig(
        name="prod",
        database="mongo",
        uri="",
        db="demo",
        permissions=PermissionConfig(mode="restricted", admins=("admin@example.com",)),
    )

    with pytest.raises(PermissionDenied):
        authorize_mutation(env, author="unknown@example.com", action="commit")


def test_admin_action_requires_admin_even_for_writer() -> None:
    env = EnvConfig(
        name="prod",
        database="mongo",
        uri="",
        db="demo",
        permissions=PermissionConfig(
            mode="restricted",
            admins=("admin@example.com",),
            writers=("bob@example.com",),
            admin_actions=("restore_system",),
        ),
    )

    with pytest.raises(PermissionDenied):
        authorize_mutation(env, author="bob@example.com", action="restore_system")

    authorize_mutation(env, author="admin@example.com", action="restore_system")
