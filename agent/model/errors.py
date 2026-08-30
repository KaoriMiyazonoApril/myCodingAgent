"""Exceptions exposed by the local LLM abstraction, never SDK exceptions."""


class LLMError(Exception):
    """Base error with stable retry metadata independent of an SDK."""

    default_retryable = False

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool | None = None,
        provider: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = self.default_retryable if retryable is None else retryable
        self.provider = provider


class LLMConfigurationError(LLMError):
    """The local provider configuration is invalid or incomplete."""


class LLMConnectionError(LLMError):
    """The provider could not be reached or timed out."""

    default_retryable = True


class LLMAuthenticationError(LLMError):
    """The provider rejected the supplied API key."""


class LLMRateLimitError(LLMError):
    """The provider refused the request because of a rate limit."""

    default_retryable = True


class LLMRequestError(LLMError):
    """The provider rejected a request, for example because of a model name."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool | None = None,
        provider: str | None = None,
    ) -> None:
        if retryable is None:
            retryable = status_code is not None and status_code >= 500
        super().__init__(
            message,
            status_code=status_code,
            retryable=retryable,
            provider=provider,
        )


class LLMResponseParseError(LLMError):
    """An OpenAI-compatible response did not have the expected structure."""


class LLMToolArgumentsParseError(LLMResponseParseError):
    """A tool call's ``function.arguments`` was not a JSON object."""


class LLMStreamingNotImplementedError(LLMError):
    """Streaming event types exist, but this first implementation is non-streaming."""

    def __init__(self, message: str, *, fallback_response=None) -> None:
        super().__init__(message)
        self.fallback_response = fallback_response
