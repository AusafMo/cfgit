# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""StorageAdapter implementations (the DB seam, SPEC §2). Mongo is first."""
from cfg.adapters.base import (
    AmbiguousConfig,
    ApplyItem,
    ApplyResult,
    AtomicityReport,
    HistoryEnvMismatch,
    NoSuchConfig,
    ReconcileReport,
    StaleHead,
    StaleLive,
    StorageAdapter,
)

__all__ = [
    "StorageAdapter",
    "ApplyItem",
    "ApplyResult",
    "ReconcileReport",
    "AtomicityReport",
    "StaleHead",
    "StaleLive",
    "AmbiguousConfig",
    "HistoryEnvMismatch",
    "NoSuchConfig",
]
