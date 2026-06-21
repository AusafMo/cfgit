# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""Lean provider interface for cfg-impact narration."""
from __future__ import annotations

from abc import ABC, abstractmethod
import json
from typing import Any


class BaseImpactProvider(ABC):
    def __init__(self, api_key: str, model: str | None = None, **_: Any):
        if not api_key:
            raise ValueError(f"{self.__class__.__name__} requires an API key")
        self.api_key = api_key
        self.model = model or self.default_model

    @property
    @abstractmethod
    def provider_name(self) -> str:
        ...

    @property
    @abstractmethod
    def default_model(self) -> str:
        ...

    @abstractmethod
    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        ...

    async def narrate(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        json_dumps = kwargs.get(
            "json_dumps",
            lambda value: json.dumps(value, indent=2, sort_keys=True),
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a senior engineer reviewing a change to a live agent/config control "
                    "plane. The JSON gives you `field_diffs` (the ACTUAL before/after of each changed "
                    "field) and `system` (a compact map of the OTHER live configs: their ids, roles, "
                    "instruction excerpts, tools, and contracts). If `system.scoped` is true, reason "
                    "about the change only against the selected records in `system.configs`; otherwise "
                    "reason about it against the whole system.\n"
                    "Be concrete. Say what the edit actually changes in behavior, quoting or paraphrasing "
                    "the specific rule, threshold, tool, or wording that changed. Then identify which OTHER "
                    "configs in `system` are affected and why (shared contracts, shared tools, hand-offs, "
                    "upstream/downstream roles, overlapping responsibilities), naming the specific "
                    "config_ids. Do NOT say 'impact unknown' when the diff is present, read it. Only list a "
                    "genuine unknown if it truly cannot be inferred from the provided data.\n"
                    "Return concise JSON with keys: summary (one concrete sentence), behavior_change (what "
                    "the agent will now do differently, specifically), blast_radius (which named configs or "
                    "consumers are affected and why), risk_level (low|medium|high), rollback_note, unknowns "
                    "(array of only real unknowns)."
                ),
            },
            {"role": "user", "content": json_dumps(payload)},
        ]
        return await self.complete(
            messages,
            temperature=kwargs.get("temperature", 0.1),
            max_tokens=kwargs.get("max_tokens", 1100),
        )


class ProviderError(Exception):
    def __init__(self, message: str, provider: str | None = None, model: str | None = None):
        super().__init__(message)
        self.provider = provider
        self.model = model


class ProviderRateLimitError(ProviderError):
    pass


class ProviderAuthError(ProviderError):
    pass
