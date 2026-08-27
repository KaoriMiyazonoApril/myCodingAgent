"""Exceptions exposed by the local LLM abstraction, never SDK exceptions."""


class LLMError(Exception):
    """Base class for errors from the local LLM connection layer."""


class LLMConfigurationError(LLMError):
    """The local provider configuration is invalid or incomplete."""


class LLMConnectionError(LLMError):
    """The provider could not be reached or timed out."""


class LLMAuthenticationError(LLMError):
    """The provider rejected the supplied API key."""


class LLMRateLimitError(LLMError):
    """The provider refused the request because of a rate limit."""


class LLMRequestError(LLMError):
    """The provider rejected a request, for example because of a model name."""


class LLMResponseParseError(LLMError):
    """An OpenAI-compatible response did not have the expected structure."""


class LLMToolArgumentsParseError(LLMResponseParseError):
    """A tool call's ``function.arguments`` was not a JSON object."""


class LLMStreamingNotImplementedError(LLMError):
    """Streaming event types exist, but this first implementation is non-streaming."""
