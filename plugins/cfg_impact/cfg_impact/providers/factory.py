# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""Factory for cfg-impact LLM providers."""
from __future__ import annotations

from typing import Any

from cfg_impact.providers.base import BaseImpactProvider
from cfg_impact.providers.claude import ClaudeProvider
from cfg_impact.providers.gemini import GeminiProvider
from cfg_impact.providers.openai_provider import OpenAIProvider


class ImpactProviderFactory:
    _providers = {
        "claude": ClaudeProvider,
        "openai": OpenAIProvider,
        "gemini": GeminiProvider,
        "google": GeminiProvider,
    }

    @classmethod
    def create_provider(
        cls,
        provider: str,
        *,
        model: str | None = None,
        api_key: str | None = None,
        **kwargs: Any,
    ) -> BaseImpactProvider:
        provider_name = provider.lower()
        if provider_name not in cls._providers:
            raise ValueError(
                f"unsupported impact provider '{provider_name}'. "
                f"Available: {', '.join(sorted(cls._providers))}"
            )
        return cls._providers[provider_name](api_key=api_key, model=model, **kwargs)
