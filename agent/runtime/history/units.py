"""Atomic transcript units and deterministic history fingerprints."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import inspect
import json
from typing import Any, Protocol, TypeAlias

from agent.core.messages import (
    Message,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from agent.model.types import LLMRequest
from agent.tools.result_bounds import reduce_tool_result_block


DEFAULT_RECENT_TAIL_RATIO = 0.20
DEFAULT_TOOL_RESULT_PRUNE_THRESHOLD = 4096
COMPACTION_SUMMARY_SCHEMA_VERSION = 1
COMPACTION_CHECKPOINT_SCHEMA_VERSION = 1


def _validate_history(history: Sequence[Message] | Iterable[Message]) -> list[Message]:
    """Return a detached list and reject values outside the message seam."""

    try:
        messages = list(history)
    except TypeError as error:
        raise ValueError("history must be an iterable of Message") from error
    if any(not isinstance(message, Message) for message in messages):
        raise ValueError("history must contain only Message values")
    return deepcopy(messages)


def _call_ids(message: Message) -> tuple[str, ...]:
    if message.role != "assistant":
        return ()
    return tuple(
        block.id for block in message.content if isinstance(block, ToolCallBlock)
    )


def _result_ids(message: Message) -> tuple[str, ...]:
    if message.role != "tool":
        return ()
    return tuple(
        block.tool_call_id
        for block in message.content
        if isinstance(block, ToolResultBlock)
    )


@dataclass(frozen=True, slots=True)
class AtomicHistoryUnit:
    """One indivisible region of the canonical transcript.

    ``start_index`` and ``end_index`` are inclusive positions in the input
    history.  A unit containing tool calls includes all matching results; if a
    result is missing, the unit is marked open and extends to the end of the
    available transcript.  Extending an incomplete unit is intentional: it
    keeps a pending call together with any later evidence instead of allowing
    selection to expose an orphan.
    """

    messages: list[Message]
    start_index: int
    end_index: int
    tool_call_ids: tuple[str, ...] = ()
    result_ids: tuple[str, ...] = ()
    complete: bool = True

    def __post_init__(self) -> None:
        if (
            isinstance(self.start_index, bool)
            or not isinstance(self.start_index, int)
            or self.start_index < 0
            or isinstance(self.end_index, bool)
            or not isinstance(self.end_index, int)
            or self.end_index < self.start_index
        ):
            raise ValueError("atomic history indexes must be ordered integers")
        if not isinstance(self.complete, bool):
            raise ValueError("atomic history complete flag must be boolean")
        object.__setattr__(self, "messages", deepcopy(list(self.messages)))
        object.__setattr__(self, "tool_call_ids", tuple(self.tool_call_ids))
        object.__setattr__(self, "result_ids", tuple(self.result_ids))

    @property
    def open(self) -> bool:
        """Whether this interaction still has an unresolved call/result pair."""

        return not self.complete

    @property
    def closed(self) -> bool:
        return self.complete

    @property
    def is_open(self) -> bool:
        return self.open

    @property
    def is_tool_interaction(self) -> bool:
        return bool(self.tool_call_ids or self.result_ids)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_call_ids)

    @property
    def message_count(self) -> int:
        return len(self.messages)

    @property
    def canonical_start(self) -> int:
        return self.start_index

    @property
    def canonical_end(self) -> int:
        return self.end_index

    @property
    def canonical_start_index(self) -> int:
        return self.start_index

    @property
    def canonical_end_index(self) -> int:
        return self.end_index

    @property
    def start(self) -> int:
        return self.start_index

    @property
    def end(self) -> int:
        return self.end_index

    @property
    def is_complete(self) -> bool:
        return self.complete

    @property
    def is_closed(self) -> bool:
        return self.closed


def parse_atomic_history(history: Sequence[Message] | Iterable[Message]) -> list[AtomicHistoryUnit]:
    """Parse a transcript into protocol-safe atomic units.

    Ordinary messages are one unit.  An assistant message with one or more
    ``ToolCallBlock`` values is grouped with every subsequent message needed
    to find all corresponding ``ToolResultBlock`` values.  Tool results that
    have no matching call are retained as their own (open) unit rather than
    silently discarded.
    """

    messages = _validate_history(history)
    units: list[AtomicHistoryUnit] = []
    index = 0
    total = len(messages)
    while index < total:
        message = messages[index]
        call_ids = _call_ids(message)
        if call_ids:
            expected = set(call_ids)
            end = index
            probe = index + 1
            # Scan until every expected result is found.  The normal protocol
            # has contiguous tool messages; scanning farther also keeps a
            # pair atomic when a provider inserts an explanatory message.
            while expected and probe < total:
                found = set(_result_ids(messages[probe])) & expected
                if found:
                    expected -= found
                    end = probe
                probe += 1
            if expected:
                # No matching result exists yet.  Retain the entire available
                # continuation as one open interaction.
                end = total - 1
            grouped = messages[index : end + 1]
            result_ids = tuple(
                result_id
                for grouped_message in grouped
                for result_id in _result_ids(grouped_message)
                if result_id in set(call_ids)
            )
            units.append(
                AtomicHistoryUnit(
                    messages=grouped,
                    start_index=index,
                    end_index=end,
                    tool_call_ids=call_ids,
                    result_ids=result_ids,
                    complete=not expected,
                )
            )
            index = end + 1
            continue

        result_ids = _result_ids(message)
        units.append(
            AtomicHistoryUnit(
                messages=[message],
                start_index=index,
                end_index=index,
                result_ids=result_ids,
                # A result without a visible call is an unresolved protocol
                # fragment and is therefore protected by selectors/pruners.
                complete=not result_ids,
            )
        )
        index += 1
    return units


class AtomicHistoryParser:
    """Named adapter for callers that prefer an object seam."""

    @staticmethod
    def parse(history: Sequence[Message] | Iterable[Message]) -> list[AtomicHistoryUnit]:
        return parse_atomic_history(history)


AtomicInteractionUnit = AtomicHistoryUnit


class _MessageEstimator(Protocol):
    def estimate(self, messages: Sequence[Message], *args: Any, **kwargs: Any) -> int:
        """Estimate a complete message collection in model tokens."""


def _default_estimate(messages: Sequence[Message]) -> int:
    """Small provider-independent estimate used by reduction policies.

    This is intentionally character based rather than UTF-8 byte based.  It
    only needs to make deterministic policy decisions; a provider-specific
    estimator can be injected through the public seam.
    """

    # Keep this seam aligned with the centralized V1 policy whenever it is
    # available.  The local fallback below is retained for import-cycle-safe
    # use by lightweight embedders.
    try:
        from .context_budget import TokenEstimator

        return TokenEstimator().estimate(messages, ())
    except (ImportError, AttributeError, TypeError, ValueError):
        pass

    total = 2  # request envelope
    for message in messages:
        total += 4 + len(message.role)
        for block in message.content:
            total += 2
            if isinstance(block, (TextBlock, ReasoningBlock)):
                text = block.text
                total += sum(1 if ord(char) > 127 else 0.25 for char in text)
            elif isinstance(block, ToolCallBlock):
                total += len(block.id) + len(block.name) + 2
                try:
                    total += sum(
                        1 if ord(char) > 127 else 0.25
                        for char in json.dumps(
                            block.arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                    )
                except (TypeError, ValueError):
                    total += len(block.raw_arguments or "") * 0.25
            elif isinstance(block, ToolResultBlock):
                total += sum(1 if ord(char) > 127 else 0.25 for char in block.content)
                try:
                    total += len(json.dumps(block.metadata, ensure_ascii=False)) * 0.25
                except (TypeError, ValueError):
                    total += 8
                total += len(block.tool_call_id)
    return max(1, int(total + 0.999))


def estimate_history_tokens(
    messages: Sequence[Message], estimator: _MessageEstimator | Callable[..., int] | None = None
) -> int:
    """Call an injected estimator without imposing one concrete interface."""

    if estimator is None:
        return _default_estimate(messages)
    candidates: list[Callable[..., Any]] = []
    for name in ("estimate_messages", "estimate", "estimate_tokens"):
        method = getattr(estimator, name, None)
        if callable(method):
            candidates.append(method)
    if callable(estimator):
        candidates.append(estimator)
    for method in candidates:
        for args, kwargs in (
            ((list(messages),), {"tools": ()}),
            ((list(messages), ()), {}),
            ((list(messages),), {}),
        ):
            try:
                estimate = method(*args, **kwargs)
            except TypeError:
                continue
            if isinstance(estimate, int) and not isinstance(estimate, bool) and estimate >= 0:
                return estimate
            break
    return _default_estimate(messages)


def _fingerprint_block(block: object) -> dict[str, object]:
    """Return stable, provider-independent data for one message block."""

    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ReasoningBlock):
        return {"type": "reasoning", "text": block.text}
    if isinstance(block, ToolCallBlock):
        return {
            "type": "tool_call",
            "id": block.id,
            "name": block.name,
            "arguments": block.arguments,
            "arguments_error": block.arguments_error,
            "raw_arguments": block.raw_arguments,
        }
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_call_id": block.tool_call_id,
            "content": block.content,
            "metadata": block.metadata,
            "error_code": block.error_code,
        }
    raise ValueError(f"unsupported history block: {type(block).__name__}")


def _fingerprint_message(message: Message) -> dict[str, object]:
    return {
        "role": message.role,
        "content": [_fingerprint_block(block) for block in message.content],
    }


def canonical_history_fingerprint(
    history: Sequence[Message] | Iterable[Message],
    covered_through: int | None = None,
) -> str:
    """Hash the canonical prefix through an inclusive message position.

    The digest includes the endpoint and an algorithm tag so it can safely be
    persisted as an append-only anchor.  Appending messages leaves an existing
    prefix digest unchanged; editing, deleting or reordering that prefix does
    not.
    """

    messages = _validate_history(history)
    endpoint = len(messages) - 1 if covered_through is None else covered_through
    if (
        isinstance(endpoint, bool)
        or not isinstance(endpoint, int)
        or endpoint < -1
        or endpoint >= len(messages)
    ):
        raise ValueError("history fingerprint endpoint is outside canonical history")
    payload = {
        "algorithm": "sha256-canonical-history-v1",
        "covered_through": endpoint,
        "messages": [
            _fingerprint_message(message) for message in messages[: endpoint + 1]
        ],
    }
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("history contains non-serializable fingerprint data") from error
    return hashlib.sha256(encoded).hexdigest()


# Explicit aliases keep the purpose discoverable at call sites that refer to
# the digest as a prefix, coverage or append-only anchor.
canonical_prefix_fingerprint = canonical_history_fingerprint
history_fingerprint = canonical_history_fingerprint



__all__ = [
    "AtomicHistoryParser",
    "AtomicInteractionUnit",
    "AtomicHistoryUnit",
    "canonical_history_fingerprint",
    "canonical_prefix_fingerprint",
    "estimate_history_tokens",
    "history_fingerprint",
    "parse_atomic_history",
]
