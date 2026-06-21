# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""OpenAI provider for cfg-impact narration."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from cfg_impact.providers.base import (
    BaseImpactProvider,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
)

logger = logging.getLogger(__name__)

OPENAI_API_BASE = "https://api.openai.com/v1"


class OpenAIProvider(BaseImpactProvider):
    def __init__(self, api_key: str | None = None, model: str | None = None, **kwargs: Any):
        super().__init__(api_key=api_key or os.getenv("OPENAI_API_KEY", ""), model=model, **kwargs)

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def default_model(self) -> str:
        return os.getenv("CFGIT_OPENAI_MODEL", "gpt-4o-mini")

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if self.model.startswith(("o1", "o3")):
            if kwargs.get("max_tokens") is not None:
                payload["max_completion_tokens"] = kwargs["max_tokens"]
        else:
            if kwargs.get("temperature") is not None:
                payload["temperature"] = kwargs["temperature"]
            if kwargs.get("max_tokens") is not None:
                payload["max_tokens"] = kwargs["max_tokens"]

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                response = await client.post(
                    f"{OPENAI_API_BASE}/chat/completions",
                    json=payload,
                    headers=headers,
                )
            if response.status_code == 401:
                raise ProviderAuthError("invalid OpenAI API key", provider="openai")
            if response.status_code == 429:
                raise ProviderRateLimitError("OpenAI rate limit exceeded", provider="openai")
            if response.status_code != 200:
                raise ProviderError(
                    f"OpenAI API error ({response.status_code}): {response.text[:500]}",
                    provider="openai",
                    model=self.model,
                )
            data = response.json()
            choice = (data.get("choices") or [{}])[0]
            return {
                "content": ((choice.get("message") or {}).get("content") or ""),
                "usage": data.get("usage") or {},
                "model": data.get("model") or self.model,
                "stop_reason": choice.get("finish_reason"),
            }
        except ProviderError:
            raise
        except json.JSONDecodeError as exc:
            raise ProviderError("OpenAI returned invalid JSON", provider="openai", model=self.model) from exc
        except Exception as exc:
            logger.error("OpenAI impact provider error: %s", exc)
            raise ProviderError(str(exc), provider="openai", model=self.model) from exc
