# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""ApprovalProvider — the real human-in-the-loop (SPEC §11).

The keystone for "safe for agents AND safe for fat-fingers." A gated-env mutation
NEVER completes from a caller-supplied flag; it completes only when a human resolves
an approval OUT OF BAND (a different channel / a separate invocation). There is
intentionally NO MCP tool that grants approval — an agent can observe status, never
grant it (SPEC §5.18, §12).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

ApprovalState = Literal["pending", "approved", "denied", "expired"]


@dataclass
class Pending:
    approval_id: str
    state: ApprovalState  # "pending" on creation


@runtime_checkable
class ApprovalProvider(Protocol):
    def request(self, *, action: dict, requester: str, env: str) -> Pending: ...
    def status(self, approval_id: str) -> ApprovalState: ...
    # resolution (approve/deny) happens OUT OF BAND, not through this caller.
    # The id is single-use and bound to (action, plan_oid); a changed plan invalidates it.
