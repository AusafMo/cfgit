from __future__ import annotations

import hashlib
import json
from datetime import timedelta
import re
from typing import Any

from cfg.core.diff import diff_values
from cfg.core.engine import RecordRef
from cfg_agent.patches import apply_json_patch, patch_paths
from cfg_agent.resources import parse_resource, paths_overlap, resources_overlap
from cfg_agent.policy import AgentPolicyHook, StaticAgentPolicy
from cfg_agent.state import AgentStateAdapter, AgentStateError, InMemoryAgentStateAdapter, new_id, utcnow


VALID_SESSION_STATUSES = {"running", "blocked", "completed", "failed", "abandoned"}
VALID_INTENT_STATUSES = {"open", "committed", "review_requested", "superseded", "rejected", "abandoned"}


class AgentCoordinator:
    def __init__(
        self,
        adapter: AgentStateAdapter | None = None,
        *,
        policy: AgentPolicyHook | None = None,
        default_lease_ttl_seconds: int = 900,
    ) -> None:
        self.adapter = adapter or InMemoryAgentStateAdapter()
        self.policy = policy or StaticAgentPolicy()
        self.default_lease_ttl_seconds = default_lease_ttl_seconds

    def start_session(
        self,
        *,
        task: str,
        agent_id: str = "agent",
        agent_kind: str = "custom",
        actor: str | None = None,
        tool_client: str = "mcp",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        session = {
            "kind": "session",
            "session_id": new_id("ses"),
            "agent_id": agent_id,
            "agent_kind": agent_kind,
            "task": task,
            "actor": actor or agent_id,
            "status": "running",
            "started_at": _iso(now),
            "heartbeat_at": _iso(now),
            "ended_at": None,
            "tool_client": tool_client,
            "metadata": metadata or {},
        }
        created = self.adapter.create_session(session)
        self._event("session.started", session_id=created["session_id"], actor=created["actor"], details=created)
        return created

    def heartbeat(self, session_id: str) -> dict[str, Any]:
        session = self.adapter.heartbeat_session(session_id, utcnow())
        self._event("session.heartbeat", session_id=session_id, actor=session.get("actor"), details={})
        return session

    def end_session(self, session_id: str, *, status: str = "completed", summary: str | None = None) -> dict[str, Any]:
        if status not in VALID_SESSION_STATUSES:
            raise AgentStateError("bad_session_status", "invalid session status", {"status": status})
        session = self.adapter.close_session(session_id, status, utcnow())
        self._event(
            "session.ended",
            session_id=session_id,
            actor=session.get("actor"),
            details={"status": status, "summary": summary},
        )
        return session

    def claim(
        self,
        *,
        session_id: str,
        resource: str,
        ttl_seconds: int | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        session = self._require_running_session(session_id)
        parsed = parse_resource(resource)
        self.policy.check_claim(session=session, resource=parsed.format())
        now = utcnow()
        lease = {
            "kind": "lease",
            "lease_id": new_id("lea"),
            "session_id": session_id,
            "resource": parsed.format(),
            "collection": parsed.collection,
            "record_id": parsed.record_id,
            "path": parsed.path,
            "scope": parsed.scope,
            "status": "active",
            "reason": reason,
            "created_at": _iso(now),
            "expires_at": _iso(now + timedelta(seconds=ttl_seconds or self.default_lease_ttl_seconds)),
            "released_at": None,
        }
        try:
            created = self.adapter.acquire_lease(lease, now=now)
        except AgentStateError as exc:
            if exc.code != "lease_conflict":
                raise
            conflict = self._open_conflict(
                session_id=session_id,
                resource=parsed.format(),
                conflict_type="lease_conflict",
                message=exc.message,
                details=exc.details,
            )
            raise AgentStateError(exc.code, exc.message, {"conflict": conflict, **exc.details}) from exc
        self._event(
            "lease.acquired",
            session_id=session_id,
            actor=session.get("actor"),
            resource=created["resource"],
            details=created,
        )
        return created

    def release(self, *, session_id: str, lease_id: str) -> dict[str, Any]:
        session = self._require_running_session(session_id)
        lease_before_release = self.adapter.get_lease(lease_id)
        if lease_before_release is None:
            raise AgentStateError("lease_not_found", f"{lease_id} was not found", {"id": lease_id})
        if lease_before_release.get("session_id") != session_id:
            raise AgentStateError(
                "lease_not_owned",
                "lease belongs to a different session",
                {"lease_id": lease_id, "owner_session_id": lease_before_release.get("session_id")},
            )
        lease = self.adapter.release_lease(lease_id, now=utcnow())
        self._event(
            "lease.released",
            session_id=session_id,
            actor=session.get("actor"),
            resource=lease.get("resource"),
            details=lease,
        )
        return lease

    def renew(self, *, session_id: str, lease_id: str, ttl_seconds: int | None = None) -> dict[str, Any]:
        """Extend an active lease the session owns, so a long-running agent can keep its claim
        without releasing and re-acquiring (which would open a window for another agent)."""
        session = self._require_running_session(session_id)
        lease = self.adapter.get_lease(lease_id)
        if lease is None:
            raise AgentStateError("lease_not_found", f"{lease_id} was not found", {"id": lease_id})
        if lease.get("session_id") != session_id:
            raise AgentStateError(
                "lease_not_owned",
                "lease belongs to a different session",
                {"lease_id": lease_id, "owner_session_id": lease.get("session_id")},
            )
        ttl = int(ttl_seconds) if ttl_seconds else self.default_lease_ttl_seconds
        renewed = self.adapter.renew_lease(lease_id, ttl_seconds=ttl, now=utcnow())
        self._event(
            "lease.renewed",
            session_id=session_id,
            actor=session.get("actor"),
            resource=renewed.get("resource"),
            details=renewed,
        )
        return renewed

    def open_intent(
        self,
        *,
        session_id: str,
        resources: list[str],
        summary: str,
        planned_paths: list[str],
        risk_level: str = "medium",
        expected_base: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        session = self._require_running_session(session_id)
        if not resources:
            raise AgentStateError("bad_intent", "intent requires at least one resource")
        normalized_resources = [parse_resource(resource).format() for resource in resources]
        now = utcnow()
        intent = {
            "kind": "intent",
            "intent_id": new_id("int"),
            "session_id": session_id,
            "resources": normalized_resources,
            "summary": summary,
            "planned_paths": planned_paths,
            "risk_level": risk_level,
            "expected_base": expected_base or {},
            "idempotency_key": idempotency_key,
            "status": "open",
            "created_at": _iso(now),
            "closed_at": None,
        }
        created = self.adapter.open_intent(intent)
        self._event("intent.opened", session_id=session_id, actor=session.get("actor"), details=created)
        return created

    def close_intent(self, *, session_id: str, intent_id: str, status: str) -> dict[str, Any]:
        session = self._require_running_session(session_id)
        if status not in VALID_INTENT_STATUSES:
            raise AgentStateError("bad_intent_status", "invalid intent status", {"status": status})
        intent = self.adapter.close_intent(intent_id, status, utcnow())
        self._event("intent.closed", session_id=session_id, actor=session.get("actor"), details=intent)
        return intent

    def remember_idempotency(
        self,
        *,
        key: str,
        payload: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        payload_hash = _payload_hash(payload)
        remembered = self.adapter.remember_idempotency(key, payload_hash, result, utcnow())
        if remembered.replay:
            return {"replay": True, "result": remembered.result}
        return {"replay": False, "result": result}

    def validate_patch(
        self,
        *,
        engine: Any,
        session_id: str,
        record: str,
        patch: list[dict[str, Any]],
        intent_id: str,
        base: dict[str, Any] | None = None,
        allow_live_drift: bool = False,
    ) -> dict[str, Any]:
        session = self._require_running_session(session_id)
        record_ref = _parse_record(record)
        record_resource = f"{record_ref.collection}:{record_ref.record_id}"
        paths = patch_paths(patch)
        intent = self.adapter.get_intent(intent_id)
        if intent is None or intent.get("session_id") != session_id or intent.get("status") != "open":
            conflict = self._open_conflict(
                session_id=session_id,
                resource=record_resource,
                conflict_type="intent_required",
                message="open intent is required for patch validation",
                details={"intent_id": intent_id},
            )
            raise AgentStateError("intent_required", "open intent is required", {"conflict": conflict})
        if not any(resources_overlap(resource, record_resource) for resource in intent.get("resources", [])):
            conflict = self._open_conflict(
                session_id=session_id,
                resource=record_resource,
                conflict_type="intent_scope",
                message="intent does not include this record",
                details={"intent_id": intent_id, "resources": intent.get("resources", [])},
            )
            raise AgentStateError("intent_scope", "intent does not include this record", {"conflict": conflict})
        self._check_planned_paths(session_id, record_resource, paths, intent)
        if getattr(self.policy, "require_claims", True):
            self._check_claims(session_id, record_ref, paths)
        self.policy.check_patch(
            session=session,
            record=record_resource,
            patch_paths=paths,
            collection=engine.config.collection(record_ref.collection),
        )

        head = engine.resolve_ref(record_ref, "=HEAD")
        status = engine.status(record_ref)[0]
        # Resolve the expected base tolerantly. Both the `base=` param and the intent's stored
        # expected_base may be given either FLAT ({head_seq, head_oid}) or NESTED keyed by record
        # ({record: {head_seq, head_oid}}). Accept both so the stale-write guarantee does not
        # silently fail open on an easy-to-get-wrong shape.
        expected = _resolve_expected_base(base, intent.get("expected_base"), record_resource)
        expected_seq = expected.get("head_seq")
        expected_oid = expected.get("head_oid")
        if expected_seq is not None and int(expected_seq) != int(head.get("seq")):
            conflict = self._open_conflict(
                session_id=session_id,
                resource=record_resource,
                conflict_type="base_moved",
                message="HEAD sequence moved since the patch base was chosen",
                details={"expected_head_seq": expected_seq, "actual_head_seq": head.get("seq")},
            )
            raise AgentStateError("base_moved", "HEAD sequence moved", {"conflict": conflict})
        if expected_oid is not None and expected_oid != head.get("oid"):
            conflict = self._open_conflict(
                session_id=session_id,
                resource=record_resource,
                conflict_type="base_moved",
                message="HEAD oid moved since the patch base was chosen",
                details={"expected_head_oid": expected_oid, "actual_head_oid": head.get("oid")},
            )
            raise AgentStateError("base_moved", "HEAD oid moved", {"conflict": conflict})
        if status.state == "changed_outside_cfgit" and not allow_live_drift:
            conflict = self._open_conflict(
                session_id=session_id,
                resource=record_resource,
                conflict_type="live_drift",
                message="live record differs from cfgit HEAD",
                details={"live_oid": status.live_oid, "head_oid": status.head_oid},
            )
            raise AgentStateError("live_drift", "live record differs from cfgit HEAD", {"conflict": conflict})

        patched = apply_json_patch(head["doc"], patch)
        changes = diff_values(head["doc"], patched)
        result = {
            "state": "ok",
            "record": record_resource,
            "base": {"head_seq": head.get("seq"), "head_oid": head.get("oid")},
            "patch_paths": paths,
            "changes": changes,
            "patched_doc": patched,
            "intent_id": intent_id,
        }
        self._event(
            "patch.validated",
            session_id=session_id,
            actor=session.get("actor"),
            resource=record_resource,
            details={key: value for key, value in result.items() if key != "patched_doc"},
        )
        return result

    def apply_patch(
        self,
        *,
        engine: Any,
        session_id: str,
        record: str,
        patch: list[dict[str, Any]],
        intent_id: str,
        message: str,
        base: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        allow_live_drift: bool = False,
    ) -> dict[str, Any]:
        session = self._require_running_session(session_id)
        payload = {
            "record": record,
            "patch": patch,
            "intent_id": intent_id,
            "message": message,
            "base": base,
            "allow_live_drift": allow_live_drift,
        }
        payload_hash = _payload_hash(payload)
        if idempotency_key:
            existing = self.adapter.get_idempotency(idempotency_key)
            if existing:
                if existing.get("payload_hash") != payload_hash:
                    raise AgentStateError(
                        "idempotency_conflict",
                        "idempotency key was already used with a different payload",
                        {"key": idempotency_key},
                    )
                return {
                    "state": "replayed",
                    "idempotency_key": idempotency_key,
                    "result": existing.get("result"),
                }

        validation = self.validate_patch(
            engine=engine,
            session_id=session_id,
            record=record,
            patch=patch,
            intent_id=intent_id,
            base=base,
            allow_live_drift=allow_live_drift,
        )
        review = self.policy.review_required(
            session=session,
            record=validation["record"],
            patch_paths=validation["patch_paths"],
        )
        if review:
            result = self._route_patch_to_pr(
                engine=engine,
                session=session,
                validation=validation,
                message=message,
                review=review,
            )
            self.adapter.close_intent(intent_id, "review_requested", utcnow())
            if idempotency_key:
                self.adapter.remember_idempotency(idempotency_key, payload_hash, result, utcnow())
            return result
        record_ref = _parse_record(record)
        commit = engine.commit(record_ref, validation["patched_doc"], message=message)
        if commit.get("state") != "committed":
            conflict = self._open_conflict(
                session_id=session_id,
                resource=validation["record"],
                conflict_type="apply_blocked",
                message="cfgit core did not commit the patch",
                details={"commit": commit},
            )
            raise AgentStateError("apply_blocked", "cfgit core did not commit the patch", {"conflict": conflict})
        result = {
            "state": "applied",
            "record": validation["record"],
            "intent_id": intent_id,
            "commit": commit,
            "changes": validation["changes"],
        }
        self.adapter.close_intent(intent_id, "committed", utcnow())
        if idempotency_key:
            self.adapter.remember_idempotency(idempotency_key, payload_hash, result, utcnow())
        self._event(
            "patch.applied",
            session_id=session_id,
            actor=session.get("actor"),
            resource=validation["record"],
            details={key: value for key, value in result.items() if key != "changes"},
        )
        self._event(
            "commit.created",
            session_id=session_id,
            actor=session.get("actor"),
            resource=validation["record"],
            details=commit,
        )
        return result

    def _route_patch_to_pr(
        self,
        *,
        engine: Any,
        session: dict[str, Any],
        validation: dict[str, Any],
        message: str,
        review: dict[str, Any],
    ) -> dict[str, Any]:
        if not engine.config.branches.enabled:
            conflict = self._open_conflict(
                session_id=session["session_id"],
                resource=validation["record"],
                conflict_type="review_unavailable",
                message="agent policy requires review, but cfgit branches are not enabled",
                details={"review": review},
            )
            raise AgentStateError(
                "review_unavailable",
                "agent policy requires review, but cfgit branches are not enabled",
                {"conflict": conflict},
            )
        record_ref = _parse_record(validation["record"])
        branch = self._create_review_branch(engine, session=session, record=validation["record"])
        draft = engine.branch_commit(
            branch,
            record_ref,
            validation["patched_doc"],
            message=message,
        )
        if draft.get("state") != "committed":
            conflict = self._open_conflict(
                session_id=session["session_id"],
                resource=validation["record"],
                conflict_type="review_branch_blocked",
                message="cfgit branch draft commit was blocked",
                details={"draft": draft, "review": review},
            )
            raise AgentStateError("review_branch_blocked", "cfgit branch draft commit was blocked", {"conflict": conflict})
        pr = engine.pr_create(
            base=engine.config.branches.default_branch,
            head=branch,
            message=message,
        )
        linked_pr = {
            **pr,
            "agent": {
                "session_id": session["session_id"],
                "agent_id": session.get("agent_id"),
                "intent_id": validation["intent_id"],
                "review": review,
            },
        }
        engine.adapter.put_ref(linked_pr)
        result = {
            "state": "review_requested",
            "record": validation["record"],
            "intent_id": validation["intent_id"],
            "branch": branch,
            "draft_commit": draft,
            "pr": linked_pr,
            "review": review,
            "runtime_mutated": False,
        }
        self._event(
            "patch.routed_to_pr",
            session_id=session["session_id"],
            actor=session.get("actor"),
            resource=validation["record"],
            details={
                "branch": branch,
                "pr_id": linked_pr["id"],
                "intent_id": validation["intent_id"],
                "review": review,
            },
        )
        self._event(
            "pr.created",
            session_id=session["session_id"],
            actor=session.get("actor"),
            resource=validation["record"],
            details=linked_pr,
        )
        return result

    def _create_review_branch(self, engine: Any, *, session: dict[str, Any], record: str) -> str:
        prefix = f"agent/{_safe_branch_part(session.get('agent_id') or 'agent')}/{_safe_branch_part(record)}"
        for _attempt in range(5):
            branch = f"{prefix}/{new_id('rev')[-8:]}"[:128].rstrip("/.")
            try:
                engine.branch_create(branch, from_branch=engine.config.branches.default_branch, message=f"agent review {record}")
                return branch
            except ValueError as exc:
                if "branch already exists" not in str(exc):
                    raise
        raise AgentStateError("branch_conflict", "could not create a unique agent review branch", {"record": record})

    def status(self, session_id: str | None = None) -> dict[str, Any]:
        return {
            "session": self.adapter.get_session(session_id) if session_id else None,
            "sessions": [] if session_id else self.adapter.list_sessions(),
            "leases": self.adapter.list_leases(active_only=False),
            "intents": self.adapter.list_intents(),
            "conflicts": self.adapter.list_conflicts(status=None),
        }

    def conflicts(self, status: str | None = None) -> list[dict[str, Any]]:
        return self.adapter.list_conflicts(status=status)

    def watch(self, *, since_event_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.adapter.list_events(since_event_id=since_event_id, limit=limit)

    def _check_planned_paths(
        self,
        session_id: str,
        record_resource: str,
        paths: list[str],
        intent: dict[str, Any],
    ) -> None:
        planned = list(intent.get("planned_paths") or [])
        missing = [path for path in paths if not any(paths_overlap(plan, path) for plan in planned)]
        if missing:
            conflict = self._open_conflict(
                session_id=session_id,
                resource=record_resource,
                conflict_type="intent_scope",
                message="patch touches paths outside the declared intent",
                details={"missing_paths": missing, "planned_paths": planned},
            )
            raise AgentStateError("intent_scope", "patch touches paths outside the declared intent", {"conflict": conflict})

    def _check_claims(self, session_id: str, record_ref: RecordRef, paths: list[str]) -> None:
        leases = [
            lease
            for lease in self.adapter.list_leases(active_only=True)
            if lease.get("session_id") == session_id
        ]
        missing: list[str] = []
        for path in paths:
            target = f"{record_ref.collection}:{record_ref.record_id}:{path}"
            if not any(resources_overlap(lease["resource"], target) for lease in leases):
                missing.append(path)
        if missing:
            resource = f"{record_ref.collection}:{record_ref.record_id}"
            conflict = self._open_conflict(
                session_id=session_id,
                resource=resource,
                conflict_type="claim_required",
                message="active lease is required for every patch path",
                details={"missing_paths": missing},
            )
            raise AgentStateError("claim_required", "active lease is required for every patch path", {"conflict": conflict})

    def _require_running_session(self, session_id: str) -> dict[str, Any]:
        session = self.adapter.get_session(session_id)
        if session is None:
            raise AgentStateError("session_not_found", "session was not found", {"session_id": session_id})
        if session.get("status") not in {"running", "blocked"}:
            raise AgentStateError(
                "session_not_active",
                "session is not active",
                {"session_id": session_id, "status": session.get("status")},
            )
        return session

    def _open_conflict(
        self,
        *,
        session_id: str,
        resource: str,
        conflict_type: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        conflict = {
            "kind": "conflict",
            "conflict_id": new_id("con"),
            "session_id": session_id,
            "resource": resource,
            "type": conflict_type,
            "severity": "blocking",
            "sessions": [session_id],
            "paths": [parse_resource(resource).path] if parse_resource(resource).path else [],
            "message": message,
            "resolution": None,
            "status": "open",
            "created_at": _iso(now),
            "resolved_at": None,
            "details": details or {},
        }
        created = self.adapter.open_conflict(conflict)
        self._event("conflict.detected", session_id=session_id, resource=resource, details=created)
        return created

    def _event(
        self,
        event: str,
        *,
        session_id: str | None = None,
        actor: str | None = None,
        resource: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.adapter.append_event(
            {
                "event_id": new_id("evt"),
                "event": event,
                "session_id": session_id,
                "actor": actor,
                "resource": resource,
                "recorded_at": _iso(utcnow()),
                "details": details or {},
            }
        )


def _resolve_expected_base(base, intent_expected, record_resource: str) -> dict[str, Any]:
    """Pull an {head_seq, head_oid} base out of either a FLAT dict or a NESTED
    {record: {...}} dict, from the `base=` param first then the intent's stored expected_base.

    Both entry points historically accepted different shapes, so a flat dict passed where a nested
    one was expected (or vice-versa) silently disabled the base_moved check. Accept both here so
    the stale-write guarantee never fails open on shape alone.
    """
    for candidate in (base, intent_expected):
        norm = _normalize_base(candidate, record_resource)
        if norm:
            return norm
    return {}


def _normalize_base(candidate, record_resource: str) -> dict[str, Any]:
    if not isinstance(candidate, dict) or not candidate:
        return {}
    # flat shape: has head_seq / head_oid directly
    if "head_seq" in candidate or "head_oid" in candidate:
        return {"head_seq": candidate.get("head_seq"), "head_oid": candidate.get("head_oid")}
    # nested shape: keyed by the record resource
    inner = candidate.get(record_resource)
    if isinstance(inner, dict) and ("head_seq" in inner or "head_oid" in inner):
        return {"head_seq": inner.get("head_seq"), "head_oid": inner.get("head_oid")}
    return {}


def _payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _iso(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_record(record: str) -> RecordRef:
    if ":" not in record:
        raise AgentStateError("bad_record", "record must look like collection:id")
    collection, record_id = record.split(":", 1)
    if not collection or not record_id:
        raise AgentStateError("bad_record", "record must look like collection:id")
    return RecordRef(collection, record_id)


def _safe_branch_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._/-]+", "-", str(value)).strip("-/.")
    cleaned = cleaned.replace("..", ".").replace("//", "/")
    return cleaned[:40] or "item"
