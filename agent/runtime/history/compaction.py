"""Semantic compaction summaries, checkpoints, and provider adapters."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
import inspect
import json
from typing import Any, Protocol

from agent.core.messages import (
    Message,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from agent.model.types import LLMRequest

from .pruning import (
    DEFAULT_TOOL_RESULT_PRUNE_THRESHOLD,
    PrunedHistory,
    PruningResult,
)
from .selection import (
    HistorySelection,
    RecentRawTailSelector,
    RecentTailSelector,
    _flatten,
)
from .units import (
    AtomicHistoryUnit,
    AtomicInteractionUnit,
    DEFAULT_RECENT_TAIL_RATIO,
    _MessageEstimator,
    _validate_history,
    canonical_history_fingerprint,
    estimate_history_tokens,
    parse_atomic_history,
)

COMPACTION_SUMMARY_SCHEMA_VERSION = 1
COMPACTION_CHECKPOINT_SCHEMA_VERSION = 1

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
            # A checkpoint without a canonical prefix fingerprint is legacy
            # metadata, not proof that the durable prefix is unchanged.  It
            # may be retained for diagnostics/migration, but it must never
            # hide canonical history from a model request.
            if self.canonical_fingerprint is None:
                return False
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
        # An open unit is not a complete compaction source even when the
        # detached message slice happens to match it byte-for-byte.  The
        # caller may compact a shorter prefix before this unit, but may never
        # claim coverage through the incomplete interaction.
        if not unit.complete:
            break
        # Only a complete prefix beginning at the first candidate is valid.
        # A permissive search that skipped a mismatching unit could advance a
        # checkpoint over an atomic interaction never sent to the compactor.
        if source_messages[cursor : cursor + count] != unit.messages:
            break
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
            # ``coverage_end`` is a hint only.  Never let it advance coverage
            # beyond the final complete atomic unit actually sent to the
            # compactor, including for generic sequence adapters.
            if coverage_end is not None:
                candidate_end = min(candidate_end, coverage_end)
                if candidate_end not in {unit.end_index for unit in actual_units}:
                    candidate_end = max(unit.end_index for unit in actual_units)
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
