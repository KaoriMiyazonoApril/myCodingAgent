"""Provider-independent context estimation and budget policy.

The model context boundary deliberately lives below ``ModelInvoker``.  A
provider can replace :class:`TokenEstimator` later, while the policy keeps
the output reserve, safety margin and pressure labels stable for every
provider.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json
import math

from agent.core.messages import (
    Message,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from agent.tools.types import ToolDefinition

from .errors import ContextLimitError


DEFAULT_CONTEXT_WINDOW_TOKENS = 32_000
DEFAULT_SOFT_THRESHOLD = 0.8
DEFAULT_SAFETY_MARGIN_TOKENS = 256


def _json_text(value: object) -> str:
    """Serialize metadata/schema values without allowing estimation to fail."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=repr,
        )
    except (TypeError, ValueError):
        return repr(value)


class TokenEstimator:
    """Estimate a complete model request without depending on a tokenizer.

    ASCII text is counted at roughly four characters per token.  CJK and
    other non-ASCII characters receive a more conservative per-character
    weight, while message/block/tool framing is counted explicitly.  This is
    intentionally a useful estimate rather than a claim of provider-token
    exactness.
    """

    # Public constants make the deliberately approximate framing visible and
    # easy to tune in focused tests or a future provider-specific adapter.
    ASCII_CHARS_PER_TOKEN = 4.0
    CJK_CHARS_PER_TOKEN = 1.5
    OTHER_NON_ASCII_CHARS_PER_TOKEN = 2.0
    REQUEST_OVERHEAD_TOKENS = 16
    MESSAGE_OVERHEAD_TOKENS = 4
    BLOCK_OVERHEAD_TOKENS = 2
    TOOL_OVERHEAD_TOKENS = 8

    def estimate(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
    ) -> int:
        """Estimate messages, tool definitions and request framing."""

        message_values = list(messages)
        tool_values = list(tools)
        total = self.REQUEST_OVERHEAD_TOKENS
        total += len(message_values) * self.MESSAGE_OVERHEAD_TOKENS
        for message in message_values:
            # The role is part of the wire request even when a provider later
            # chooses a compact representation for a particular block.
            total += self._text_tokens(message.role)
            total += len(message.content) * self.BLOCK_OVERHEAD_TOKENS
            for block in message.content:
                total += self._block_tokens(block)
        total += len(tool_values) * self.TOOL_OVERHEAD_TOKENS
        for tool in tool_values:
            total += self._text_tokens(tool.name)
            total += self._text_tokens(tool.description)
            total += self._text_tokens(_json_text(tool.parameters))
        return max(0, int(total))

    def estimate_request(self, request: object) -> int:
        """Estimate an ``LLMRequest``-shaped object at the public seam."""

        messages = getattr(request, "messages", None)
        tools = getattr(request, "tools", None) or ()
        if messages is None:
            raise ValueError("request must provide messages")
        estimate = self.estimate(messages, tools)
        # These fields are request envelope values as well.  They are small,
        # but counting them prevents an apparently fitting request from
        # silently omitting provider framing.
        estimate += self._text_tokens(str(getattr(request, "temperature", "")))
        estimate += self._text_tokens(str(getattr(request, "max_tokens", "")))
        extra_body = getattr(request, "extra_body", None)
        if extra_body:
            estimate += self._text_tokens(_json_text(extra_body))
        return estimate

    def estimate_messages(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
    ) -> int:
        """Alias retained for callers that name the message boundary."""

        return self.estimate(messages, tools)

    def estimate_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
    ) -> int:
        """Alias used by history policies and provider adapters."""

        return self.estimate(messages, tools)

    def _block_tokens(self, block: object) -> int:
        if isinstance(block, (TextBlock, ReasoningBlock)):
            return self._text_tokens(block.text)
        if isinstance(block, ToolCallBlock):
            arguments = (
                block.raw_arguments
                if block.raw_arguments is not None
                else _json_text(block.arguments)
            )
            return (
                self._text_tokens(block.id)
                + self._text_tokens(block.name)
                + self._text_tokens(arguments)
                + self._text_tokens(block.arguments_error or "")
            )
        if isinstance(block, ToolResultBlock):
            return (
                self._text_tokens(block.tool_call_id)
                + self._text_tokens(block.content)
                + self._text_tokens(_json_text(block.metadata))
                + self._text_tokens(block.error_code or "")
            )
        # Message validation normally makes this unreachable.  Keeping a
        # deterministic fallback makes the estimator safe for detached test
        # adapters that carry a compatible block object.
        return self._text_tokens(_json_text(block))

    @classmethod
    def _text_tokens(cls, value: str) -> int:
        if not value:
            return 0
        ascii_count = 0
        cjk_count = 0
        other_count = 0
        for character in value:
            codepoint = ord(character)
            if codepoint < 128:
                ascii_count += 1
            elif (
                0x3400 <= codepoint <= 0x4DBF
                or 0x4E00 <= codepoint <= 0x9FFF
                or 0xF900 <= codepoint <= 0xFAFF
                or 0x20000 <= codepoint <= 0x3FFFF
            ):
                cjk_count += 1
            else:
                other_count += 1
        return max(
            1,
            math.ceil(ascii_count / cls.ASCII_CHARS_PER_TOKEN)
            + math.ceil(cjk_count / cls.CJK_CHARS_PER_TOKEN)
            + math.ceil(other_count / cls.OTHER_NON_ASCII_CHARS_PER_TOKEN),
        )


@dataclass(frozen=True, slots=True)
class BudgetAssessment:
    """Explain one estimate's pressure and fit against a policy."""

    estimated_input_tokens: int
    context_window_tokens: int
    reserved_output_tokens: int
    safety_margin_tokens: int
    usable_input_tokens: int
    soft_limit_tokens: int
    pressure: str
    fits: bool

    @property
    def overflow(self) -> bool:
        return not self.fits


class ContextBudgetPolicy:
    """Centralize context-window, reserve and pressure decisions."""

    def __init__(
        self,
        *,
        context_window_tokens: int,
        output_tokens: int | None = None,
        output_reserve_tokens: int | None = None,
        safety_margin_tokens: int = DEFAULT_SAFETY_MARGIN_TOKENS,
        safety_margin: int | None = None,
        soft_threshold: float = DEFAULT_SOFT_THRESHOLD,
        estimator: TokenEstimator | None = None,
    ) -> None:
        _positive_int(context_window_tokens, "context_window_tokens")
        if output_tokens is not None and output_reserve_tokens is not None:
            raise ValueError("provide only one output reserve setting")
        reserve = output_tokens if output_tokens is not None else output_reserve_tokens
        if reserve is not None:
            _positive_int(reserve, "output_reserve_tokens")
        else:
            reserve = max(1, min(4096, context_window_tokens // 4))
        if safety_margin is not None:
            if safety_margin_tokens != DEFAULT_SAFETY_MARGIN_TOKENS:
                raise ValueError("provide only one safety margin setting")
            safety_margin_tokens = safety_margin
        _non_negative_int(safety_margin_tokens, "safety_margin_tokens")
        if (
            isinstance(soft_threshold, bool)
            or not isinstance(soft_threshold, (float, int))
            or not 0 < soft_threshold <= 1
        ):
            raise ValueError("soft_threshold must be greater than 0 and at most 1")
        if estimator is not None and not isinstance(estimator, TokenEstimator):
            # A provider adapter may implement the same callable surface.  Do
            # not require inheritance, but fail early for accidental values.
            if not callable(getattr(estimator, "estimate", None)):
                raise ValueError("estimator must provide estimate(messages, tools)")
        self._context_window_tokens = context_window_tokens
        self._output_reserve = reserve
        self._safety_margin = safety_margin_tokens
        self._soft_threshold = float(soft_threshold)
        self.estimator = estimator or TokenEstimator()

    @property
    def context_window_tokens(self) -> int:
        return self._context_window_tokens

    @property
    def output_reserve_tokens(self) -> int:
        return self._output_reserve

    @property
    def reserved_output_tokens(self) -> int:
        return self._output_reserve

    @property
    def safety_margin_tokens(self) -> int:
        return self._safety_margin

    @property
    def soft_threshold(self) -> float:
        return self._soft_threshold

    @property
    def input_budget_tokens(self) -> int:
        return max(
            0,
            self._context_window_tokens
            - self._output_reserve
            - self._safety_margin,
        )

    @property
    def usable_input_tokens(self) -> int:
        return self.input_budget_tokens

    @property
    def soft_limit_tokens(self) -> int:
        return math.floor(self.input_budget_tokens * self._soft_threshold)

    def estimate_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
    ) -> int:
        for name in ("estimate_messages", "estimate", "estimate_tokens"):
            method = getattr(self.estimator, name, None)
            if not callable(method):
                continue
            for args, kwargs in (
                ((messages,), {"tools": tools}),
                ((messages, tools), {}),
                ((messages,), {}),
            ):
                try:
                    estimate = method(*args, **kwargs)
                except TypeError:
                    continue
                if (
                    isinstance(estimate, int)
                    and not isinstance(estimate, bool)
                    and estimate >= 0
                ):
                    return estimate
                break
        if callable(self.estimator):
            estimate = self.estimator(messages, tools)
            if isinstance(estimate, int) and not isinstance(estimate, bool) and estimate >= 0:
                return estimate
        raise ValueError("estimator must return a non-negative integer")

    def assess(self, estimated_input_tokens: int) -> BudgetAssessment:
        _non_negative_int(estimated_input_tokens, "estimated_input_tokens")
        usable = self.input_budget_tokens
        soft_limit = self.soft_limit_tokens
        if usable <= 0 or estimated_input_tokens >= usable:
            pressure = "hard"
        elif estimated_input_tokens >= soft_limit:
            pressure = "soft"
        else:
            pressure = "normal"
        return BudgetAssessment(
            estimated_input_tokens=estimated_input_tokens,
            context_window_tokens=self.context_window_tokens,
            reserved_output_tokens=self.output_reserve_tokens,
            safety_margin_tokens=self.safety_margin_tokens,
            usable_input_tokens=usable,
            soft_limit_tokens=soft_limit,
            pressure=pressure,
            fits=usable > 0 and estimated_input_tokens <= usable,
        )

    def pressure_for(self, estimated_input_tokens: int) -> str:
        return self.assess(estimated_input_tokens).pressure

    def ensure_fits(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> BudgetAssessment:
        assessment = self.assess(self.estimate_tokens(messages, tools))
        if not assessment.fits:
            raise ContextLimitError(
                "conversation exceeds the configured model context budget"
            )
        return assessment


class ContextBudget(ContextBudgetPolicy):
    """Backwards-compatible name for the V1 policy.

    Existing embedders used ``ContextBudget.estimate_tokens`` as a static
    helper.  Keep that helper while routing all new policy decisions through
    the replaceable estimator above.
    """

    def estimate_tokens(
        self_or_messages: ContextBudget | Sequence[Message],
        messages_or_tools: Sequence[Message] | Sequence[ToolDefinition] = (),
        tools: Sequence[ToolDefinition] = (),
    ) -> int:
        # ``ContextBudget.estimate_tokens(messages, tools)`` was a documented
        # helper in the old class.  Supporting that unbound call while also
        # honoring an instance-injected estimator keeps both seams stable.
        if isinstance(self_or_messages, ContextBudget):
            return ContextBudgetPolicy.estimate_tokens(
                self_or_messages, messages_or_tools, tools
            )
        return TokenEstimator().estimate(self_or_messages, messages_or_tools)


def _positive_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _non_negative_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


__all__ = [
    "BudgetAssessment",
    "ContextBudget",
    "ContextBudgetPolicy",
    "DEFAULT_CONTEXT_WINDOW_TOKENS",
    "TokenEstimator",
]
