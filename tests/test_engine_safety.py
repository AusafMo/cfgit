from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from cfg.adapters.base import (
    ApplyResult,
    AtomicityReport,
    AtomicityUnavailable,
    NoSuchConfig,
    ReconcileReport,
    StaleHead,
    StaleLive,
)
from cfg.core.config import (
    CollectionConfig,
    EnvConfig,
    HistoryConfig,
    IdentityConfig,
    ProjectConfig,
    SecretsConfig,
)
from cfg.core.engine import Engine, RecordRef, SecretBlocked
from cfg.core.hashing import hash_doc
from cfg.core.identity import Identity


def test_commit_refuses_secret_matches_before_history_write() -> None:
    engine, adapter = _engine(
        records={("demo", "alpha"): {"id": "alpha", "value": 1}},
        secrets=SecretsConfig(block_fields=("*api_key*",), block_values=("sk-[A-Za-z0-9]{6,}",)),
    )

    with pytest.raises(SecretBlocked):
        engine.commit(
            RecordRef("demo", "alpha"),
            {"id": "alpha", "api_key": "sk-ABCDEF1234"},
            message="try leaked key",
        )

    assert adapter.history == []


def test_allow_secret_is_audited_in_history_meta() -> None:
    engine, adapter = _engine(
        records={("demo", "alpha"): {"id": "alpha", "value": 1}},
        secrets=SecretsConfig(block_fields=("*api_key*",), block_values=("sk-[A-Za-z0-9]{6,}",)),
    )

    result = engine.commit(
        RecordRef("demo", "alpha"),
        {"id": "alpha", "api_key": "sk-ABCDEF1234"},
        message="intentional fixture key",
        allow_secret=True,
    )

    assert result["state"] == "committed"
    assert adapter.history[-1]["meta"]["allow_secret"] is True
    assert adapter.history[-1]["meta"]["allow_secret_author"] == "dev@example.com"
    assert adapter.history[-1]["meta"]["secret_matches"][0]["path"] == "api_key"


def test_secret_fields_are_stripped_instead_of_blocked() -> None:
    coll = CollectionConfig(name="demo", id_field="id", secret_fields=("api_key",))
    engine, adapter = _engine(
        collection=coll,
        records={("demo", "alpha"): {"id": "alpha", "value": 1, "api_key": "sk-ABCDEF1234"}},
        secrets=SecretsConfig(block_fields=("*api_key*",), block_values=("sk-[A-Za-z0-9]{6,}",)),
    )

    result = engine.import_records(RecordRef("demo", "alpha"), message="import with stripped secret")

    assert result[0]["state"] == "imported"
    assert "api_key" not in adapter.history[-1]["doc"]
    meta = adapter.history[-1]["meta"]
    assert set(meta) == {"identity"}
    assert "allow_secret" not in meta
    assert "secret_matches" not in meta


def test_authenticated_identity_is_recorded_in_history_meta() -> None:
    identity = Identity(
        author="dev@example.com",
        mode="authenticated",
        source="token",
        authenticated=True,
        fingerprint="abc12",
        principal="token:abc12",
        credential="token:abc12",
    )
    engine, adapter = _engine(
        records={("demo", "alpha"): {"id": "alpha", "value": 1}},
        identity=identity,
        identity_config=IdentityConfig(mode="authenticated"),
    )

    engine.commit(RecordRef("demo", "alpha"), {"id": "alpha", "value": 2}, message="verified change")

    meta = adapter.history[-1]["meta"]["identity"]
    assert meta["author"] == "dev@example.com"
    assert meta["authenticated"] is True
    assert meta["source"] == "token"
    assert meta["fingerprint"] == "abc12"


def test_mutation_refuses_non_atomic_adapter() -> None:
    engine, adapter = _engine(records={("demo", "alpha"): {"id": "alpha", "value": 1}})
    adapter.atomic = False

    with pytest.raises(AtomicityUnavailable):
        engine.commit(
            RecordRef("demo", "alpha"),
            {"id": "alpha", "value": 2},
            message="change value",
        )


def test_system_restore_recreates_history_only_record() -> None:
    coll = CollectionConfig(name="demo", id_field="id", live_when={"active": True})
    doc = {"id": "alpha", "active": True, "value": "old"}
    row = _history_row(coll, doc, seq=1, valid_from=datetime(2026, 6, 1, tzinfo=timezone.utc))
    engine, adapter = _engine(collection=coll, records={}, history=[row], heads={("demo", "alpha"): row})

    result = engine.restore_system_as_of(
        datetime(2026, 6, 7, 12, tzinfo=timezone.utc),
        message="restore deleted record",
    )

    assert result["state"] == "restored"
    assert result["results"][0]["state"] == "restored_deleted"
    assert adapter.records[("demo", "alpha")] == doc


def test_single_record_valid_time_ref_resolves_date_syntax() -> None:
    coll = CollectionConfig(name="demo", id_field="id")
    row = _history_row(
        coll,
        {"id": "alpha", "value": "old"},
        seq=1,
        valid_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    engine, _adapter = _engine(collection=coll, history=[row], heads={("demo", "alpha"): row})

    resolved = engine.resolve_ref(RecordRef("demo", "alpha"), "@{2026-06-07}")

    assert resolved["seq"] == 1
    assert resolved["doc"]["value"] == "old"


def test_empty_message_is_rejected_in_engine() -> None:
    engine, _adapter = _engine(records={("demo", "alpha"): {"id": "alpha", "value": 1}})

    with pytest.raises(ValueError, match="message must be non-empty"):
        engine.commit(RecordRef("demo", "alpha"), {"id": "alpha", "value": 2}, message=" ")


class FakeAdapter:
    def __init__(
        self,
        *,
        project: ProjectConfig,
        records: dict[tuple[str, str], dict[str, Any]] | None = None,
        history: list[dict[str, Any]] | None = None,
        heads: dict[tuple[str, str], dict[str, Any]] | None = None,
        principal: str | None = None,
        invariant_violations: list[str] | None = None,
    ):
        self.project = project
        self.env_name = "dev"
        self.records = deepcopy(records or {})
        self.history = deepcopy(history or [])
        self.heads = deepcopy(heads or {})
        self.atomic = True
        self.principal = principal
        self.invariant_violations = invariant_violations or []
        self.clock = datetime(2026, 6, 21, tzinfo=timezone.utc)

    def get_record(self, collection: str, record_id: str) -> dict | None:
        doc = self.records.get((collection, record_id))
        return deepcopy(doc) if doc is not None else None

    def put_record(self, collection: str, record_id: str, doc: dict) -> None:
        if (collection, record_id) not in self.records:
            raise NoSuchConfig(f"{collection}:{record_id}")
        self.records[(collection, record_id)] = deepcopy(doc)

    def seed_record(self, collection: str, record_id: str, doc: dict) -> None:
        if (collection, record_id) in self.records:
            raise ValueError("already exists")
        self.records[(collection, record_id)] = deepcopy(doc)

    def list_record_ids(self, collection: str) -> list[str]:
        return sorted(record_id for coll, record_id in self.records if coll == collection)

    def get_head(self, collection: str, record_id: str) -> dict | None:
        row = self.heads.get((collection, record_id))
        return deepcopy(row) if row is not None else None

    def query_history(
        self,
        *,
        collection: str | None = None,
        record_id: str | None = None,
        ref: str | None = None,
        as_of_recorded: datetime | None = None,
        as_of_valid: datetime | None = None,
        tag: str | None = None,
        git_sha: str | None = None,
        limit: int | None = None,
        order: str = "desc",
        with_doc: bool = False,
    ) -> list[dict]:
        rows = []
        for row in self.history:
            if collection is not None and row["collection"] != collection:
                continue
            if record_id is not None and row["record_id"] != record_id:
                continue
            if ref is not None and ref.startswith("@") and row["seq"] != int(ref[1:]):
                continue
            if as_of_recorded is not None and row["recorded_at"] > as_of_recorded:
                continue
            if as_of_valid is not None:
                valid_to = row.get("valid_to")
                if not (row["valid_from"] <= as_of_valid and (valid_to is None or valid_to > as_of_valid)):
                    continue
            if tag is not None and tag not in row.get("tags", []):
                continue
            if git_sha is not None and git_sha not in row.get("git_shas", []):
                continue
            out = deepcopy(row)
            if not with_doc:
                out.pop("doc", None)
            rows.append(out)
        rows.sort(key=lambda item: (item["collection"], item["record_id"], item["seq"]))
        if order == "desc":
            rows.reverse()
        return rows[:limit] if limit is not None else rows

    def list_tags(self) -> list[dict]:
        return []

    def apply(
        self,
        *,
        collection: str,
        record_id: str,
        new_doc: dict | None,
        entry: dict,
        expected_head_oid: str | None,
        expected_live_oid: str | None = None,
        make_head: bool = True,
        seed_missing: bool = False,
    ) -> ApplyResult:
        if not self.atomic:
            raise AtomicityUnavailable("fake adapter is not atomic")
        key = (collection, record_id)
        head = self.heads.get(key)
        current_head = head["oid"] if head else None
        if current_head != expected_head_oid:
            raise StaleHead(current_head)
        if expected_live_oid is not None:
            live = self.records.get(key)
            if live is None:
                raise NoSuchConfig(f"{collection}:{record_id}")
            if hash_doc(live, self.project.collection(collection)) != expected_live_oid:
                raise StaleLive("live changed")
        seq = int(head["seq"]) + 1 if head else 1
        stored = deepcopy(entry)
        stored.update({"env": self.env_name, "collection": collection, "record_id": record_id, "seq": seq})
        if head:
            head["valid_to"] = stored["valid_from"]
        self.history.append(stored)
        if new_doc is not None:
            if seed_missing:
                self.records[key] = deepcopy(new_doc)
            elif key not in self.records:
                raise NoSuchConfig(f"{collection}:{record_id}")
            else:
                self.records[key] = deepcopy(new_doc)
        if make_head:
            self.heads[key] = stored
        return ApplyResult(collection=collection, record_id=record_id, seq=seq, oid=stored["oid"], head_oid=stored["oid"])

    def add_tag(self, *, collection: str, record_id: str, seq: int, tag: str) -> None:
        for row in self.history:
            if row["collection"] == collection and row["record_id"] == record_id and row["seq"] == seq:
                row.setdefault("tags", []).append(tag)

    def remove_tag(self, *, collection: str, record_id: str, seq: int, tag: str) -> None:
        return None

    def list_pending(self) -> list[dict]:
        return []

    def reconcile(self) -> ReconcileReport:
        return ReconcileReport(rolled_forward=[], rolled_back=[])

    def ensure_schema(self) -> None:
        return None

    def check_runtime_invariant(self, collection: str | None = None) -> list[str]:
        return list(self.invariant_violations)

    def check_atomicity_scope(self) -> AtomicityReport:
        return AtomicityReport(
            atomic=self.atomic,
            runtime_cluster="fake",
            history_cluster="fake",
            reason="ok" if self.atomic else "fake adapter is not atomic",
        )

    def backend_name(self) -> str:
        return "fake"

    def supports_transactions(self) -> bool:
        return self.atomic

    def authenticated_principal(self) -> str | None:
        return self.principal

    def now(self) -> datetime:
        return self.clock


def _engine(
    *,
    collection: CollectionConfig | None = None,
    records: dict[tuple[str, str], dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
    heads: dict[tuple[str, str], dict[str, Any]] | None = None,
    secrets: SecretsConfig | None = None,
    identity: Identity | None = None,
    identity_config: IdentityConfig | None = None,
    invariant_violations: list[str] | None = None,
) -> tuple[Engine, FakeAdapter]:
    coll = collection or CollectionConfig(name="demo", id_field="id")
    project = ProjectConfig(
        name="test",
        path=Path("/tmp/.cfg.toml"),
        history=HistoryConfig(),
        collections=(coll,),
        envs={
            "dev": EnvConfig(
                name="dev",
                database="fake",
                uri="",
                db="test",
                identity=identity_config or IdentityConfig(),
            )
        },
        secrets=secrets or SecretsConfig(),
    )
    adapter = FakeAdapter(
        project=project,
        records=records,
        history=history,
        heads=heads,
        invariant_violations=invariant_violations,
    )
    return Engine(project, adapter, env="dev", author="dev@example.com", identity=identity), adapter


def _history_row(
    coll: CollectionConfig,
    doc: dict[str, Any],
    *,
    seq: int,
    valid_from: datetime,
    valid_to: datetime | None = None,
) -> dict[str, Any]:
    oid = hash_doc(doc, coll)
    return {
        "env": "dev",
        "collection": coll.name,
        "record_id": str(doc[coll.id_field]),
        "seq": seq,
        "oid": oid,
        "parent_oid": None,
        "doc": deepcopy(doc),
        "message": "seed",
        "author": "dev@example.com",
        "recorded_at": valid_from,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "valid_from_estimated": False,
        "op": "import",
        "git_shas": [],
        "tags": [],
        "meta": {},
    }


def test_doctor_clean_when_no_issues() -> None:
    coll = CollectionConfig(name="demo", id_field="id")
    engine, _ = _engine(
        collection=coll,
        records={("demo", "alpha"): {"id": "alpha", "value": 1}},
    )
    report = engine.doctor()
    assert report["ok"] is True
    assert report["scanned"] == 1
    assert report["secret_blocks"] == []
    assert report["large_fields"] == []


def test_doctor_groups_secret_hits_and_suggests_secret_fields() -> None:
    coll = CollectionConfig(name="demo", id_field="id")
    engine, _ = _engine(
        collection=coll,
        records={
            ("demo", "a"): {"id": "a", "provider_config": {"api_key": "plain1"}},
            ("demo", "b"): {"id": "b", "provider_config": {"api_key": "plain2"}},
        },
        secrets=SecretsConfig(block_fields=("*api_key*",)),
    )
    report = engine.doctor()
    assert report["ok"] is False
    # two records, same field path -> one grouped block with count 2
    blocks = [b for b in report["secret_blocks"] if b["path"] == "provider_config.api_key"]
    assert len(blocks) == 1
    assert blocks[0]["count"] == 2
    assert blocks[0]["kind"] == "field"
    assert "provider_config.api_key" in report["suggestions"]["demo"]["secret_fields"]


def test_doctor_rolls_nested_secret_path_up_to_container() -> None:
    coll = CollectionConfig(name="demo", id_field="id")
    engine, _ = _engine(
        collection=coll,
        records={
            ("demo", "a"): {
                "id": "a",
                "schema": {"properties": {"openai_api_key": {"type": "string", "title": "Key"}}},
            }
        },
        secrets=SecretsConfig(block_fields=("*api_key*",)),
    )
    report = engine.doctor()
    # the two leaf hits (.type, .title) collapse to one container suggestion
    sf = report["suggestions"]["demo"]["secret_fields"]
    assert sf == ["schema.properties.openai_api_key"]


def test_doctor_flags_value_match_and_large_field() -> None:
    coll = CollectionConfig(name="demo", id_field="id")
    engine, _ = _engine(
        collection=coll,
        records={
            ("demo", "a"): {
                "id": "a",
                "headers": {"X-Auth": "sk-ABCDEFGHIJKLMNOPQRSTUVWX"},
                "blob": "x" * 30000,
            }
        },
        secrets=SecretsConfig(block_values=("sk-[A-Za-z0-9]{20,}",)),
    )
    report = engine.doctor(large_field_bytes=20000)
    assert any(b["kind"] == "value" for b in report["secret_blocks"])
    large = report["large_fields"]
    assert len(large) == 1 and large[0]["path"] == "blob"
    assert large[0]["max_bytes"] >= 30000
    assert "blob" in report["suggestions"]["demo"]["ignore_fields"]


def test_doctor_reports_runtime_invariant_violations() -> None:
    coll = CollectionConfig(name="demo", id_field="id")
    engine, _ = _engine(
        collection=coll,
        records={("demo", "alpha"): {"id": "alpha", "value": 1}},
        invariant_violations=["demo:alpha (2 live records)"],
    )

    report = engine.doctor()

    assert report["ok"] is False
    assert report["key_issues"] == ["demo:alpha (2 live records)"]
