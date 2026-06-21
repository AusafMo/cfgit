# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""Google Gemini provider for cfg-impact narration."""
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

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider(BaseImpactProvider):
    def __init__(self, api_key: str | None = None, model: str | None = None, **kwargs: Any):
        super().__init__(
            api_key=api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", ""),
            model=model,
            **kwargs,
        )

    @property
    def provider_name(self) -> str:
        return "gemini"

    @property
    def default_model(self) -> str:
        return os.getenv("CFGIT_GEMINI_MODEL", "gemini-3.5-flash")

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        contents, system_instruction = _format_messages(messages)
        payload: dict[str, Any] = {"contents": contents}
        if system_instruction:
            payload["system_instruction"] = {"parts": [{"text": system_instruction}]}
        gen_config: dict[str, Any] = {}
        if kwargs.get("temperature") is not None:
            gen_config["temperature"] = kwargs["temperature"]
        if kwargs.get("max_tokens") is not None:
            gen_config["maxOutputTokens"] = kwargs["max_tokens"]
        if gen_config:
            payload["generationConfig"] = gen_config

        url = f"{GEMINI_API_BASE}/models/{self.model}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": self.api_key}

        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                response = await client.post(url, json=payload, headers=headers)
            if response.status_code in (401, 403):
                raise ProviderAuthError("invalid Google API key", provider="gemini")
            if response.status_code == 429:
                raise ProviderRateLimitError("Gemini rate limit exceeded", provider="gemini")
            if response.status_code != 200:
                raise ProviderError(
                    f"Gemini API error ({response.status_code}): {response.text[:500]}",
                    provider="gemini",
                    model=self.model,
                )
            data = response.json()
            candidate = (data.get("candidates") or [{}])[0]
            parts = ((candidate.get("content") or {}).get("parts")) or []
            content = "".join(part.get("text", "") for part in parts)
            usage = data.get("usageMetadata") or {}
            return {
                "content": content,
                "usage": {
                    "input_tokens": usage.get("promptTokenCount"),
                    "output_tokens": usage.get("candidatesTokenCount"),
                    "total_tokens": usage.get("totalTokenCount"),
                },
                "model": self.model,
                "stop_reason": candidate.get("finishReason"),
            }
        except ProviderError:
            raise
        except json.JSONDecodeError as exc:
            raise ProviderError("Gemini returned invalid JSON", provider="gemini", model=self.model) from exc
        except Exception as exc:
            logger.error("Gemini impact provider error: %s", exc)
            raise ProviderError(str(exc), provider="gemini", model=self.model) from exc


def _format_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    contents: list[dict[str, Any]] = []
    system_instruction = None
    for msg in messages:
        role = msg.get("role")
        text = msg.get("content", "")
        if role == "system":
            system_instruction = text
            continue
        contents.append(
            {"role": "model" if role == "assistant" else "user", "parts": [{"text": text}]}
        )
    return contents, system_instruction
