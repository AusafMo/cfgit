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
                    "You summarize runtime-control-plane diffs. Use only the provided JSON. "
                    "Return concise JSON with keys summary, behavior_change, blast_radius, "
                    "risk_level, rollback_note, unknowns."
                ),
            },
            {"role": "user", "content": json_dumps(payload)},
        ]
        return await self.complete(
            messages,
            temperature=kwargs.get("temperature", 0.1),
            max_tokens=kwargs.get("max_tokens", 900),
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
