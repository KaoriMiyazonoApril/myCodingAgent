"""Deterministic pressure pruning of detached tool results."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy

from agent.core.messages import Message, ToolResultBlock
from agent.tools.result_bounds import reduce_tool_result_block

from .units import (
    DEFAULT_TOOL_RESULT_PRUNE_THRESHOLD,
    _MessageEstimator,
    _validate_history,
    estimate_history_tokens,
    parse_atomic_history,
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



__all__ = [
    "HistoryPruner",
    "OldToolResultPruner",
    "PressurePruner",
    "PressureToolResultPruner",
    "PrunedHistory",
    "PruningResult",
    "ToolResultPruner",
    "ToolResultPressurePruner",
    "prune_old_tool_results",
]

PrunedHistory = PruningResult
