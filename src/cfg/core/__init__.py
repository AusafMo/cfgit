# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""cfg.core — the engine (SPEC §1).

Depends ONLY on StorageAdapter (cfg.adapters.base) and ApprovalProvider
(cfg.approval.base). MUST NOT import any DB driver (pymongo/psycopg/...) or LLM SDK.
That boundary is enforced by tests/test_core_purity.py.

Modules (to be built, SPEC §17):
    hashing   — oid(doc) = sha256(canonical(strip(doc)))            [SPEC §4]
    asof      — valid-time interval reconstruction                  [SPEC §5.8, V3-5]
    engine    — commit/restore/adopt/status orchestration over apply()
    refs      — ref grammar (@seq | sha256: | @{date} | tag: | =live | =HEAD)  [SPEC §5.9]
"""
