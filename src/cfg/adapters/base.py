# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""StorageAdapter: the DB seam (SPEC §2).

The single contract every backend implements. The core engine talks ONLY to this
Protocol; no DB driver is imported in cfg.core (enforced by tests/test_core_purity).

The v1 surface is collection-aware: cfgit versions opaque records in live
collections, not only "configs." Mongo is the first concrete adapter
(cfg.adapters.mongo).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


# --- errors the engine branches on (mapped to CLI exit codes / MCP envelope) ---
class StaleHead(Exception):
    """A concurrent commit moved HEAD since we read it. Exit 2."""


class StaleLive(Exception):
    """A raw bypass moved the runtime record since we read it. Exit 2."""


class AmbiguousConfig(Exception):
    """More than one live record matched a collection id. Exit 6."""


class HistoryEnvMismatch(AmbiguousConfig):
    """History for this record exists under another configured env name. Exit 6."""


class NoSuchConfig(Exception):
    """No live record matched where one was required. Exit 5."""


class AtomicityUnavailable(Exception):
    """The backend cannot make the requested mutation atomically. Exit 3."""


@dataclass
class ApplyResult:
    collection: str
    record_id: str
    seq: int
    oid: str
    head_oid: str


@dataclass
class ApplyItem:
    collection: str
    record_id: str
    new_doc: dict | None
    entry: dict
    expected_head_oid: str | None
    expected_live_oid: str | None = None
    make_head: bool = True
    seed_missing: bool = False


@dataclass
class ReconcileReport:
    rolled_forward: list[str]
    rolled_back: list[str]


@dataclass
class AtomicityReport:
    """[SPEC V3-1] Whether runtime+history+heads can share one transaction."""
    atomic: bool
    runtime_cluster: str
    history_cluster: str
    reason: str


def history_env_mismatch_message(
    *,
    collection: str,
    record_id: str,
    current_env: str,
    other_envs: list[str],
) -> str:
    env_list = ", ".join(other_envs)
    return (
        f"no history found for {collection}:{record_id} under env={current_env!r}, "
        f"but history/head rows exist under env(s): {env_list}. "
        "Run with the env name that originally wrote this history, or update .cfg.toml so "
        "this database is always addressed with one stable env name."
    )


@runtime_checkable
class StorageAdapter(Protocol):
    # runtime store
    def get_record(self, collection: str, record_id: str) -> dict | None: ...
    def put_record(self, collection: str, record_id: str, doc: dict) -> None: ...
    def seed_record(self, collection: str, record_id: str, doc: dict) -> None: ...
    def list_record_ids(self, collection: str) -> list[str]: ...

    # OPTIONAL batch reads. Callers (e.g. Engine.status) feature-detect these and
    # fall back to per-record get_record/get_head when an adapter does not provide
    # them, so third-party adapters that omit them keep working. Implementing them
    # collapses the per-record N+1 (2 round-trips per record) into ~2 queries per
    # collection — the difference between a snappy and a stalling UI on a remote DB.
    def get_records(self, collection: str, record_ids: list[str]) -> dict[str, dict]: ...
    def get_heads(self, collection: str, record_ids: list[str]) -> dict[str, dict]: ...

    # history reads
    def get_head(self, collection: str, record_id: str) -> dict | None: ...
    def query_history(self, *, collection: str | None = None, record_id: str | None = None,
                      ref: str | None = None,
                      as_of_recorded: datetime | None = None, as_of_valid: datetime | None = None,
                      tag: str | None = None, git_sha: str | None = None,
                      limit: int | None = None, order: str = "desc",
                      with_doc: bool = False) -> list[dict]: ...
    def list_tags(self) -> list[dict]: ...

    # optional typed sidecar refs (branches, branch commits, PRs)
    def put_ref(self, doc: dict) -> None: ...
    def get_ref(self, ref_type: str, ref_id: str) -> dict | None: ...
    def list_refs(self, ref_type: str, **filters) -> list[dict]: ...
    def delete_ref(self, ref_type: str, ref_id: str) -> None: ...

    # the one atomic mutation
    def apply(self, *, collection: str, record_id: str, new_doc: dict | None, entry: dict,
              expected_head_oid: str | None, expected_live_oid: str | None = None,
              make_head: bool = True, seed_missing: bool = False) -> ApplyResult: ...
    def apply_many(self, *, items: list[ApplyItem], put_refs: list[dict] | None = None) -> list[ApplyResult]: ...

    # labels
    def add_tag(self, *, collection: str, record_id: str, seq: int, tag: str) -> None: ...
    def remove_tag(self, *, collection: str, record_id: str, seq: int, tag: str) -> None: ...

    # crash recovery / non-atomic fallback
    def list_pending(self) -> list[dict]: ...
    def reconcile(self) -> ReconcileReport: ...

    # meta
    def ensure_schema(self) -> None: ...
    def check_runtime_invariant(self, collection: str | None = None) -> list[str]: ...
    def check_atomicity_scope(self) -> AtomicityReport: ...
    def backend_name(self) -> str: ...
    def supports_transactions(self) -> bool: ...
    def authenticated_principal(self) -> str | None: ...
    def now(self) -> datetime: ...
