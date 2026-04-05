"""Provider registry — loads providers from config, handles model routing."""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Optional

from .base import BaseProvider, ModelInfo, NormalizedMessage, StreamEvent

logger = logging.getLogger(__name__)

_registry: Optional["ProviderRegistry"] = None


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, BaseProvider] = {}

    def register(self, provider: BaseProvider) -> None:
        self._providers[provider.name] = provider

    def from_config(self, cfg: Any) -> None:
        """Instantiate and register providers from config object."""
        from .anthropic_provider import AnthropicProvider
        from .openai_provider import OpenAIProvider

        pcfg = getattr(cfg, "providers", None)
        if not pcfg:
            return

        # Anthropic
        acfg = getattr(pcfg, "anthropic", None)
        if acfg and getattr(acfg, "enabled", False):
            key = getattr(acfg, "api_key", "")
            if key:
                self.register(AnthropicProvider(key))
                logger.info("Registered Anthropic provider")

        # OpenAI
        ocfg = getattr(pcfg, "openai", None)
        if ocfg and getattr(ocfg, "enabled", False):
            self.register(OpenAIProvider(
                api_key=getattr(ocfg, "api_key", ""),
                base_url=getattr(ocfg, "base_url", None),
                name="openai",
            ))
            logger.info("Registered OpenAI provider")

        # Gemini (via Google's OpenAI-compat endpoint)
        gcfg = getattr(pcfg, "gemini", None)
        if gcfg and getattr(gcfg, "enabled", False):
            self.register(OpenAIProvider(
                api_key=getattr(gcfg, "api_key", ""),
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                name="gemini",
            ))
            logger.info("Registered Gemini provider")

        # Ollama
        ollcfg = getattr(pcfg, "ollama", None)
        if ollcfg and getattr(ollcfg, "enabled", False):
            base = getattr(ollcfg, "base_url", "http://localhost:11434")
            self.register(OpenAIProvider(
                api_key="ollama",
                base_url=f"{base.rstrip('/')}/v1",
                name="ollama",
            ))
            logger.info("Registered Ollama provider at %s", base)

        # Custom OpenAI-compat endpoints
        custom = getattr(pcfg, "custom", []) or []
        for ccfg in custom:
            if not getattr(ccfg, "enabled", False):
                continue
            name = getattr(ccfg, "name", "custom")
            self.register(OpenAIProvider(
                api_key=getattr(ccfg, "api_key", ""),
                base_url=getattr(ccfg, "base_url", ""),
                name=name,
            ))
            logger.info("Registered custom provider '%s'", name)

    async def list_models(self) -> list[ModelInfo]:
        """Collect models from all registered providers."""
        models: list[ModelInfo] = []
        for provider in self._providers.values():
            if not provider.is_available():
                continue
            try:
                models.extend(await provider.list_models())
            except Exception as exc:
                logger.warning("list_models failed for %s: %s", provider.name, exc)
        return models

    def resolve(self, model_id: str) -> tuple[BaseProvider, str]:
        """
        Parse "provider/model" or bare "model".
        Returns (provider, model_name).
        """
        if "/" in model_id:
            provider_name, model_name = model_id.split("/", 1)
        else:
            # Guess: use first available provider
            provider_name = next(iter(self._providers), "")
            model_name = model_id

        provider = self._providers.get(provider_name)
        if not provider:
            available = list(self._providers.keys())
            raise ValueError(
                f"Provider '{provider_name}' not configured. "
                f"Available: {available}"
            )
        return provider, model_name

    async def stream(
        self,
        model_id: str,
        messages: list[NormalizedMessage],
        tools: list[dict],
        system: str = "",
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        provider, model_name = self.resolve(model_id)
        async for event in provider.stream(messages, tools, model_name, system, **kwargs):
            yield event

    @property
    def provider_names(self) -> list[str]:
        return list(self._providers.keys())


def get_registry() -> ProviderRegistry:
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry
