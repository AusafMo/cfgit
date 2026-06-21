# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""ApprovalProvider implementations (SPEC §11). local (TTY) first, then slack/webhook."""
from cfg.approval.base import ApprovalProvider, ApprovalState, Pending

__all__ = ["ApprovalProvider", "ApprovalState", "Pending"]
