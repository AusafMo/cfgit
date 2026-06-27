# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""Claude provider for cfgit-impact narration."""
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

CLAUDE_API_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"


class ClaudeProvider(BaseImpactProvider):
    def __init__(self, api_key: str | None = None, model: str | None = None, **kwargs: Any):
        super().__init__(api_key=api_key or os.getenv("ANTHROPIC_API_KEY", ""), model=model, **kwargs)

    @property
    def provider_name(self) -> str:
        return "claude"

    @property
    def default_model(self) -> str:
        return os.getenv("CFGIT_CLAUDE_MODEL", "claude-sonnet-4-20250514")

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        formatted, system_prompt = _format_messages(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": formatted,
            "max_tokens": kwargs.get("max_tokens", 900),
        }
        if system_prompt:
            payload["system"] = system_prompt
        if kwargs.get("temperature") is not None:
            payload["temperature"] = kwargs["temperature"]

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }

        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                response = await client.post(
                    f"{CLAUDE_API_BASE}/messages",
                    json=payload,
                    headers=headers,
                )
            if response.status_code == 401:
                raise ProviderAuthError("invalid Anthropic API key", provider="claude")
            if response.status_code == 429:
                raise ProviderRateLimitError("Anthropic rate limit exceeded", provider="claude")
            if response.status_code != 200:
                raise ProviderError(
                    f"Claude API error ({response.status_code}): {response.text[:500]}",
                    provider="claude",
                    model=self.model,
                )
            data = response.json()
            content = "".join(
                block.get("text", "")
                for block in data.get("content", [])
                if block.get("type") == "text"
            )
            return {
                "content": content,
                "usage": data.get("usage") or {},
                "model": data.get("model") or self.model,
                "stop_reason": data.get("stop_reason"),
            }
        except ProviderError:
            raise
        except json.JSONDecodeError as exc:
            raise ProviderError("Claude returned invalid JSON", provider="claude", model=self.model) from exc
        except Exception as exc:
            logger.error("Claude impact provider error: %s", exc)
            raise ProviderError(str(exc), provider="claude", model=self.model) from exc


def _format_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    formatted: list[dict[str, Any]] = []
    system_prompt = None
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "system":
            system_prompt = content
            continue
        formatted.append({"role": "assistant" if role == "assistant" else "user", "content": content})
    return formatted, system_prompt
