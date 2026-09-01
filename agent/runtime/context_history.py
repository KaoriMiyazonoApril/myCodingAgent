"""Detached history reduction primitives used by the Context policy.

The :class:`~agent.runtime.conversation.Conversation` remains the owner of the
canonical transcript.  The objects in this module deliberately operate on
copies and return copies, which makes pressure reduction safe to use while a
Thread is still retaining its complete audit history.

There are three small seams here:

* ``parse_atomic_history`` groups an assistant tool-call message with every
  matching result so a selector can never manufacture an orphaned protocol
  message;
* ``ToolResultPruner`` performs the cheap, deterministic pressure reduction;
* ``RecentRawTailSelector`` keeps a recent raw tail and exposes the older
  compact region to an asynchronous semantic compactor.

``LLMHistoryCompactor`` is only a low-level Provider adapter.  It does not own
an Agent loop, execute tools, or mutate a Conversation.
"""

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


def _flatten(units: Iterable[AtomicHistoryUnit]) -> list[Message]:
    return [deepcopy(message) for unit in units for message in unit.messages]


class HistorySelection(list[Message]):
    """A list-compatible raw-tail result with compact-region diagnostics."""

    def __init__(
        self,
        messages: Sequence[Message],
        *,
        compact_candidates: Sequence[Message],
        retained_units: Sequence[AtomicHistoryUnit],
        compact_units: Sequence[AtomicHistoryUnit],
        target_tokens: int,
        retained_tokens: int,
        boundary: int | None,
        canonical_end: int | None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        super().__init__(deepcopy(list(messages)))
        self.compact_candidates = deepcopy(list(compact_candidates))
        self.compact_region = self.compact_candidates
        self.candidates = self.compact_candidates
        self.retained_units = deepcopy(list(retained_units))
        self.compact_units = deepcopy(list(compact_units))
        self.target_tokens = target_tokens
        self.retained_tokens = retained_tokens
        self.boundary = boundary
        self.selector_boundary = boundary
        self.canonical_end = canonical_end
        self.metadata = deepcopy(metadata or {})

    @property
    def selected_history(self) -> list[Message]:
        return list(self)

    @property
    def raw_tail(self) -> list[Message]:
        return list(self)

    @property
    def retained_history(self) -> list[Message]:
        return list(self)

    @property
    def compact_candidate_history(self) -> list[Message]:
        return list(self.compact_candidates)

    @property
    def compact_candidate_units(self) -> list[AtomicHistoryUnit]:
        return deepcopy(self.compact_units)

    @property
    def retained(self) -> list[Message]:
        return list(self)

    @property
    def retained_message_count(self) -> int:
        return len(self)

    def __deepcopy__(self, memo: dict[int, object]) -> HistorySelection:
        del memo
        return HistorySelection(
            self,
            compact_candidates=self.compact_candidates,
            retained_units=self.retained_units,
            compact_units=self.compact_units,
            target_tokens=self.target_tokens,
            retained_tokens=self.retained_tokens,
            boundary=self.boundary,
            canonical_end=self.canonical_end,
            metadata=self.metadata,
        )


class RecentRawTailSelector:
    """Select a recent, protocol-safe raw tail from newest to oldest."""

    def __init__(
        self,
        usable_input_tokens: int | object | None = None,
        *,
        target_ratio: float = DEFAULT_RECENT_TAIL_RATIO,
        estimator: _MessageEstimator | Callable[..., int] | None = None,
        budget_tokens: int | None = None,
        usable_tokens: int | None = None,
        target_budget_tokens: int | None = None,
        budget: object | None = None,
    ) -> None:
        if budget is not None:
            if usable_input_tokens is not None:
                raise ValueError("provide only one budget source")
            usable_input_tokens = budget
        if usable_tokens is not None:
            if usable_input_tokens is not None:
                raise ValueError("provide only one usable input budget")
            usable_input_tokens = usable_tokens
        if target_budget_tokens is not None:
            if budget_tokens is not None:
                raise ValueError("provide only one target budget")
            budget_tokens = target_budget_tokens
        if usable_input_tokens is not None and not isinstance(usable_input_tokens, int):
            candidate = getattr(usable_input_tokens, "usable_input_tokens", None)
            if candidate is None:
                candidate = getattr(usable_input_tokens, "input_budget_tokens", None)
            usable_input_tokens = candidate
        if budget_tokens is not None:
            usable_input_tokens = budget_tokens
        if usable_input_tokens is not None and (
            isinstance(usable_input_tokens, bool)
            or not isinstance(usable_input_tokens, int)
            or usable_input_tokens < 0
        ):
            raise ValueError("usable_input_tokens must be a non-negative integer")
        if (
            isinstance(target_ratio, bool)
            or not isinstance(target_ratio, (int, float))
            or target_ratio <= 0
            or target_ratio > 1
        ):
            raise ValueError("target_ratio must be greater than zero and at most one")
        self.usable_input_tokens = usable_input_tokens
        self.target_ratio = float(target_ratio)
        self.estimator = estimator

    def select(
        self,
        history: Sequence[Message],
        budget_tokens: int | None = None,
        *,
        usable_input_tokens: int | None = None,
        usable_tokens: int | None = None,
        checkpoint: CompactionCheckpoint | None = None,
        covered_through: int | None = None,
    ) -> HistorySelection:
        if usable_tokens is not None:
            if usable_input_tokens is not None:
                raise ValueError("provide only one usable input budget")
            usable_input_tokens = usable_tokens
        if checkpoint is not None:
            if covered_through is not None:
                raise ValueError("provide only one checkpoint coverage")
            covered_through = checkpoint.covered_through
        if covered_through is not None and (
            isinstance(covered_through, bool)
            or not isinstance(covered_through, int)
            or covered_through < -1
        ):
            raise ValueError("covered_through must be an integer at least -1")
        snapshot = _validate_history(history)
        units = parse_atomic_history(snapshot)
        available = (
            usable_input_tokens
            if usable_input_tokens is not None
            else budget_tokens
            if budget_tokens is not None
            else self.usable_input_tokens
        )
        if available is None:
            # A standalone selector remains useful outside ContextManager.  A
            # conservative estimate of the complete input gives it a stable
            # target without requiring a hidden global budget.
            available = estimate_history_tokens(snapshot, self.estimator)
        if isinstance(available, bool) or not isinstance(available, int) or available < 0:
            raise ValueError("usable input budget must be a non-negative integer")
        target = int(available * self.target_ratio)

        def is_system_unit(unit: AtomicHistoryUnit) -> bool:
            return any(message.role == "system" for message in unit.messages)

        def is_covered_unit(unit: AtomicHistoryUnit) -> bool:
            return covered_through is not None and unit.end_index <= covered_through

        # A canonical system message is retained as a renderer source, but it
        # is not a raw-tail unit and therefore does not consume the 20% raw
        # history target.  Covered canonical units are never retained beside
        # their synthetic checkpoint summary.
        retained_indexes: set[int] = {
            index
            for index, unit in enumerate(units)
            if is_system_unit(unit)
            or (unit.open and not is_covered_unit(unit))
        }
        tool_indexes = [
            index for index, unit in enumerate(units) if unit.is_tool_interaction
        ]
        if tool_indexes:
            latest_tool_index = tool_indexes[-1]
            if not is_covered_unit(units[latest_tool_index]):
                retained_indexes.add(latest_tool_index)

        def retained_token_count(indexes: set[int]) -> int:
            raw_units = [
                units[index]
                for index in sorted(indexes)
                if not is_system_unit(units[index])
            ]
            # Token estimators generally include a request-envelope floor.
            # That floor must not make an empty raw tail appear to have
            # reached its target before the first uncovered unit is added.
            return (
                estimate_history_tokens(_flatten(raw_units), self.estimator)
                if raw_units
                else 0
            )

        # Walk newest to oldest.  The first unit that crosses the soft target
        # is retained in full; no unit is split to hit a character/token edge.
        retained_tokens = retained_token_count(retained_indexes)
        for index in range(len(units) - 1, -1, -1):
            if index in retained_indexes or is_covered_unit(units[index]):
                continue
            if retained_tokens < target or not retained_indexes:
                retained_indexes.add(index)
                retained_tokens = retained_token_count(retained_indexes)
                continue
            break

        retained_units = [unit for idx, unit in enumerate(units) if idx in retained_indexes]
        compact_units = [unit for idx, unit in enumerate(units) if idx not in retained_indexes]
        selected = _flatten(retained_units)
        candidates = _flatten(
            unit
            for unit in compact_units
            if (covered_through is None or unit.end_index > covered_through)
            and not any(message.role == "system" for message in unit.messages)
        )
        non_system_retained = [
            unit for unit in retained_units if not any(message.role == "system" for message in unit.messages)
        ]
        boundary = (
            min(unit.start_index for unit in non_system_retained)
            if non_system_retained
            else None
        )
        candidate_units = [
            unit
            for unit in compact_units
            if covered_through is None or unit.end_index > covered_through
        ]
        canonical_end = max((unit.end_index for unit in candidate_units), default=None)
        metadata = {
            "target_ratio": self.target_ratio,
            "usable_input_tokens": available,
            "target_tokens": target,
            "retained_tokens": retained_tokens,
            "retained_message_count": len(selected),
            "compact_candidate_messages": len(candidates),
            "compact_candidate_units": len(compact_units),
            "boundary": boundary,
            "canonical_compact_end": canonical_end,
            "covered_through": covered_through,
            "open_units_retained": sum(unit.open for unit in retained_units),
        }
        return HistorySelection(
            selected,
            compact_candidates=candidates,
            retained_units=retained_units,
            compact_units=compact_units,
            target_tokens=target,
            retained_tokens=retained_tokens,
            boundary=boundary,
            canonical_end=canonical_end,
            metadata=metadata,
        )


class PruningResult(list[Message]):
    """List-compatible detached result of one pressure prune."""

    def __init__(
        self,
        messages: Sequence[Message],
        *,
        pruned_count: int,
        before_tokens: int,
        after_tokens: int,
        protected_units: Sequence[int],
        metadata: dict[str, object] | None = None,
    ) -> None:
        super().__init__(deepcopy(list(messages)))
        self.history = self
        self.messages = self
        self.pruned_count = pruned_count
        self.before_tokens = before_tokens
        self.after_tokens = after_tokens
        self.protected_units = tuple(protected_units)
        self.metadata = deepcopy(metadata or {})

    @property
    def pruned(self) -> int:
        return self.pruned_count

    @property
    def count(self) -> int:
        return self.pruned_count

    @property
    def pruned_history(self) -> list[Message]:
        return list(self)


class ToolResultPruner:
    """Prune old large closed ToolResult blocks on a detached transcript."""

    def __init__(
        self,
        threshold_chars: int = DEFAULT_TOOL_RESULT_PRUNE_THRESHOLD,
        *,
        estimator: _MessageEstimator | Callable[..., int] | None = None,
        protect_latest: bool = True,
        large_result_threshold: int | None = None,
        max_result_chars: int | None = None,
        threshold: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        if threshold is not None:
            threshold_chars = threshold
        if max_bytes is not None:
            threshold_chars = max_bytes
        if large_result_threshold is not None:
            threshold_chars = large_result_threshold
        if max_result_chars is not None:
            threshold_chars = max_result_chars
        if isinstance(threshold_chars, bool) or not isinstance(threshold_chars, int) or threshold_chars < 0:
            raise ValueError("threshold_chars must be a non-negative integer")
        self.threshold_chars = threshold_chars
        self.estimator = estimator
        self.protect_latest = protect_latest

    def prune(self, history: Sequence[Message]) -> PruningResult:
        snapshot = _validate_history(history)
        units = parse_atomic_history(snapshot)
        protected: set[int] = {
            index for index, unit in enumerate(units) if unit.open
        }
        if self.protect_latest and units and units[-1].is_tool_interaction:
            # Only the trailing closed interaction is awaiting a subsequent
            # model response.  An older tool round followed by assistant/user
            # messages has already been consumed and is safe to prune.
            protected.add(len(units) - 1)
        before = estimate_history_tokens(snapshot, self.estimator)
        result = deepcopy(snapshot)
        pruned_count = 0
        for index, unit in enumerate(units):
            if index in protected or not unit.closed:
                continue
            for offset, message in enumerate(unit.messages):
                if message.role != "tool":
                    continue
                # Unit messages are slices into the snapshot, so map to the
                # corresponding detached output by canonical index.
                output_message = result[unit.start_index + offset]
                for block_index, block in enumerate(output_message.content):
                    if not isinstance(block, ToolResultBlock):
                        continue
                    if block.metadata.get("pruned") is True:
                        continue
                    original = block.content
                    # Layer-1 normally bounds this at the canonical write
                    # boundary, but detached histories can come from older
                    # persisted transcripts.  Always run the unified reducer
                    # so an oversized stdout/stderr (or nested metadata) can
                    # never be copied back into a model-visible Layer-2
                    # result, even when content itself is short.
                    bounded_block = reduce_tool_result_block(block)
                    metadata_was_bounded = bounded_block.metadata != block.metadata
                    if len(original) <= self.threshold_chars and not metadata_was_bounded:
                        continue
                    if len(original) <= self.threshold_chars:
                        output_message.content[block_index] = bounded_block
                        pruned_count += 1
                        continue
                    marker = (
                        "[tool result pruned under context pressure; "
                        f"original_size={len(original)}; "
                        f"omitted_size={{omitted}}]"
                    )
                    marker = marker.format(omitted=max(0, len(original)))
                    metadata = deepcopy(block.metadata)
                    metadata.update(
                        {
                            "pruned": True,
                            "prune_strategy": "pressure_old_tool_result",
                            "pruning_strategy": "pressure_old_tool_result",
                            "original_size": len(original),
                            "original_bytes": len(original.encode("utf-8")),
                            "retained_size": len(marker),
                            "omitted_size": len(original),
                            "retained_size_bytes": len(marker.encode("utf-8")),
                            "omitted_size_bytes": len(original.encode("utf-8")),
                            "truncated": True,
                        }
                    )
                    output_message.content[block_index] = reduce_tool_result_block(ToolResultBlock(
                        tool_call_id=block.tool_call_id,
                        content=marker,
                        metadata=metadata,
                        error_code=block.error_code,
                    ))
                    pruned_count += 1
        after = estimate_history_tokens(result, self.estimator)
        return PruningResult(
            result,
            pruned_count=pruned_count,
            before_tokens=before,
            after_tokens=after,
            protected_units=sorted(protected),
            metadata={
                "threshold_chars": self.threshold_chars,
                "pruned_count": pruned_count,
                "before_tokens": before,
                "after_tokens": after,
                "protected_units": sorted(protected),
            },
        )

    def prune_old_tool_results(self, history: Sequence[Message]) -> PruningResult:
        return self.prune(history)


PressureToolResultPruner = ToolResultPruner
OldToolResultPruner = ToolResultPruner
PressurePruner = ToolResultPruner
HistoryPruner = ToolResultPruner
ToolResultPressurePruner = ToolResultPruner


def prune_old_tool_results(
    history: Sequence[Message],
    *,
    threshold_chars: int = DEFAULT_TOOL_RESULT_PRUNE_THRESHOLD,
    estimator: _MessageEstimator | Callable[..., int] | None = None,
) -> PruningResult:
    """Convenience function for one bounded pressure prune."""

    return ToolResultPruner(threshold_chars, estimator=estimator).prune(history)


@dataclass(frozen=True, slots=True, init=False)
class CompactionSummary:
    """Synthetic semantic handoff; never a historical assistant message."""

    text: str
    covered_start: int | None
    covered_end: int | None
    source_estimate: int
    metadata: dict[str, object]
    synthetic: bool
    type: str

    def __init__(
        self,
        text: str = "",
        *,
        content: str | None = None,
        summary: str | None = None,
        covered_start: int | None = None,
        covered_end: int | None = None,
        source_range: tuple[int, int] | None = None,
        source_estimate: int = 0,
        metadata: dict[str, object] | None = None,
        synthetic: bool = True,
        type: str = "compaction_summary",
    ) -> None:
        if source_range is not None:
            if (
                not isinstance(source_range, tuple)
                or len(source_range) != 2
                or any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in source_range
                )
            ):
                raise ValueError("summary source_range must contain two indexes")
            covered_start, covered_end = source_range
        selected_text = text
        if content is not None:
            selected_text = content
        if summary is not None:
            selected_text = summary
        if not isinstance(selected_text, str) or not selected_text.strip():
            raise ValueError("compaction summary text must be non-empty")
        for name, value in (("covered_start", covered_start), ("covered_end", covered_end)):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"summary {name} must be a non-negative integer or None")
        if isinstance(source_estimate, bool) or not isinstance(source_estimate, int) or source_estimate < 0:
            raise ValueError("summary source estimate must be non-negative")
        if not isinstance(synthetic, bool) or not synthetic:
            raise ValueError("compaction summaries must be synthetic")
        if not isinstance(type, str) or not type:
            raise ValueError("summary type must be non-empty")
        object.__setattr__(self, "text", selected_text)
        object.__setattr__(self, "covered_start", covered_start)
        object.__setattr__(self, "covered_end", covered_end)
        object.__setattr__(self, "source_estimate", source_estimate)
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("summary metadata must be an object")
        summary_metadata = {"synthetic": True, "type": type}
        summary_metadata.update(deepcopy(metadata or {}))
        summary_metadata["synthetic"] = True
        summary_metadata.setdefault("type", type)
        object.__setattr__(self, "metadata", summary_metadata)
        object.__setattr__(self, "synthetic", True)
        object.__setattr__(self, "type", type)

    @property
    def content(self) -> str:
        return self.text

    @property
    def summary(self) -> str:
        return self.text

    @property
    def source_coverage(self) -> tuple[int | None, int | None]:
        return self.covered_start, self.covered_end

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": COMPACTION_SUMMARY_SCHEMA_VERSION,
            "type": self.type,
            "synthetic": True,
            "text": self.text,
            "covered_start": self.covered_start,
            "covered_end": self.covered_end,
            "source_estimate": self.source_estimate,
            "metadata": deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: object) -> CompactionSummary:
        if not isinstance(raw, dict):
            raise ValueError("compaction summary must be an object")
        if raw.get("synthetic", True) is not True:
            raise ValueError("compaction summary must be synthetic")
        text = raw.get("text", raw.get("content"))
        if not isinstance(text, str):
            raise ValueError("compaction summary text must be text")
        return cls(
            text,
            covered_start=raw.get("covered_start"),
            covered_end=raw.get("covered_end"),
            source_estimate=raw.get("source_estimate", 0),
            metadata=raw.get("metadata", {}),
            type=raw.get("type", "compaction_summary"),
        )


@dataclass(frozen=True, slots=True, init=False)
class CompactionCheckpoint:
    """Durable rolling summary and its inclusive canonical coverage position."""

    version: int
    summary: CompactionSummary
    covered_through: int
    created_at: str
    updated_at: str
    source_estimate: int
    metadata: dict[str, object]
    canonical_fingerprint: str | None

    def __init__(
        self,
        summary: CompactionSummary | str,
        covered_through: int = -1,
        *,
        version: int = COMPACTION_CHECKPOINT_SCHEMA_VERSION,
        coverage_position: int | None = None,
        coverage_end: int | None = None,
        canonical_position: int | None = None,
        canonical_coverage: int | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
        source_estimate: int = 0,
        metadata: dict[str, object] | None = None,
        canonical_fingerprint: str | None = None,
        coverage_fingerprint: str | None = None,
        anchor_fingerprint: str | None = None,
        history_fingerprint: str | None = None,
    ) -> None:
        if coverage_position is not None:
            covered_through = coverage_position
        if coverage_end is not None:
            covered_through = coverage_end
        if canonical_position is not None:
            covered_through = canonical_position
        if canonical_coverage is not None:
            covered_through = canonical_coverage
        fingerprints = [
            value
            for value in (
                canonical_fingerprint,
                coverage_fingerprint,
                anchor_fingerprint,
                history_fingerprint,
            )
            if value is not None
        ]
        if len(set(fingerprints)) > 1:
            raise ValueError("checkpoint fingerprint aliases must agree")
        selected_fingerprint = fingerprints[0] if fingerprints else None
        if selected_fingerprint is not None and (
            not isinstance(selected_fingerprint, str) or not selected_fingerprint
        ):
            raise ValueError("checkpoint fingerprint must be non-empty text")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise ValueError("checkpoint version must be a positive integer")
        if isinstance(covered_through, bool) or not isinstance(covered_through, int) or covered_through < -1:
            raise ValueError("checkpoint coverage must be an integer at least -1")
        if isinstance(source_estimate, bool) or not isinstance(source_estimate, int) or source_estimate < 0:
            raise ValueError("checkpoint source estimate must be non-negative")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("checkpoint metadata must be an object")
        normalized = (
            summary
            if isinstance(summary, CompactionSummary)
            else CompactionSummary(summary)
        )
        now = datetime.now(UTC).isoformat()
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "summary", normalized)
        object.__setattr__(self, "covered_through", covered_through)
        object.__setattr__(self, "created_at", created_at or now)
        object.__setattr__(self, "updated_at", updated_at or created_at or now)
        object.__setattr__(self, "source_estimate", source_estimate)
        object.__setattr__(self, "metadata", deepcopy(metadata or {}))
        object.__setattr__(self, "canonical_fingerprint", selected_fingerprint)

    @property
    def schema_version(self) -> int:
        return self.version

    @property
    def coverage_position(self) -> int:
        return self.covered_through

    @property
    def coverage_end(self) -> int:
        return self.covered_through

    @property
    def canonical_position(self) -> int:
        return self.covered_through

    @property
    def canonical_coverage(self) -> int:
        return self.covered_through

    @property
    def coverage_fingerprint(self) -> str | None:
        return self.canonical_fingerprint

    @property
    def anchor_fingerprint(self) -> str | None:
        return self.canonical_fingerprint

    @property
    def history_fingerprint(self) -> str | None:
        return self.canonical_fingerprint

    @property
    def covered_position(self) -> int:
        return self.covered_through

    def valid_for_history(
        self,
        history: Sequence[Message],
        *,
        require_fingerprint: bool = False,
    ) -> bool:
        if self.version != COMPACTION_CHECKPOINT_SCHEMA_VERSION or not self.summary.synthetic:
            return False
        try:
            messages = _validate_history(history)
            if not -1 <= self.covered_through < len(messages):
                return False
            if self.covered_through >= 0:
                units = parse_atomic_history(messages)
                # Coverage may only advance after a complete atomic unit.  A
                # checkpoint in the middle of a tool-call/result interaction
                # would make the next request expose an orphan protocol item.
                if not any(unit.end_index == self.covered_through for unit in units):
                    return False
            if self.canonical_fingerprint is None:
                return not require_fingerprint
            return self.canonical_fingerprint == canonical_history_fingerprint(
                messages, self.covered_through
            )
        except (TypeError, ValueError):
            return False

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.version,
            "version": self.version,
            "summary": self.summary.to_dict(),
            "covered_through": self.covered_through,
            "coverage_position": self.covered_through,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_estimate": self.source_estimate,
            "metadata": deepcopy(self.metadata),
            "canonical_fingerprint": self.canonical_fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: object) -> CompactionCheckpoint:
        if not isinstance(raw, dict):
            raise ValueError("compaction checkpoint must be an object")
        summary = CompactionSummary.from_dict(raw["summary"])
        return cls(
            summary,
            covered_through=raw.get(
                "covered_through",
                raw.get("coverage_position", raw.get("coverage_end", -1)),
            ),
            version=raw.get("version", raw.get("schema_version", 1)),
            created_at=raw.get("created_at"),
            updated_at=raw.get("updated_at"),
            source_estimate=raw.get("source_estimate", 0),
            metadata=raw.get("metadata", {}),
            canonical_fingerprint=raw.get(
                "canonical_fingerprint",
                raw.get(
                    "coverage_fingerprint",
                    raw.get("anchor_fingerprint", raw.get("history_fingerprint")),
                ),
            ),
        )


class CompactionError(RuntimeError):
    """A semantic compaction attempt could not produce a safe summary."""

    def __init__(self, message: str, *, previous_checkpoint: CompactionCheckpoint | None = None) -> None:
        super().__init__(message)
        self.previous_checkpoint = previous_checkpoint


class AsyncHistoryCompactor(Protocol):
    """Narrow async seam implemented by a low-level model adapter."""

    async def compact(
        self,
        previous_summary: CompactionSummary | None,
        compact_region: Sequence[Message],
    ) -> CompactionSummary | str:
        """Return one synthetic handoff summary."""


HistoryCompactor = AsyncHistoryCompactor


COMPACTION_SYSTEM_PROMPT = """You are the semantic history compactor for a coding agent.
Produce one concise synthetic handoff for the next model step. Retain the user's
goal and constraints; completed work; important decisions and reasoning; files
read, created, or modified; findings and command/tool evidence; validation
performed and its outcomes; and open work or risks. Delete redundant chat,
obsolete reasoning, repeated tool output, and bulk output that has no durable
meaning. Preserve uncertainty and failures instead of claiming success. Return
only the handoff text, with clear labels for goal, constraints, completed work,
decisions, files, findings, validation, and open work.
"""


def _format_message(message: Message) -> str:
    parts: list[str] = [f"[{message.role}]"]
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ReasoningBlock):
            parts.append(f"reasoning: {block.text}")
        elif isinstance(block, ToolCallBlock):
            parts.append(
                "tool_call "
                + json.dumps(
                    {
                        "id": block.id,
                        "name": block.name,
                        "arguments": block.arguments,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        elif isinstance(block, ToolResultBlock):
            parts.append(
                "tool_result "
                + json.dumps(
                    {
                        "tool_call_id": block.tool_call_id,
                        "content": block.content,
                        "metadata": block.metadata,
                        "error_code": block.error_code,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
    return "\n".join(parts)


def _summary_text(summary: CompactionSummary | str | None) -> str:
    if summary is None:
        return "(none)"
    if isinstance(summary, CompactionSummary):
        return summary.text
    return str(summary)


class LLMHistoryCompactor:
    """Use one low-level ``provider.chat`` call to create a handoff summary."""

    def __init__(
        self,
        provider: Any,
        *,
        estimator: _MessageEstimator | Callable[..., int] | None = None,
        request_budget_tokens: int | None = None,
        max_request_tokens: int | None = None,
        system_prompt: str = COMPACTION_SYSTEM_PROMPT,
    ) -> None:
        if not callable(getattr(provider, "chat", None)):
            raise ValueError("compaction provider must expose async chat(request)")
        if max_request_tokens is not None:
            request_budget_tokens = max_request_tokens
        if request_budget_tokens is not None and (
            isinstance(request_budget_tokens, bool)
            or not isinstance(request_budget_tokens, int)
            or request_budget_tokens <= 0
        ):
            raise ValueError("request_budget_tokens must be a positive integer")
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("system_prompt must be non-empty text")
        self.provider = provider
        self.estimator = estimator
        self.request_budget_tokens = request_budget_tokens
        self.system_prompt = system_prompt
        self.last_request: LLMRequest | None = None
        # Detached source actually represented in the latest provider
        # request.  Rolling checkpoint coverage must use this value when a
        # budgeted request keeps only a prefix of candidate units.
        self.last_compaction_source: list[Message] | None = None
        self.last_request_source: list[Message] | None = None

    def _build_messages(
        self,
        previous_summary: CompactionSummary | None,
        compact_region: Sequence[Message],
    ) -> list[Message]:
        source = _validate_history(compact_region)
        source_text = "\n\n".join(_format_message(message) for message in source)
        user_text = (
            "PREVIOUS SYNTHETIC HANDOFF:\n"
            f"{_summary_text(previous_summary)}\n\n"
            "NEWLY OLD RAW HISTORY (do not assume omitted history is absent):\n"
            f"{source_text or '(none)'}"
        )
        return [
            Message(role="system", content=[TextBlock(text=self.system_prompt)]),
            Message(role="user", content=[TextBlock(text=user_text)]),
        ]

    def _bounded_messages(
        self,
        previous_summary: CompactionSummary | None,
        compact_region: Sequence[Message],
    ) -> list[Message]:
        source = _validate_history(compact_region)
        messages = self._build_messages(previous_summary, source)
        budget = self.request_budget_tokens
        if budget is None or estimate_history_tokens(messages, self.estimator) <= budget:
            self.last_compaction_source = deepcopy(source)
            return messages

        units = parse_atomic_history(source)
        # Keep a chronological prefix when the compaction request itself needs
        # bounding.  A checkpoint stores an inclusive canonical position, so
        # a prefix is the only bounded subset that cannot claim coverage over
        # an omitted unit.  No assistant/tool pair is split.
        kept: list[AtomicHistoryUnit] = []
        for unit in units:
            candidate = _flatten([*kept, unit])
            attempt = self._build_messages(previous_summary, candidate)
            if estimate_history_tokens(attempt, self.estimator) <= budget:
                kept.append(unit)
                continue
            if not kept:
                # Do not turn an unrepresentable atomic source into an empty
                # ``(none)`` request: that would produce a summary and a
                # checkpoint that falsely cover the original candidates.
                raise CompactionError(
                    "no complete atomic unit fits the compaction request budget"
                )
            # Later units would move the coverage boundary forward and cannot
            # be claimed by this request.  Leave them for a future step.
            break
        bounded_source = _flatten(kept)
        self.last_compaction_source = deepcopy(bounded_source)
        bounded = self._build_messages(previous_summary, bounded_source)
        if estimate_history_tokens(bounded, self.estimator) > budget:
            raise CompactionError("compaction request exceeds its input budget")
        return bounded

    async def compact(
        self,
        previous_summary: CompactionSummary | Sequence[Message] | None = None,
        compact_region: Sequence[Message] | None = None,
    ) -> CompactionSummary:
        # The two-argument form is the rolling seam.  Accepting a single
        # history argument keeps this adapter source-compatible with the
        # original synchronous HistoryCompactor protocol while callers move
        # to the explicit previous-summary boundary.
        if compact_region is None:
            if previous_summary is None:
                compact_region = ()
                previous_summary = None
            elif isinstance(previous_summary, CompactionSummary) or isinstance(
                previous_summary, str
            ):
                raise TypeError(
                    "compact_region is required when previous_summary is supplied"
                )
            else:
                compact_region = previous_summary
                previous_summary = None
        if previous_summary is not None and not isinstance(
            previous_summary, CompactionSummary
        ):
            raise TypeError("previous_summary must be CompactionSummary or None")
        messages = self._bounded_messages(previous_summary, compact_region)
        actual_source = deepcopy(self.last_compaction_source or [])
        request = LLMRequest(messages=messages, tools=[])
        self.last_request = request
        self.last_request_source = deepcopy(actual_source)
        try:
            response = self.provider.chat(request)
            if inspect.isawaitable(response):
                response = await response
        except CompactionError:
            raise
        except Exception as error:
            raise CompactionError(f"semantic compaction provider failed: {error}") from error
        message = getattr(response, "message", None)
        if not isinstance(message, Message):
            raise CompactionError("semantic compaction provider returned no Message")
        text = "\n".join(
            block.text for block in message.content if isinstance(block, TextBlock)
        ).strip()
        if not text:
            raise CompactionError("semantic compaction provider returned an empty summary")
        return CompactionSummary(
            text,
            source_estimate=estimate_history_tokens(actual_source, self.estimator),
            metadata={
                "request_budget_tokens": self.request_budget_tokens,
                "source_messages": len(actual_source),
            },
        )


ProviderHistoryCompactor = LLMHistoryCompactor
SemanticHistoryCompactor = LLMHistoryCompactor


@dataclass(frozen=True, slots=True)
class RollingCompactionResult:
    """Detached result of one successful rolling compaction attempt."""

    summary: CompactionSummary
    checkpoint: CompactionCheckpoint
    compacted_messages: list[Message]
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "compacted_messages", deepcopy(self.compacted_messages))
        object.__setattr__(self, "metadata", deepcopy(self.metadata))


def _units_matching_source(
    units: Sequence[AtomicHistoryUnit],
    source: Sequence[Message],
) -> list[AtomicHistoryUnit]:
    """Map a compactor's detached source back to complete canonical units.

    The source is expected to be a chronological prefix of ``units``.  The
    comparison is intentionally message-based and detached; if an adapter
    exposes a partial or foreign message, returning an empty match lets the
    rolling seam fail closed instead of advancing checkpoint coverage.
    """

    source_messages = _validate_history(source)
    if not source_messages:
        return []
    matched: list[AtomicHistoryUnit] = []
    cursor = 0
    for unit in units:
        count = len(unit.messages)
        if source_messages[cursor : cursor + count] == unit.messages:
            matched.append(unit)
            cursor += count
            if cursor == len(source_messages):
                break
    if cursor != len(source_messages):
        return []
    return matched


class RollingSemanticCompactor:
    """Combine a previous checkpoint with only newly old raw history."""

    def __init__(
        self,
        compactor: AsyncHistoryCompactor,
        *,
        estimator: _MessageEstimator | Callable[..., int] | None = None,
    ) -> None:
        if not callable(getattr(compactor, "compact", None)):
            raise ValueError("compactor must expose compact(previous_summary, region)")
        self.compactor = compactor
        self.estimator = estimator

    async def compact(
        self,
        canonical_history: Sequence[Message],
        selection: HistorySelection | Sequence[Message],
        *,
        previous_checkpoint: CompactionCheckpoint | None = None,
        coverage_end: int | None = None,
        checkpoint: CompactionCheckpoint | None = None,
    ) -> RollingCompactionResult | None:
        if checkpoint is not None:
            if previous_checkpoint is not None:
                raise ValueError("provide only one previous checkpoint")
            previous_checkpoint = checkpoint
        canonical = _validate_history(canonical_history)
        previous = previous_checkpoint
        if previous is not None and not previous.valid_for_history(canonical):
            previous = None

        if isinstance(selection, HistorySelection):
            units = selection.compact_units
            candidates = selection.compact_candidates
            if coverage_end is None:
                coverage_end = selection.canonical_end
        else:
            candidates = _validate_history(selection)
            units = parse_atomic_history(candidates)

        if previous is not None:
            # A valid checkpoint covers an inclusive canonical prefix.  The
            # selector's units retain their original indexes; filter old units
            # before asking the model to summarize anything.
            units = [unit for unit in units if unit.end_index > previous.covered_through]
            candidates = _flatten(units)
        if not candidates:
            return None

        previous_summary = None if previous is None else previous.summary
        try:
            # A compactor may bound the request while it is running.  Invoke
            # it before reading its exposed source so a reused adapter cannot
            # leak the previous call's source into this checkpoint.
            response = self.compactor.compact(previous_summary, candidates)
            if inspect.isawaitable(response):
                response = await response
            if isinstance(response, str):
                response = CompactionSummary(response)
            if not isinstance(response, CompactionSummary) or not response.text.strip():
                raise CompactionError("semantic compactor returned an invalid summary")

            # Derive coverage from the exact detached source sent to the
            # provider.  Otherwise a bounded request could silently mark an
            # omitted atomic unit as summarized.  Generic async fakes do not
            # expose a source, so they are treated as having received the
            # complete candidate region.
            exposed_source = getattr(self.compactor, "last_compaction_source", None)
            if exposed_source is None:
                actual_units = list(units)
                actual_candidates = _flatten(actual_units)
            else:
                actual_units = _units_matching_source(units, exposed_source)
                if not actual_units:
                    raise CompactionError(
                        "semantic compactor did not expose complete atomic source",
                        previous_checkpoint=previous,
                    )
                actual_candidates = _flatten(actual_units)
            if not actual_candidates:
                raise CompactionError(
                    "semantic compactor received no complete atomic unit",
                    previous_checkpoint=previous,
                )

            candidate_end = max(
                (unit.end_index for unit in actual_units),
                default=coverage_end if coverage_end is not None else -1,
            )
            # ``coverage_end`` is only a fallback for a generic sequence input;
            # when actual unit boundaries are known it must never extend beyond
            # the source sent to the compactor.
            if exposed_source is None and coverage_end is not None:
                candidate_end = max(candidate_end, coverage_end)
        except CompactionError as error:
            if error.previous_checkpoint is None:
                error.previous_checkpoint = previous  # type: ignore[misc]
            raise
        except Exception as error:
            raise CompactionError(
                f"semantic compaction failed: {error}", previous_checkpoint=previous
            ) from error

        now = datetime.now(UTC).isoformat()
        start = (
            previous.covered_through + 1
            if previous is not None
            else (min(unit.start_index for unit in actual_units) if actual_units else 0)
        )
        summary = CompactionSummary(
            response.text,
            covered_start=start,
            covered_end=candidate_end,
            source_estimate=estimate_history_tokens(actual_candidates, self.estimator),
            metadata={
                **response.metadata,
                "synthetic": True,
                "covered_start": start,
                "covered_end": candidate_end,
                "previous_checkpoint_coverage": (
                    None if previous is None else previous.covered_through
                ),
            },
        )
        checkpoint = CompactionCheckpoint(
            summary,
            covered_through=candidate_end,
            created_at=now if previous is None else previous.created_at,
            updated_at=now,
            source_estimate=summary.source_estimate,
            canonical_fingerprint=canonical_history_fingerprint(
                canonical, candidate_end
            ),
            metadata={
                "source_messages": len(actual_candidates),
                "rolled_from": None if previous is None else previous.covered_through,
            },
        )
        return RollingCompactionResult(
            summary=summary,
            checkpoint=checkpoint,
            compacted_messages=actual_candidates,
            metadata={
                "previous_checkpoint_reused": previous is not None,
                "covered_through": candidate_end,
                "source_messages": len(actual_candidates),
            },
        )

    async def compact_once(
        self,
        canonical_history: Sequence[Message],
        selection: HistorySelection | Sequence[Message],
        *,
        previous_checkpoint: CompactionCheckpoint | None = None,
        coverage_end: int | None = None,
        checkpoint: CompactionCheckpoint | None = None,
    ) -> RollingCompactionResult | None:
        return await self.compact(
            canonical_history,
            selection,
            previous_checkpoint=previous_checkpoint,
            coverage_end=coverage_end,
            checkpoint=checkpoint,
        )


RollingCompactor = RollingSemanticCompactor


__all__ = [
    "AsyncHistoryCompactor",
    "AtomicHistoryParser",
    "AtomicInteractionUnit",
    "AtomicHistoryUnit",
    "COMPACTION_CHECKPOINT_SCHEMA_VERSION",
    "COMPACTION_SUMMARY_SCHEMA_VERSION",
    "COMPACTION_SYSTEM_PROMPT",
    "CompactionCheckpoint",
    "CompactionError",
    "CompactionSummary",
    "DEFAULT_RECENT_TAIL_RATIO",
    "DEFAULT_TOOL_RESULT_PRUNE_THRESHOLD",
    "HistoryCompactor",
    "HistoryPruner",
    "HistorySelection",
    "LLMHistoryCompactor",
    "OldToolResultPruner",
    "PressurePruner",
    "PressureToolResultPruner",
    "PrunedHistory",
    "PruningResult",
    "ProviderHistoryCompactor",
    "RecentRawTailSelector",
    "RecentTailSelector",
    "RollingCompactionResult",
    "RollingCompactor",
    "RollingSemanticCompactor",
    "SemanticHistoryCompactor",
    "ToolResultPruner",
    "ToolResultPressurePruner",
    "estimate_history_tokens",
    "parse_atomic_history",
    "prune_old_tool_results",
]


# Compatibility spelling used by a few embedders.
PrunedHistory = PruningResult
RecentTailSelector = RecentRawTailSelector
