"""Protocol-safe raw-tail selection for Context reduction."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from copy import deepcopy
from typing import Any

from agent.core.messages import Message

from .units import (
    AtomicHistoryUnit,
    DEFAULT_RECENT_TAIL_RATIO,
    _MessageEstimator,
    _validate_history,
    estimate_history_tokens,
    parse_atomic_history,
)

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



RecentTailSelector = RecentRawTailSelector

__all__ = ["HistorySelection", "RecentRawTailSelector", "RecentTailSelector"]
