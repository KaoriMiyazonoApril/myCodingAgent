"""Abstract boundary that keeps agent code independent of any SDK."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from .errors import LLMStreamingNotImplementedError
from .types import LLMEvent, LLMRequest, LLMResponse, ProviderCapabilities


class LLMProvider(ABC):
    """A provider that accepts and returns only local model-layer types."""

    capabilities = ProviderCapabilities()

    @abstractmethod
    async def chat(self, request: LLMRequest) -> LLMResponse:
        """Perform one non-streaming chat completion."""

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        """Yield local streaming events when a future provider supports it."""
        raise LLMStreamingNotImplementedError("Streaming is not implemented yet")
        yield  # Makes this an async generator for the declared interface.

    async def close(self) -> None:
        """Release one Turn-scoped provider lease when the adapter owns one."""

        return None
