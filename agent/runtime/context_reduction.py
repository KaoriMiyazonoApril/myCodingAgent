"""Small deterministic reductions used by the context orchestration layer."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy

from agent.core.messages import Message

from .history import AtomicHistoryUnit, HistorySelection, estimate_history_tokens


def promote_retained_prefix_for_compaction(
    selection: HistorySelection,
    *,
    estimator: object | None = None,
) -> HistorySelection:
    """Expose a closed retained prefix to the one compaction pass.

    The selector keeps an atomic boundary unit whole. Under hard pressure a
    large closed interaction can otherwise consume the entire raw tail while
    leaving no candidate for semantic compaction. This helper moves only a
    chronological, complete prefix; the newest tool interaction and open
    units remain raw.
    """

    retained_units = sorted(selection.retained_units, key=lambda unit: unit.start_index)
    if len(retained_units) < 2:
        return selection
    latest_tool_start = max(
        (unit.start_index for unit in retained_units if unit.is_tool_interaction),
        default=None,
    )
    eligible = [
        unit
        for unit in retained_units
        if unit.closed
        and not any(message.role == "system" for message in unit.messages)
        and unit.start_index != latest_tool_start
    ]
    if not eligible:
        return selection

    def unit_tokens(unit: AtomicHistoryUnit) -> int:
        return estimate_history_tokens(unit.messages, estimator)

    chosen = max(eligible, key=lambda unit: (unit_tokens(unit), unit.start_index))
    moved = [
        unit
        for unit in retained_units
        if unit.end_index <= chosen.end_index
        and unit.closed
        and not any(message.role == "system" for message in unit.messages)
    ]
    if not moved:
        return selection
    moved_indexes = {unit.start_index for unit in moved}
    remaining = [unit for unit in retained_units if unit.start_index not in moved_indexes]
    compact_units = sorted([*selection.compact_units, *moved], key=lambda unit: unit.start_index)

    def flatten(units: Sequence[AtomicHistoryUnit]) -> list[Message]:
        return [deepcopy(message) for unit in units for message in unit.messages]

    selected = flatten(remaining)
    compact_candidates = flatten(
        [
            unit
            for unit in compact_units
            if not any(message.role == "system" for message in unit.messages)
        ]
    )
    non_system_retained = [
        unit for unit in remaining if not any(message.role == "system" for message in unit.messages)
    ]
    boundary = min((unit.start_index for unit in non_system_retained), default=None)
    canonical_end = max((unit.end_index for unit in compact_units), default=None)
    retained_tokens = estimate_history_tokens(selected, estimator) if selected else 0
    metadata = deepcopy(selection.metadata)
    metadata.update(
        {
            "retained_tokens": retained_tokens,
            "retained_message_count": len(selected),
            "compact_candidate_messages": len(compact_candidates),
            "compact_candidate_units": len(compact_units),
            "boundary": boundary,
            "canonical_compact_end": canonical_end,
            "promoted_for_hard_pressure": True,
        }
    )
    return HistorySelection(
        selected,
        compact_candidates=compact_candidates,
        retained_units=remaining,
        compact_units=compact_units,
        target_tokens=selection.target_tokens,
        retained_tokens=retained_tokens,
        boundary=boundary,
        canonical_end=canonical_end,
        metadata=metadata,
    )


__all__ = ["promote_retained_prefix_for_compaction"]
