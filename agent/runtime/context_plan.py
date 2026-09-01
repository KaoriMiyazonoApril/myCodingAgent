"""Detached source and plan data for model context assembly.

This module intentionally contains no orchestration.  ``ContextManager`` owns
the reduction pipeline while these value objects describe the inputs and the
result of one request.  Keeping the data model in a small module makes the
context seams useful to tests and embedders without importing the manager.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field

from agent.core.messages import Message

from .context_types import BaseSystemInstructions, ContextSection
from .history import CompactionSummary
from .instructions import ProjectInstructions
from .runtime_environment import RuntimeContext
from .task_state import TaskState


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


__all__ = ["ContextPlan", "ContextSources"]
