"""Provider-independent model context assembly and orchestration.

``Conversation`` owns the durable, complete Thread transcript.  This module
turns a detached transcript plus request-scoped sources into the messages a
model sees.  The default history policies deliberately do nothing so that
future selection and compaction cannot silently change durable history.
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from agent.core.messages import Message, TextBlock
from agent.tools.types import ToolDefinition

from .context_budget import (
    ContextBudget,
    ContextBudgetPolicy,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
)
from .context_types import (
    BaseSystemInstructions,
    ContextSection,
    ContextSource,
)
from .instructions import (
    FileProjectInstructionsProvider,
    MAX_PROJECT_INSTRUCTIONS_BYTES,
    PROJECT_INSTRUCTIONS_MAX_BYTES,
    ProjectInstructions,
    ProjectInstructionsProvider,
    RootProjectInstructionsProvider,
    StaticProjectInstructionsProvider,
    WorkspaceProjectInstructionsProvider,
)
from .runtime_environment import RuntimeContext
from .task_state import (
    CommandEvidence,
    Evidence,
    EvidenceKind,
    EvidenceStatus,
    HarnessEvidence,
    MAX_COMMAND_EVIDENCE_STRING_CHARS,
    MAX_EVIDENCE_COMMAND_CHARS,
    MAX_EVIDENCE_RESULT_ID_CHARS,
    MAX_EVIDENCE_STATUS_CHARS,
    MAX_EVIDENCE_TIMESTAMP_CHARS,
    MAX_EVIDENCE_TOOL_CHARS,
    MAX_PLAN_STEP_TEXT_CHARS,
    MAX_RECENT_COMPLETED_MILESTONES,
    MAX_TASK_EVIDENCE,
    MAX_TASK_PLAN_STEPS,
    MAX_TASK_PLAN_STEP_TEXT_CHARS,
    PlanStep,
    PlanStepStatus,
    TaskEvidence,
    TaskPlan,
    TaskState,
    TaskStateView,
)
from .history import (
    AsyncHistoryCompactor,
    AtomicHistoryParser,
    AtomicInteractionUnit,
    AtomicHistoryUnit,
    CompactionCheckpoint,
    CompactionError,
    CompactionSummary,
    HistorySelection,
    HistoryPruner,
    LLMHistoryCompactor,
    OldToolResultPruner,
    PressurePruner,
    PressureToolResultPruner,
    PruningResult,
    RecentRawTailSelector,
    RecentTailSelector,
    RollingCompactionResult,
    RollingCompactor,
    RollingSemanticCompactor,
    ToolResultPruner,
    ToolResultPressurePruner,
    estimate_history_tokens,
    parse_atomic_history,
    prune_old_tool_results,
)


@dataclass(frozen=True, slots=True)
class ContextSources:
    """The stable and request-scoped sources for one model request."""

    base_system_instructions: BaseSystemInstructions
    project_instructions: ProjectInstructions
    runtime_context: RuntimeContext | None = None
    compaction_summary: CompactionSummary | None = None
    task_state: TaskState | None = None
    available_skills: object | None = None
    available_skills_max_chars: int = 8_000

    def sections(self) -> list[ContextSection]:
        sections = self.base_system_instructions.sections()
        sections.extend(self.project_instructions.sections())
        if self.available_skills is not None:
            projection = getattr(self.available_skills, "model_projection", None)
            if callable(projection):
                projected = projection(max_chars=self.available_skills_max_chars)
                content = getattr(projected, "text", projected)
            else:
                content = str(getattr(self.available_skills, "model_catalog", ""))
            if content:
                # Keep the catalog in its own deterministic baseline block so
                # the short base instructions remain an exact prefix for
                # embedders that cache the first system block.
                sections.append(ContextSection("available_skills", content, stable=True))
        if self.runtime_context is not None:
            sections.extend(self.runtime_context.sections())
        if self.compaction_summary is not None:
            sections.append(
                ContextSection(
                    "compaction_summary",
                    self.compaction_summary.text,
                    stable=False,
                )
            )
        if self.task_state is not None:
            sections.extend(self.task_state.sections())
        return sections


class HistorySelector(Protocol):
    """Choose model-visible history from a detached canonical snapshot."""

    def select(self, history: Sequence[Message]) -> Sequence[Message]:
        """Return selected history without owning persistence."""


class NoOpHistorySelector:
    """Preserve every canonical message in its original order."""

    def select(self, history: Sequence[Message]) -> list[Message]:
        return deepcopy(list(history))


class HistoryCompactor(Protocol):
    """Compact selected history behind a future policy seam."""

    def compact(self, history: Sequence[Message]) -> Sequence[Message]:
        """Return compacted history without mutating its input."""


class NoOpHistoryCompactor:
    """Keep selected history complete; no slice, ranking or summarization."""

    def compact(self, history: Sequence[Message]) -> list[Message]:
        return deepcopy(list(history))


@dataclass(frozen=True, slots=True)
class ContextPlan:
    """Detached, inspectable description of one model-visible context."""

    source_sections: list[ContextSection]
    selected_history: list[Message]
    compacted_history: list[Message]
    current_input: Message | None
    estimated_tokens: int
    input_budget_tokens: int
    budget_status: str
    decision_metadata: dict[str, object] = field(default_factory=dict)
    pressure: str = "normal"
    final_fit: str = "fits"
    late_sections: list[ContextSection] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.budget_status not in {"fits", "exceeds", "overflow"}:
            raise ValueError("budget_status must be 'fits', 'exceeds', or 'overflow'")
        if self.pressure not in {"normal", "soft", "hard"}:
            raise ValueError("pressure must be normal, soft, or hard")
        if self.final_fit not in {"fits", "overflow"}:
            raise ValueError("final_fit must be fits or overflow")
        if (
            isinstance(self.estimated_tokens, bool)
            or not isinstance(self.estimated_tokens, int)
            or self.estimated_tokens < 0
            or isinstance(self.input_budget_tokens, bool)
            or not isinstance(self.input_budget_tokens, int)
            or self.input_budget_tokens < 0
        ):
            raise ValueError("context estimates must be non-negative integers")
        object.__setattr__(self, "source_sections", deepcopy(self.source_sections))
        object.__setattr__(self, "selected_history", deepcopy(self.selected_history))
        object.__setattr__(self, "compacted_history", deepcopy(self.compacted_history))
        object.__setattr__(self, "current_input", deepcopy(self.current_input))
        object.__setattr__(self, "decision_metadata", deepcopy(self.decision_metadata))
        object.__setattr__(self, "late_sections", deepcopy(self.late_sections))

    @property
    def baseline_sections(self) -> list[ContextSection]:
        late_names = {section.name for section in self.late_sections}
        return [section for section in self.source_sections if section.name not in late_names]

    @property
    def working_tail_sections(self) -> list[ContextSection]:
        return deepcopy(self.late_sections)

    @property
    def epoch_sections(self) -> list[ContextSection]:
        return self.baseline_sections


class ContextRenderer:
    """Render a ContextPlan into provider-independent model messages."""

    def render(self, plan: ContextPlan) -> list[Message]:
        def section_text(section: ContextSection) -> str:
            # TaskState/Skill projections already carry a self-describing
            # heading.  Avoid rendering ``task_state: task_state:`` while
            # retaining headings for ordinary source sections.
            prefix = f"{section.name}:"
            return (
                section.content
                if section.content.startswith(prefix)
                else f"{prefix}\n{section.content}"
            )

        late_names = {section.name for section in plan.late_sections}
        baseline_sections = [
            section for section in plan.source_sections if section.name not in late_names
        ]
        stable = [section for section in baseline_sections if section.stable]
        dynamic = [section for section in baseline_sections if not section.stable]

        # Keep the base/project pair as the first block.  That small
        # compatibility detail lets existing embedders append their own
        # project instructions while the provider serializer still sees the
        # same stable prefix.  The remaining epoch sections retain their
        # boundaries as additional TextBlocks; provider adapters concatenate
        # them in order, while tests and observability seams can distinguish
        # runtime/catalog changes from the stable core.
        core_names = {"base_system_instructions", "project_instructions"}
        core = [section for section in stable if section.name in core_names]
        epoch_tail = [section for section in stable if section.name not in core_names]
        system_content: list[TextBlock] = []
        core_text = "\n\n".join(section.content for section in core if section.content)
        if core_text:
            system_content.append(TextBlock(text=core_text))
        tail_text = "\n\n".join(
            section_text(section)
            for section in (*epoch_tail, *dynamic)
            if section.content
        )
        if tail_text:
            separator = "\n\n" if system_content else ""
            system_content.append(TextBlock(text=separator + tail_text))
        messages = [Message(role="system", content=system_content)]
        history = [
            deepcopy(message)
            for message in plan.compacted_history
            if message.role != "system"
        ]
        messages.extend(history)
        if plan.current_input is not None and (
            not history or history[-1] != plan.current_input
        ):
            messages.append(deepcopy(plan.current_input))
        if plan.late_sections:
            late_text = "\n\n".join(
                section_text(section)
                for section in plan.late_sections
                if section.content
            )
            if late_text:
                messages.append(
                    Message(role="system", content=[TextBlock(text=late_text)])
                )
        return messages


class ContextManager:
    """Own source assembly, history policy hooks, rendering and budget checks."""

    def __init__(
        self,
        *,
        base_system_instructions: BaseSystemInstructions | None = None,
        project_instructions_provider: ProjectInstructionsProvider | None = None,
        budget: ContextBudgetPolicy | None = None,
        history_selector: HistorySelector | None = None,
        history_compactor: HistoryCompactor | None = None,
        pressure_pruner: ToolResultPruner | None = None,
        recent_tail_ratio: float = 0.20,
        renderer: ContextRenderer | None = None,
        skill_catalog: object | None = None,
        available_skill_catalog: object | None = None,
        skill_state: object | None = None,
    ) -> None:
        self.base_system_instructions = (
            base_system_instructions or BaseSystemInstructions()
        )
        self.project_instructions_provider = (
            project_instructions_provider or StaticProjectInstructionsProvider()
        )
        self.budget = budget or ContextBudget(
            context_window_tokens=DEFAULT_CONTEXT_WINDOW_TOKENS,
            output_tokens=None,
        )
        self.history_selector = history_selector or NoOpHistorySelector()
        self.history_compactor = history_compactor or NoOpHistoryCompactor()
        self.pressure_pruner = pressure_pruner or ToolResultPruner(
            estimator=self.budget.estimator
        )
        if (
            isinstance(recent_tail_ratio, bool)
            or not isinstance(recent_tail_ratio, (int, float))
            or not 0 < recent_tail_ratio <= 1
        ):
            raise ValueError("recent_tail_ratio must be greater than zero and at most one")
        self.recent_tail_ratio = float(recent_tail_ratio)
        self.renderer = renderer or ContextRenderer()
        self.skill_catalog = (
            skill_catalog if skill_catalog is not None else available_skill_catalog
        )
        self.skill_state = skill_state
        self._project_snapshot: ProjectInstructions | None = None
        self._project_snapshot_workspace: Path | None = None

    def freeze_project_instructions(self, workspace: str | Path) -> ProjectInstructions:
        """Load project instructions once and reuse the immutable Turn snapshot."""

        normalized = Path(workspace)
        if self._project_snapshot is None:
            project = self.project_instructions_provider.load(normalized)
            if not isinstance(project, ProjectInstructions):
                raise ValueError(
                    "project instructions provider must return ProjectInstructions"
                )
            self._project_snapshot = deepcopy(project)
            self._project_snapshot_workspace = normalized
        return deepcopy(self._project_snapshot)

    def assemble(
        self,
        canonical_history: Sequence[Message],
        *,
        current_input: str | Message | None = None,
        runtime_context: RuntimeContext | None = None,
        task_state: TaskState | None = None,
        tools: Sequence[ToolDefinition] = (),
        skill_catalog: object | None = None,
        skill_state: object | None = None,
    ) -> ContextPlan:
        """Build a detached plan and reject an oversized request explicitly.

        This synchronous compatibility seam applies the configured legacy
        selector/compactor only.  Runtime model steps use
        :meth:`assemble_with_reduction` so pressure follows the bounded V1
        prune/select/compact pipeline.
        """

        snapshot = _messages(canonical_history, "canonical history")
        selected = _messages(
            self.history_selector.select(deepcopy(snapshot)),
            "selected history",
        )
        compacted = _messages(
            self.history_compactor.compact(deepcopy(selected)),
            "compacted history",
        )
        metadata = {
            "selector": type(self.history_selector).__name__,
            "compactor": type(self.history_compactor).__name__,
            "canonical_history_messages": len(snapshot),
            "selected_history_messages": len(selected),
            "compacted_history_messages": len(compacted),
        }
        plan = self._build_plan(
            selected_history=selected,
            compacted_history=compacted,
            current_input=current_input,
            runtime_context=runtime_context,
            task_state=task_state,
            tools=tools,
            compaction_summary=None,
            metadata=metadata,
            skill_catalog=skill_catalog,
            skill_state=skill_state,
            include_late_tail=False,
        )
        if plan.final_fit == "overflow":
            self.budget.ensure_fits(self.renderer.render(plan), list(tools))
        return plan

    async def assemble_with_reduction(
        self,
        canonical_history: Sequence[Message],
        *,
        current_input: str | Message | None = None,
        runtime_context: RuntimeContext | None = None,
        task_state: TaskState | None = None,
        tools: Sequence[ToolDefinition] = (),
        checkpoint: CompactionCheckpoint | None = None,
        semantic_compactor: AsyncHistoryCompactor | RollingSemanticCompactor | None = None,
        skill_catalog: object | None = None,
        skill_state: object | None = None,
    ) -> tuple[ContextPlan, CompactionCheckpoint | None]:
        """Assemble one request with at most one prune and one compaction.

        Every operation works on detached copies.  A successful semantic
        compaction returns a new checkpoint to the Thread owner; failures
        leave the supplied checkpoint and canonical history untouched.
        """

        snapshot = _messages(canonical_history, "canonical history")
        valid_checkpoint = (
            checkpoint
            if checkpoint is not None and checkpoint.valid_for_history(snapshot)
            else None
        )
        checkpoint_reused = valid_checkpoint is not None
        checkpoint_validation = (
            "reused"
            if checkpoint_reused
            else "new"
            if checkpoint is None
            else "invalidated"
        )
        visible_start = (
            valid_checkpoint.covered_through + 1 if valid_checkpoint is not None else 0
        )
        visible_history = deepcopy(snapshot[visible_start:])
        summary = None if valid_checkpoint is None else valid_checkpoint.summary
        metadata: dict[str, object] = {
            "selector": "RecentRawTailSelector",
            "compactor": (
                "none" if semantic_compactor is None else type(semantic_compactor).__name__
            ),
            "canonical_history_messages": len(snapshot),
            "checkpoint_reused": checkpoint_reused,
            "checkpoint_validation": checkpoint_validation,
            "checkpoint_new_state": not checkpoint_reused,
            "checkpoint_covered_through": (
                None if valid_checkpoint is None else valid_checkpoint.covered_through
            ),
            "compaction_performed": False,
            "tool_results_pruned": 0,
            "reduction_attempts": {"tool_prune": 0, "compaction": 0},
        }
        initial = self._build_plan(
            selected_history=visible_history,
            compacted_history=visible_history,
            current_input=current_input,
            runtime_context=runtime_context,
            task_state=task_state,
            tools=tools,
            compaction_summary=summary,
            metadata=metadata,
            skill_catalog=skill_catalog,
            skill_state=skill_state,
            include_late_tail=True,
        )
        metadata["initial_estimated_input_tokens"] = initial.estimated_tokens
        metadata["initial_pressure"] = initial.pressure
        if initial.pressure == "normal":
            return self._with_metadata(initial, metadata), valid_checkpoint

        # Layer 2 begins with the cheapest deterministic operation.  Pruning
        # preserves message positions, which lets selector boundaries remain
        # canonical even though contents are detached.
        pruning = self.pressure_pruner.prune(snapshot)
        metadata["reduction_attempts"] = {"tool_prune": 1, "compaction": 0}
        metadata["tool_results_pruned"] = pruning.pruned_count
        metadata["pruning_before_history_tokens"] = pruning.before_tokens
        metadata["pruning_after_history_tokens"] = pruning.after_tokens
        pruned_visible = deepcopy(list(pruning)[visible_start:])
        after_prune = self._build_plan(
            selected_history=pruned_visible,
            compacted_history=pruned_visible,
            current_input=current_input,
            runtime_context=runtime_context,
            task_state=task_state,
            tools=tools,
            compaction_summary=summary,
            metadata=metadata,
            skill_catalog=skill_catalog,
            skill_state=skill_state,
            include_late_tail=True,
        )
        metadata["pruning_before_estimated_input_tokens"] = initial.estimated_tokens
        metadata["pruning_after_estimated_input_tokens"] = after_prune.estimated_tokens
        metadata["pressure_after_pruning"] = after_prune.pressure
        if after_prune.pressure == "normal":
            return self._with_metadata(after_prune, metadata), valid_checkpoint

        selector = RecentRawTailSelector(
            self.budget.usable_input_tokens,
            target_ratio=self.recent_tail_ratio,
            estimator=self.budget.estimator,
        )
        selection = selector.select(list(pruning), checkpoint=valid_checkpoint)
        if after_prune.final_fit == "overflow" and semantic_compactor is not None:
            # The selector deliberately retains the first atomic unit that
            # crosses its raw-tail target.  A single very large interaction
            # can therefore consume the whole hard budget while leaving only
            # an earlier ordinary message as a compaction candidate.  Under
            # hard pressure, promote the retained closed prefix through the
            # largest eligible unit so semantic compaction still has a
            # protocol-safe region to summarize.
            promoted = _promote_retained_prefix_for_compaction(
                selection,
                estimator=self.budget.estimator,
            )
            if promoted is not selection:
                selection = promoted
                metadata["selector_promoted_for_compaction"] = True
        metadata.update(
            {
                "selector_compact_boundary": selection.boundary,
                "selector_compact_end": selection.canonical_end,
                "retained_raw_tail_tokens": selection.retained_tokens,
                "retained_raw_tail_messages": len(selection),
                "compact_candidate_messages": len(selection.compact_candidates),
                "selector_metadata": selection.metadata,
            }
        )

        if not selection.compact_candidates:
            candidate = self._build_plan(
                selected_history=list(selection),
                compacted_history=list(selection),
                current_input=current_input,
                runtime_context=runtime_context,
                task_state=task_state,
                tools=tools,
                compaction_summary=summary,
                metadata=metadata,
                skill_catalog=skill_catalog,
                skill_state=skill_state,
                include_late_tail=True,
            )
            metadata["post_compaction_estimated_input_tokens"] = candidate.estimated_tokens
            if candidate.final_fit == "overflow":
                self.budget.ensure_fits(self.renderer.render(candidate), list(tools))
            return self._with_metadata(candidate, metadata), valid_checkpoint

        if semantic_compactor is None:
            # A compatibility caller may choose not to provide the expensive
            # semantic seam.  Soft pressure still fits safely; a hard request
            # remains an explicit limit rather than silently dropping history.
            if after_prune.final_fit == "overflow":
                self.budget.ensure_fits(self.renderer.render(after_prune), list(tools))
            return self._with_metadata(after_prune, metadata), valid_checkpoint

        metadata["reduction_attempts"] = {"tool_prune": 1, "compaction": 1}
        rolling = (
            semantic_compactor
            if isinstance(semantic_compactor, RollingSemanticCompactor)
            else RollingSemanticCompactor(
                semantic_compactor,
                estimator=self.budget.estimator,
            )
        )
        try:
            compacted = await rolling.compact(
                snapshot,
                selection,
                previous_checkpoint=valid_checkpoint,
            )
        except CompactionError as error:
            if error.previous_checkpoint is None:
                error.previous_checkpoint = valid_checkpoint  # type: ignore[misc]
            raise
        if compacted is None:
            if after_prune.final_fit == "overflow":
                self.budget.ensure_fits(self.renderer.render(after_prune), list(tools))
            return self._with_metadata(after_prune, metadata), valid_checkpoint

        metadata.update(
            {
                "compaction_performed": True,
                "previous_checkpoint_reused": compacted.metadata.get(
                    "previous_checkpoint_reused", False
                ),
                "checkpoint_covered_through": compacted.checkpoint.covered_through,
                "checkpoint_source_messages": compacted.metadata.get("source_messages", 0),
            }
        )
        final = self._build_plan(
            selected_history=list(selection),
            compacted_history=list(selection),
            current_input=current_input,
            runtime_context=runtime_context,
            task_state=task_state,
            tools=tools,
            compaction_summary=compacted.summary,
            metadata=metadata,
            skill_catalog=skill_catalog,
            skill_state=skill_state,
            include_late_tail=True,
        )
        metadata["post_compaction_estimated_input_tokens"] = final.estimated_tokens
        metadata["final_fits"] = final.final_fit == "fits"
        if final.final_fit == "overflow":
            self.budget.ensure_fits(self.renderer.render(final), list(tools))
        return self._with_metadata(final, metadata), compacted.checkpoint

    def _build_plan(
        self,
        *,
        selected_history: Sequence[Message],
        compacted_history: Sequence[Message],
        current_input: str | Message | None,
        runtime_context: RuntimeContext | None,
        task_state: TaskState | None,
        tools: Sequence[ToolDefinition],
        compaction_summary: CompactionSummary | None,
        metadata: dict[str, object],
        skill_catalog: object | None = None,
        skill_state: object | None = None,
        include_late_tail: bool = False,
    ) -> ContextPlan:
        if runtime_context is not None and not isinstance(runtime_context, RuntimeContext):
            raise ValueError("runtime_context must be RuntimeContext")
        if task_state is not None and not isinstance(task_state, TaskState):
            raise ValueError("task_state must be TaskState")
        current = _current_input(current_input)
        workspace = Path(runtime_context.workspace) if runtime_context is not None else Path(".")
        project = self.freeze_project_instructions(workspace)
        has_task_state = task_state is not None and (
            task_state.plan is not None or bool(task_state.evidence)
        )
        sources = ContextSources(
            base_system_instructions=self.base_system_instructions,
            project_instructions=project,
            runtime_context=runtime_context,
            compaction_summary=compaction_summary,
            # A runtime assembly places non-empty mutable TaskState only in
            # the late working tail.  Keep the empty ``plan: none`` source in
            # its historical baseline slot so an otherwise unchanged V1
            # request does not gain a synthetic trailing message.
            task_state=None if include_late_tail and has_task_state else task_state,
            available_skills=(
                skill_catalog if skill_catalog is not None else self.skill_catalog
            ),
            # Keep the catalog independently bounded and leave headroom for
            # the actual task/history even when the selected model has a
            # small context window.  Direct catalog callers retain the
            # documented 8,000-character default.
            available_skills_max_chars=(
                8_000
                if self.budget.usable_input_tokens >= 12_000
                else max(256, self.budget.usable_input_tokens // 16)
            ),
        )
        source_sections = sources.sections()
        selected_skill_state = skill_state if skill_state is not None else self.skill_state
        late_sections: list[ContextSection] = []
        if include_late_tail:
            if has_task_state:
                task_view = task_state.view(estimator=self.budget.estimator)
                late_sections.append(
                    ContextSection(
                        "task_state",
                        task_view.text,
                        stable=False,
                        placement="late",
                    )
                )
            if selected_skill_state is not None:
                projection = getattr(selected_skill_state, "projection", None)
                loaded_text = projection() if callable(projection) else ""
                if loaded_text:
                    late_sections.append(
                        ContextSection(
                            "loaded_skills",
                            loaded_text,
                            stable=False,
                            placement="late",
                        )
                    )
        plan_metadata = deepcopy(metadata)
        plan_metadata.update(
            {
                "selected_history_messages": len(selected_history),
                "compacted_history_messages": len(compacted_history),
                "project_instructions": project.metadata,
                "project_instructions_snapshot_workspace": str(
                    self._project_snapshot_workspace or workspace
                ),
                "synthetic_compaction_summary": compaction_summary is not None,
                "epoch_sections": [
                    section.name for section in source_sections if section.name != "task_state"
                ],
                "late_working_tail_sections": [section.name for section in late_sections],
                "loaded_skill_names": list(
                    getattr(selected_skill_state, "loaded_names", ())
                ),
                "available_skill_count": len(
                    getattr(
                        (skill_catalog if skill_catalog is not None else self.skill_catalog),
                        "skills",
                        (),
                    )
                ),
                "history_estimate_tokens": self.budget.estimator.estimate(
                    [
                        message
                        for message in compacted_history
                        if message.role != "system"
                    ],
                    (),
                ),
                "task_state_view_estimate_tokens": (
                    0
                    if task_state is None
                    else task_state.view(estimator=self.budget.estimator).estimated_tokens
                ),
            }
        )
        provisional = ContextPlan(
            source_sections=source_sections,
            selected_history=list(selected_history),
            compacted_history=list(compacted_history),
            current_input=current,
            estimated_tokens=0,
            input_budget_tokens=self.budget.input_budget_tokens,
            budget_status="fits",
            decision_metadata=plan_metadata,
            late_sections=late_sections,
        )
        estimated = self.budget.estimate_tokens(self.renderer.render(provisional), list(tools))
        assessment = self.budget.assess(estimated)
        plan_metadata.update(
            {
                "estimated_input_tokens": assessment.estimated_input_tokens,
                "final_request_estimate_tokens": assessment.estimated_input_tokens,
                "final_request_fit": "fits" if assessment.fits else "overflow",
                "reserved_output_tokens": assessment.reserved_output_tokens,
                "safety_margin_tokens": assessment.safety_margin_tokens,
                "usable_input_tokens": assessment.usable_input_tokens,
                "soft_limit_tokens": assessment.soft_limit_tokens,
                "pressure": assessment.pressure,
                "final_fit": "fits" if assessment.fits else "overflow",
                "context_epoch_sections": [
                    section.name
                    for section in source_sections
                    if section.name not in {item.name for item in late_sections}
                ],
            }
        )
        return ContextPlan(
            source_sections=source_sections,
            selected_history=list(selected_history),
            compacted_history=list(compacted_history),
            current_input=current,
            estimated_tokens=estimated,
            input_budget_tokens=self.budget.input_budget_tokens,
            budget_status="fits" if assessment.fits else "exceeds",
            decision_metadata=plan_metadata,
            pressure=assessment.pressure,
            final_fit="fits" if assessment.fits else "overflow",
            late_sections=late_sections,
        )

    @staticmethod
    def _with_metadata(plan: ContextPlan, metadata: dict[str, object]) -> ContextPlan:
        merged = deepcopy(plan.decision_metadata)
        merged.update(deepcopy(metadata))
        merged["estimated_input_tokens"] = plan.estimated_tokens
        merged["final_request_estimate_tokens"] = plan.estimated_tokens
        merged["final_request_fit"] = "fits" if plan.final_fit == "fits" else "overflow"
        merged["pressure"] = plan.pressure
        merged["final_fit"] = plan.final_fit
        merged["final_fits"] = plan.final_fit == "fits"
        return ContextPlan(
            source_sections=plan.source_sections,
            selected_history=plan.selected_history,
            compacted_history=plan.compacted_history,
            current_input=plan.current_input,
            estimated_tokens=plan.estimated_tokens,
            input_budget_tokens=plan.input_budget_tokens,
            budget_status=plan.budget_status,
            decision_metadata=merged,
            pressure=plan.pressure,
            final_fit=plan.final_fit,
            late_sections=plan.late_sections,
        )

    def render(self, plan: ContextPlan) -> list[Message]:
        return self.renderer.render(plan)


def _path_text(value: str | Path, name: str) -> str:
    if isinstance(value, Path):
        result = value.as_posix()
    elif isinstance(value, str):
        result = value.strip()
    else:
        raise ValueError(f"{name} must be a path or string")
    if not result:
        raise ValueError(f"{name} must be non-empty")
    return result


def _text_tuple(value: Sequence[str] | str, name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    try:
        values = tuple(value)
    except TypeError as error:
        raise ValueError(f"{name} must be text or a sequence of text") from error
    if any(not isinstance(item, str) for item in values):
        raise ValueError(f"{name} must contain only text")
    return values


def _messages(value: Sequence[Message], name: str) -> list[Message]:
    try:
        messages = list(value)
    except TypeError as error:
        raise ValueError(f"{name} must be a sequence of Message") from error
    if any(not isinstance(message, Message) for message in messages):
        raise ValueError(f"{name} must contain Message values")
    return deepcopy(messages)


def _promote_retained_prefix_for_compaction(
    selection: HistorySelection,
    *,
    estimator: object | None = None,
) -> HistorySelection:
    """Expose a large retained atomic prefix to the one compaction pass.

    ``RecentRawTailSelector`` keeps a boundary unit whole by design.  That
    rule is important for protocol safety, but a large closed interaction at
    the boundary can make an otherwise reducible request impossible to fit.
    This helper is used only after a hard post-prune estimate and moves the
    retained closed prefix through the largest eligible unit.  The moved
    units remain complete and chronological; the newest tool interaction and
    every open unit stay in the raw tail.
    """

    retained_units = sorted(
        selection.retained_units,
        key=lambda unit: unit.start_index,
    )
    if len(retained_units) < 2:
        return selection
    latest_tool_start = max(
        (
            unit.start_index
            for unit in retained_units
            if unit.is_tool_interaction
        ),
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

    chosen = max(
        eligible,
        key=lambda unit: (unit_tokens(unit), unit.start_index),
    )
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
    remaining = [
        unit for unit in retained_units if unit.start_index not in moved_indexes
    ]
    compact_units = sorted(
        [*selection.compact_units, *moved],
        key=lambda unit: unit.start_index,
    )

    def flatten(units: Sequence[AtomicHistoryUnit]) -> list[Message]:
        return [
            deepcopy(message)
            for unit in units
            for message in unit.messages
        ]

    selected = flatten(remaining)
    compact_candidates = flatten(
        [
            unit
            for unit in compact_units
            if not any(message.role == "system" for message in unit.messages)
        ]
    )
    non_system_retained = [
        unit
        for unit in remaining
        if not any(message.role == "system" for message in unit.messages)
    ]
    boundary = (
        min(unit.start_index for unit in non_system_retained)
        if non_system_retained
        else None
    )
    canonical_end = max(
        (unit.end_index for unit in compact_units),
        default=None,
    )
    retained_tokens = (
        estimate_history_tokens(selected, estimator) if selected else 0
    )
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


def _current_input(value: str | Message | None) -> Message | None:
    if value is None:
        return None
    if isinstance(value, Message):
        return deepcopy(value)
    if isinstance(value, str):
        return Message(role="user", content=[TextBlock(text=value)])
    raise ValueError("current_input must be text, Message, or None")


__all__ = [
    "BaseSystemInstructions",
    "ContextBudget",
    "ContextBudgetPolicy",
    "DEFAULT_CONTEXT_WINDOW_TOKENS",
    "AsyncHistoryCompactor",
    "AtomicHistoryParser",
    "AtomicInteractionUnit",
    "AtomicHistoryUnit",
    "CommandEvidence",
    "Evidence",
    "EvidenceKind",
    "EvidenceStatus",
    "TaskEvidence",
    "TaskStateView",
    "CompactionCheckpoint",
    "CompactionError",
    "CompactionSummary",
    "ContextManager",
    "ContextPlan",
    "ContextRenderer",
    "ContextSection",
    "ContextSource",
    "ContextSources",
    "HistoryCompactor",
    "HistoryPruner",
    "HistorySelection",
    "LLMHistoryCompactor",
    "HistorySelector",
    "NoOpHistoryCompactor",
    "NoOpHistorySelector",
    "OldToolResultPruner",
    "PressurePruner",
    "PressureToolResultPruner",
    "PruningResult",
    "RecentRawTailSelector",
    "RecentTailSelector",
    "FileProjectInstructionsProvider",
    "HarnessEvidence",
    "MAX_PROJECT_INSTRUCTIONS_BYTES",
    "MAX_PLAN_STEP_TEXT_CHARS",
    "MAX_RECENT_COMPLETED_MILESTONES",
    "MAX_TASK_PLAN_STEP_TEXT_CHARS",
    "MAX_EVIDENCE_TOOL_CHARS",
    "MAX_EVIDENCE_COMMAND_CHARS",
    "MAX_EVIDENCE_STATUS_CHARS",
    "MAX_EVIDENCE_RESULT_ID_CHARS",
    "MAX_EVIDENCE_TIMESTAMP_CHARS",
    "MAX_COMMAND_EVIDENCE_STRING_CHARS",
    "MAX_TASK_EVIDENCE",
    "MAX_TASK_PLAN_STEPS",
    "PlanStep",
    "PlanStepStatus",
    "ProjectInstructions",
    "ProjectInstructionsProvider",
    "PROJECT_INSTRUCTIONS_MAX_BYTES",
    "RootProjectInstructionsProvider",
    "RollingCompactionResult",
    "RollingCompactor",
    "RollingSemanticCompactor",
    "RuntimeContext",
    "StaticProjectInstructionsProvider",
    "TaskPlan",
    "TaskState",
    "ToolResultPruner",
    "ToolResultPressurePruner",
    "WorkspaceProjectInstructionsProvider",
    "estimate_history_tokens",
    "parse_atomic_history",
    "prune_old_tool_results",
]
