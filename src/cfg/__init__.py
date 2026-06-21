# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""cfg — git-shaped version control for database-resident config documents.

See docs/SPEC.md for the specification. Architecture (SPEC §1):
    cfg.core      — engine; depends ONLY on StorageAdapter + ApprovalProvider.
    cfg.adapters  — StorageAdapter implementations (the DB seam).
    cfg.approval  — ApprovalProvider implementations (out-of-band human approval).
    cfg.cli       — porcelain + plumbing interfaces.
    cfg.mcp       — agent surface (uniform result envelope).
The optional LLM impact layer lives OUT of this package, in plugins/cfg_impact.
"""

__version__ = "0.0.0"
