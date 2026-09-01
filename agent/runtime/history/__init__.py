"""Context history split by responsibility.

The package keeps protocol grouping/fingerprints in ``units``, raw-tail
selection in ``selection``, cheap pressure reduction in ``pruning``, and
semantic summaries/checkpoints in ``compaction``.  The former
``agent.runtime.context_history`` path re-exports this public surface.
"""

from .units import (
    AtomicHistoryParser,
    AtomicHistoryUnit,
    AtomicInteractionUnit,
    canonical_history_fingerprint,
    canonical_prefix_fingerprint,
    estimate_history_tokens,
    history_fingerprint,
    parse_atomic_history,
)
from .selection import HistorySelection, RecentRawTailSelector, RecentTailSelector
from .pruning import (
    DEFAULT_TOOL_RESULT_PRUNE_THRESHOLD,
    HistoryPruner,
    OldToolResultPruner,
    PressurePruner,
    PressureToolResultPruner,
    PrunedHistory,
    PruningResult,
    ToolResultPruner,
    ToolResultPressurePruner,
    prune_old_tool_results,
)
from .compaction import (
    AsyncHistoryCompactor,
    COMPACTION_CHECKPOINT_SCHEMA_VERSION,
    COMPACTION_SUMMARY_SCHEMA_VERSION,
    COMPACTION_SYSTEM_PROMPT,
    CompactionCheckpoint,
    CompactionError,
    CompactionSummary,
    HistoryCompactor,
    LLMHistoryCompactor,
    ProviderHistoryCompactor,
    RollingCompactionResult,
    RollingCompactor,
    RollingSemanticCompactor,
    SemanticHistoryCompactor,
)
from .units import DEFAULT_RECENT_TAIL_RATIO

__all__ = [
    "AsyncHistoryCompactor",
    "AtomicHistoryParser",
    "AtomicHistoryUnit",
    "AtomicInteractionUnit",
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
    "canonical_history_fingerprint",
    "canonical_prefix_fingerprint",
    "estimate_history_tokens",
    "history_fingerprint",
    "parse_atomic_history",
    "prune_old_tool_results",
]
