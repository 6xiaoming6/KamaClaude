from __future__ import annotations

from kama_claude.core.checkpoint.checkpoint import RunCheckpoint
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.provider import AnthropicProvider, DeepseekProvider, OpenaiProvider
from kama_claude.core.trace.provider import TracingProvider
from kama_claude.core.trace.writer import TraceWriter


class ProviderFactory:
    def __init__(
        self, provider: LLMProvider | None, default_model: str,
        trace: TraceWriter | None = None, *, include_payload: bool = True,
    ) -> None:
        self._provider = provider
        self._default_model = default_model
        self._trace = trace
        self._include_payload = include_payload

    def identity(self) -> tuple[str, str]:
        provider = self._provider
        if provider is None or isinstance(provider, DeepseekProvider):
            name = "deepseek"
        elif isinstance(provider, AnthropicProvider):
            name = "anthropic"
        elif isinstance(provider, OpenaiProvider):
            name = "openai"
        else:
            name = "custom"
        return name, str(getattr(provider, "_model", self._default_model))


    def create(self, run: RunCheckpoint, *, resuming: bool) -> LLMProvider:
        provider_name, model = self.identity()
        if resuming and (
            provider_name != run.provider
            or (self._provider is not None and model != run.model)
        ):
            run.reason = "provider or model changed; restore the original backend"
            raise RuntimeError(run.reason)
        return self.for_model(run.model)


    def for_model(self, model: str) -> LLMProvider:
        provider = self._provider
        try:
            if provider is None:
                provider = DeepseekProvider(model)
            elif model != self.identity()[1]:
                if isinstance(provider, DeepseekProvider):
                    provider = DeepseekProvider(model, client=provider._client)
                elif isinstance(provider, AnthropicProvider):
                    provider = AnthropicProvider(model, client=provider._client)
                elif isinstance(provider, OpenaiProvider):
                    provider = OpenaiProvider(model, client=provider._client)
                else:
                    raise ValueError("custom provider does not support a model override")
        except SystemExit as exc:
            raise RuntimeError(str(exc)) from exc
        if self._trace is not None:
            provider = TracingProvider(
                provider, self._trace, include_payload=self._include_payload,
            )
        return provider
