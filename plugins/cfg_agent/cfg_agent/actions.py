from __future__ import annotations

from typing import Any

from cfg_agent.coordinator import AgentCoordinator
from cfg_agent.state import AgentStateError, InMemoryAgentStateAdapter

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFLICT = 6


def envelope(data: Any, *, code: int = EXIT_OK, message: str = "") -> dict[str, Any]:
    return {"status": "ok" if code == EXIT_OK else "error", "code": code, "message": message, "data": data}


def error_envelope(exc: AgentStateError) -> dict[str, Any]:
    code = EXIT_CONFLICT if exc.code.endswith("conflict") or exc.code == "lease_conflict" else EXIT_ERROR
    return envelope({"error": exc.code, **exc.details}, code=code, message=exc.message)


class AgentActions:
    def __init__(self, coordinator: AgentCoordinator | None = None) -> None:
        self.coordinator = coordinator or AgentCoordinator(InMemoryAgentStateAdapter())

    def start_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call(
            self.coordinator.start_session,
            task=str(payload.get("task") or "agent task"),
            agent_id=str(payload.get("agent_id") or "agent"),
            agent_kind=str(payload.get("agent_kind") or "custom"),
            actor=payload.get("actor"),
            tool_client=str(payload.get("tool_client") or "mcp"),
            metadata=payload.get("metadata") or {},
        )

    def heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call(self.coordinator.heartbeat, str(payload["session_id"]))

    def end_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call(
            self.coordinator.end_session,
            str(payload["session_id"]),
            status=str(payload.get("status") or "completed"),
            summary=payload.get("summary"),
        )

    def claim(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call(
            self.coordinator.claim,
            session_id=str(payload["session_id"]),
            resource=str(payload["resource"]),
            ttl_seconds=int(payload["ttl_seconds"]) if payload.get("ttl_seconds") else None,
            reason=str(payload.get("reason") or ""),
        )

    def release(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call(
            self.coordinator.release,
            session_id=str(payload["session_id"]),
            lease_id=str(payload["lease_id"]),
        )

    def open_intent(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call(
            self.coordinator.open_intent,
            session_id=str(payload["session_id"]),
            resources=list(payload.get("resources") or []),
            summary=str(payload.get("summary") or ""),
            planned_paths=list(payload.get("planned_paths") or []),
            risk_level=str(payload.get("risk_level") or "medium"),
            expected_base=payload.get("expected_base") or {},
            idempotency_key=payload.get("idempotency_key"),
        )

    def validate_patch(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call(
            self.coordinator.validate_patch,
            engine=payload["engine"],
            session_id=str(payload["session_id"]),
            record=str(payload["record"]),
            patch=list(payload.get("patch") or []),
            intent_id=str(payload["intent_id"]),
            base=payload.get("base"),
            allow_live_drift=bool(payload.get("allow_live_drift", False)),
        )

    def apply_patch(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call(
            self.coordinator.apply_patch,
            engine=payload["engine"],
            session_id=str(payload["session_id"]),
            record=str(payload["record"]),
            patch=list(payload.get("patch") or []),
            intent_id=str(payload["intent_id"]),
            message=str(payload["message"]),
            base=payload.get("base"),
            idempotency_key=payload.get("idempotency_key"),
            allow_live_drift=bool(payload.get("allow_live_drift", False)),
        )

    def close_intent(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call(
            self.coordinator.close_intent,
            session_id=str(payload["session_id"]),
            intent_id=str(payload["intent_id"]),
            status=str(payload["status"]),
        )

    def status(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call(self.coordinator.status, payload.get("session_id"))

    def conflicts(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call(self.coordinator.conflicts, payload.get("status"))

    def watch(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call(
            self.coordinator.watch,
            since_event_id=payload.get("since_event_id"),
            limit=int(payload.get("limit") or 100),
        )

    @staticmethod
    def _call(fn, *args, **kwargs) -> dict[str, Any]:
        try:
            return envelope(fn(*args, **kwargs))
        except AgentStateError as exc:
            return error_envelope(exc)
        except (KeyError, TypeError, ValueError) as exc:
            return envelope({"error": "bad_request"}, code=EXIT_ERROR, message=str(exc))
