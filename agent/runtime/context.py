"""Provider-independent model context assembly.

``Conversation`` owns the durable, complete Thread transcript.  This module
turns a detached transcript plus request-scoped sources into the messages a
model sees.  The default history policies deliberately do nothing so that
future selection and compaction cannot silently change durable history.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
import os
from pathlib import Path
from typing import Protocol

from agent.core.messages import Message, TextBlock
from agent.tools.types import ToolDefinition

from .context_budget import (
    ContextBudget,
    ContextBudgetPolicy,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
)
from .prompt import DEFAULT_SYSTEM_PROMPT
from .settings import ApprovalMode
from .context_history import (
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


MAX_PROJECT_INSTRUCTIONS_BYTES = 64 * 1024
PROJECT_INSTRUCTIONS_MAX_BYTES = MAX_PROJECT_INSTRUCTIONS_BYTES
MAX_TASK_PLAN_STEPS = 20
MAX_TASK_EVIDENCE = 100
# These limits are validation boundaries, not silent truncation.  A malformed
# or adversarial update_plan/evidence item is rejected before it can consume a
# whole context request, while valid short values retain their exact text.
MAX_PLAN_STEP_TEXT_CHARS = 2_000
MAX_EVIDENCE_TOOL_CHARS = 128
MAX_EVIDENCE_COMMAND_CHARS = 8_192
MAX_EVIDENCE_STATUS_CHARS = 128
MAX_EVIDENCE_RESULT_ID_CHARS = 256
MAX_EVIDENCE_TIMESTAMP_CHARS = 128
# Descriptive aliases keep the public seam discoverable for callers that use
# the domain terms rather than the per-field constants.
MAX_TASK_PLAN_STEP_TEXT_CHARS = MAX_PLAN_STEP_TEXT_CHARS
MAX_COMMAND_EVIDENCE_STRING_CHARS = MAX_EVIDENCE_COMMAND_CHARS


@dataclass(frozen=True, slots=True)
class ContextSection:
    """One named source section included in a request plan."""

    name: str
    content: str
    stable: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("context section name must be non-empty")
        if not isinstance(self.content, str):
            raise ValueError("context section content must be text")
        if not isinstance(self.stable, bool):
            raise ValueError("context section stable flag must be boolean")


class ContextSource(Protocol):
    """A source that contributes ordered sections to a context plan."""

    def sections(self) -> Sequence[ContextSection]:
        """Return detached sections in deterministic order."""


@dataclass(frozen=True, slots=True)
class BaseSystemInstructions:
    """Stable agent principles shared by every model request."""

    text: str = DEFAULT_SYSTEM_PROMPT

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("base system instructions must be non-empty text")

    def sections(self) -> list[ContextSection]:
        return [ContextSection("base_system_instructions", self.text)]


@dataclass(frozen=True, slots=True)
class ProjectInstructions:
    """Immutable project guidance returned by a project provider."""

    text: str = ""
    source: str = ""
    original_bytes: int = 0
    retained_bytes: int = 0
    omitted_bytes: int = 0
    truncated: bool = False
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("project instructions must be text")
        if not isinstance(self.source, str):
            raise ValueError("project instructions source must be text")
        for name, value in (
            ("original_bytes", self.original_bytes),
            ("retained_bytes", self.retained_bytes),
            ("omitted_bytes", self.omitted_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.truncated, bool):
            raise ValueError("project instructions truncated must be boolean")
        if not isinstance(self.diagnostics, tuple) or any(
            not isinstance(item, str) for item in self.diagnostics
        ):
            raise ValueError("project instructions diagnostics must be text tuple")
        retained = len(self.text.encode("utf-8"))
        if self.retained_bytes == 0 and self.text:
            object.__setattr__(self, "retained_bytes", retained)
        if self.original_bytes == 0 and self.text:
            object.__setattr__(self, "original_bytes", retained)

    def sections(self) -> list[ContextSection]:
        if not self.text:
            return []
        return [ContextSection("project_instructions", self.text)]

    @property
    def loaded(self) -> bool:
        load_failures = {"AGENTS_INVALID_UTF8", "AGENTS_READ_ERROR"}
        return bool(self.source) and not load_failures.intersection(self.diagnostics)

    @property
    def metadata(self) -> dict[str, object]:
        """Detached diagnostics suitable for ContextPlan metadata."""

        return {
            "source": self.source,
            "loaded": self.loaded,
            "original_bytes": self.original_bytes,
            "retained_bytes": self.retained_bytes,
            "omitted_bytes": self.omitted_bytes,
            "truncated": self.truncated,
            "diagnostics": list(self.diagnostics),
        }


class ProjectInstructionsProvider(Protocol):
    """Resolve project guidance without touching canonical history."""

    def load(self, workspace: Path) -> ProjectInstructions:
        """Return instructions for the supplied workspace."""


class StaticProjectInstructionsProvider:
    """Provider for Runtime-supplied instructions.

    Filesystem discovery is intentionally outside this first architecture
    slice; a later provider can implement it behind this interface.
    """

    def __init__(self, instructions: ProjectInstructions | str | None = None) -> None:
        self._instructions = (
            instructions
            if isinstance(instructions, ProjectInstructions)
            else ProjectInstructions(instructions or "")
        )

    def load(self, workspace: Path) -> ProjectInstructions:
        del workspace
        return self._instructions


class RootProjectInstructionsProvider:
    """Load exactly ``<workspace>/AGENTS.md`` with a bounded UTF-8 read.

    The provider intentionally does not search parents, nested directories,
    git roots or alternate filenames.  It is called behind the ContextManager
    provider seam, so a manager can freeze one result for a whole Turn.
    """

    filename = "AGENTS.md"

    def __init__(
        self,
        *,
        max_bytes: int = MAX_PROJECT_INSTRUCTIONS_BYTES,
        additional_instructions: str | None = None,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        if additional_instructions is not None and not isinstance(
            additional_instructions, str
        ):
            raise ValueError("additional_instructions must be text or None")
        self.max_bytes = max_bytes
        self.additional_instructions = additional_instructions or ""

    def load(self, workspace: Path) -> ProjectInstructions:
        root = Path(workspace)
        path = root / self.filename
        try:
            metadata = path.stat()
            original_bytes = max(0, int(metadata.st_size))
            with path.open("rb") as source:
                raw = source.read(self.max_bytes + 1)
            if len(raw) > self.max_bytes:
                # ``stat`` is the truthful fast path.  A concurrent writer
                # can only make the lower bound stale, so use the bytes read
                # as a safe fallback and preserve deterministic diagnostics.
                original_bytes = max(original_bytes, len(raw))
            if original_bytes > self.max_bytes or len(raw) > self.max_bytes:
                text, retained, omitted = _bounded_instruction_text(
                    raw[: self.max_bytes],
                    original_bytes,
                    self.max_bytes,
                )
                instructions = ProjectInstructions(
                    text=text,
                    source=self.filename,
                    original_bytes=original_bytes,
                    retained_bytes=retained,
                    omitted_bytes=omitted,
                    truncated=True,
                    diagnostics=("AGENTS_TRUNCATED",),
                )
            else:
                text = raw.decode("utf-8")
                instructions = ProjectInstructions(
                    text=text,
                    source=self.filename,
                    original_bytes=len(raw),
                    retained_bytes=len(raw),
                )
        except FileNotFoundError:
            instructions = ProjectInstructions()
        except UnicodeDecodeError:
            instructions = ProjectInstructions(
                source=self.filename,
                diagnostics=("AGENTS_INVALID_UTF8",),
            )
        except (OSError, RuntimeError):
            instructions = ProjectInstructions(
                source=self.filename,
                diagnostics=("AGENTS_READ_ERROR",),
            )

        if not self.additional_instructions:
            return instructions
        combined = (
            f"{instructions.text}\n\n{self.additional_instructions}"
            if instructions.text
            else self.additional_instructions
        )
        combined_bytes = len(combined.encode("utf-8"))
        return ProjectInstructions(
            text=combined,
            source=instructions.source,
            original_bytes=instructions.original_bytes,
            retained_bytes=combined_bytes,
            omitted_bytes=instructions.omitted_bytes,
            truncated=instructions.truncated,
            diagnostics=instructions.diagnostics,
        )


# Descriptive aliases keep the provider seam discoverable to embedders.
FileProjectInstructionsProvider = RootProjectInstructionsProvider
WorkspaceProjectInstructionsProvider = RootProjectInstructionsProvider


def _bounded_instruction_text(
    raw_prefix: bytes,
    original_bytes: int,
    max_bytes: int,
) -> tuple[str, int, int]:
    marker_template = (
        "\n...[AGENTS.md truncated: original_bytes={original}; "
        "omitted_bytes={omitted}]...\n"
    )
    omitted_guess = max(0, original_bytes - len(raw_prefix))
    marker = marker_template.format(
        original=original_bytes,
        omitted=omitted_guess,
    ).encode("ascii")
    for _ in range(8):
        prefix_budget = max(0, max_bytes - len(marker))
        prefix = raw_prefix[:prefix_budget]
        while prefix:
            try:
                prefix.decode("utf-8")
                break
            except UnicodeDecodeError:
                prefix = prefix[:-1]
        retained_prefix = len(prefix)
        omitted = max(0, original_bytes - retained_prefix)
        next_marker = marker_template.format(
            original=original_bytes,
            omitted=omitted,
        ).encode("ascii")
        if next_marker == marker:
            break
        marker = next_marker
    if len(marker) >= max_bytes:
        marker = marker[:max_bytes]
        prefix = b""
    else:
        prefix_budget = max_bytes - len(marker)
        prefix = raw_prefix[:prefix_budget]
        while prefix:
            try:
                prefix.decode("utf-8")
                break
            except UnicodeDecodeError:
                prefix = prefix[:-1]
    omitted = max(0, original_bytes - len(prefix))
    marker = marker_template.format(
        original=original_bytes,
        omitted=omitted,
    ).encode("ascii")
    if len(marker) > max_bytes:
        marker = marker[:max_bytes]
        prefix = b""
    result = (prefix + marker)[:max_bytes]
    return result.decode("utf-8", errors="strict"), len(result), omitted


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Cheap environment facts collected for each model request."""

    workspace: str | Path
    cwd: str | Path | None = None
    shell: str | None = None
    approval_mode: ApprovalMode = ApprovalMode.ON_REQUEST
    capabilities: tuple[str, ...] = ()
    turn_id: str | None = None

    def __post_init__(self) -> None:
        workspace = _path_text(self.workspace, "workspace")
        cwd = workspace if self.cwd is None else _path_text(self.cwd, "cwd")
        shell = self.shell or os.environ.get("SHELL") or "/bin/sh"
        if not isinstance(shell, str) or not shell.strip():
            raise ValueError("shell must be non-empty text")
        approval_mode = self.approval_mode
        if not isinstance(approval_mode, ApprovalMode):
            try:
                approval_mode = ApprovalMode(approval_mode)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "approval_mode must be an ApprovalMode value"
                ) from error
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "shell", shell.strip())
        object.__setattr__(self, "approval_mode", approval_mode)
        object.__setattr__(
            self, "capabilities", _text_tuple(self.capabilities, "capabilities")
        )
        if self.turn_id is not None and (
            not isinstance(self.turn_id, str) or not self.turn_id.strip()
        ):
            raise ValueError("turn_id must be non-empty text or None")
        object.__setattr__(
            self,
            "turn_id",
            None if self.turn_id is None else self.turn_id.strip(),
        )

    def sections(self) -> list[ContextSection]:
        capability_text = ", ".join(self.capabilities) or "none"
        return [
            ContextSection(
                "runtime_context",
                "\n".join(
                    (
                        f"workspace: {self.workspace}",
                        f"cwd: {self.cwd}",
                        f"shell: {self.shell}",
                        f"approval_mode: {self.approval_mode.value}",
                        f"capabilities: {capability_text}",
                        *( ([f"turn_id: {self.turn_id}"] if self.turn_id else []) ),
                    )
                ),
                stable=False,
            )
        ]


class PlanStepStatus(str, Enum):
    """The only statuses accepted by the model-maintained task plan."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True, init=False)
class PlanStep:
    """One bounded, model-visible plan step."""

    step: str
    status: PlanStepStatus

    def __init__(
        self,
        step: str | None = None,
        status: PlanStepStatus | str = PlanStepStatus.PENDING,
        *,
        description: str | None = None,
    ) -> None:
        if step is None:
            step = description
        if not isinstance(step, str) or not step.strip():
            raise ValueError("plan step text must be non-empty")
        if len(step) > MAX_PLAN_STEP_TEXT_CHARS:
            raise ValueError(
                f"plan step text must be at most {MAX_PLAN_STEP_TEXT_CHARS} characters"
            )
        if description is not None and not isinstance(description, str):
            raise ValueError("plan step description must be text")
        try:
            normalized_status = (
                status
                if isinstance(status, PlanStepStatus)
                else PlanStepStatus(status)
            )
        except (TypeError, ValueError) as error:
            raise ValueError(
                "plan step status must be pending, in_progress, or completed"
            ) from error
        object.__setattr__(self, "step", step)
        object.__setattr__(self, "status", normalized_status)

    @property
    def description(self) -> str:
        """Alias for clients that call the step text a description."""

        return self.step


@dataclass(frozen=True, slots=True, init=False)
class TaskPlan:
    """An atomically replaced plan containing at most twenty steps."""

    steps: tuple[PlanStep, ...]

    def __init__(self, steps: Sequence[PlanStep | Mapping[str, object]] = ()) -> None:
        try:
            values = tuple(steps)
        except TypeError as error:
            raise ValueError("plan steps must be a sequence") from error
        if len(values) > MAX_TASK_PLAN_STEPS:
            raise ValueError(f"task plan may contain at most {MAX_TASK_PLAN_STEPS} steps")
        normalized: list[PlanStep] = []
        for index, value in enumerate(values):
            if isinstance(value, PlanStep):
                step = value
            elif isinstance(value, Mapping):
                raw_step = value.get("step", value.get("description"))
                step = PlanStep(raw_step, value.get("status", PlanStepStatus.PENDING))
            else:
                raise ValueError(f"plan step {index} must be a PlanStep or object")
            normalized.append(step)
        if sum(step.status is PlanStepStatus.IN_PROGRESS for step in normalized) > 1:
            raise ValueError("task plan may contain at most one in_progress step")
        object.__setattr__(self, "steps", tuple(normalized))

    @classmethod
    def from_payload(cls, value: object) -> TaskPlan:
        """Parse the narrow ``update_plan`` argument shape."""

        if isinstance(value, TaskPlan):
            return cls(value.steps)
        if isinstance(value, Mapping):
            value = value.get("steps")
        if value is None:
            return cls()
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise ValueError("update_plan steps must be an array")
        return cls(value)

    def to_dict(self) -> dict[str, object]:
        return {
            "steps": [
                {"step": step.step, "status": step.status.value}
                for step in self.steps
            ]
        }


@dataclass(frozen=True, slots=True, init=False)
class CommandEvidence:
    """Objective fact recorded from one command-capable tool completion."""

    tool: str
    command: str
    status: str
    exit_code: int | None
    result_id: str
    timestamp: str

    def __init__(
        self,
        tool: str,
        command: str,
        status: str,
        exit_code: int | None = None,
        result_id: str = "",
        timestamp: str = "",
    ) -> None:
        required_limits = (
            ("tool", tool, MAX_EVIDENCE_TOOL_CHARS),
            ("command", command, MAX_EVIDENCE_COMMAND_CHARS),
            ("status", status, MAX_EVIDENCE_STATUS_CHARS),
        )
        for name, value, limit in required_limits:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"evidence {name} must be non-empty text")
            if len(value) > limit:
                raise ValueError(
                    f"evidence {name} must be at most {limit} characters"
                )
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            raise ValueError("evidence exit_code must be an integer or None")
        if not isinstance(result_id, str) or not isinstance(timestamp, str):
            raise ValueError("evidence result_id and timestamp must be text")
        if len(result_id) > MAX_EVIDENCE_RESULT_ID_CHARS:
            raise ValueError(
                f"evidence result_id must be at most {MAX_EVIDENCE_RESULT_ID_CHARS} characters"
            )
        if len(timestamp) > MAX_EVIDENCE_TIMESTAMP_CHARS:
            raise ValueError(
                f"evidence timestamp must be at most {MAX_EVIDENCE_TIMESTAMP_CHARS} characters"
            )
        object.__setattr__(self, "tool", tool)
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "exit_code", exit_code)
        object.__setattr__(self, "result_id", result_id)
        object.__setattr__(self, "timestamp", timestamp)

    @property
    def tool_name(self) -> str:
        return self.tool


HarnessEvidence = CommandEvidence


@dataclass(slots=True, init=False)
class TaskState:
    """Turn-local model plan plus Harness-owned objective command evidence."""

    plan: TaskPlan | None
    evidence: tuple[CommandEvidence, ...]

    def __init__(
        self,
        plan: TaskPlan | Sequence[PlanStep | Mapping[str, object]] | None = None,
        evidence: Sequence[CommandEvidence] = (),
        *,
        task_plan: TaskPlan | Sequence[PlanStep | Mapping[str, object]] | None = None,
        command_evidence: Sequence[CommandEvidence] | None = None,
    ) -> None:
        if plan is not None and task_plan is not None:
            raise ValueError("provide only one plan value")
        selected_plan = task_plan if task_plan is not None else plan
        normalized_plan = (
            None
            if selected_plan is None
            else selected_plan
            if isinstance(selected_plan, TaskPlan)
            else TaskPlan(selected_plan)
        )
        selected_evidence = evidence if command_evidence is None else command_evidence
        try:
            evidence_values = tuple(selected_evidence)
        except TypeError as error:
            raise ValueError("task evidence must be a sequence") from error
        if len(evidence_values) > MAX_TASK_EVIDENCE:
            raise ValueError(f"task evidence may contain at most {MAX_TASK_EVIDENCE} items")
        if any(not isinstance(item, CommandEvidence) for item in evidence_values):
            raise ValueError("task evidence must contain CommandEvidence values")
        self.plan = normalized_plan
        self.evidence = evidence_values

    @property
    def task_plan(self) -> TaskPlan | None:
        return self.plan

    @property
    def command_evidence(self) -> tuple[CommandEvidence, ...]:
        return self.evidence

    def replace_plan(
        self,
        plan: TaskPlan | Sequence[PlanStep | Mapping[str, object]] | None,
    ) -> TaskPlan | None:
        normalized = (
            None
            if plan is None
            else plan
            if isinstance(plan, TaskPlan)
            else TaskPlan(plan)
        )
        self.plan = normalized
        return deepcopy(normalized)

    def update_plan(
        self,
        plan: TaskPlan | Sequence[PlanStep | Mapping[str, object]] | None,
    ) -> TaskPlan | None:
        """Alias matching the model capability's operation name."""

        return self.replace_plan(plan)

    def record_evidence(self, evidence: CommandEvidence) -> None:
        if not isinstance(evidence, CommandEvidence):
            raise ValueError("task evidence must be CommandEvidence")
        values = (*self.evidence, evidence)
        # Keep the newest objective facts when a very long Turn produces many
        # command calls.  The bound is intentionally independent of context
        # token policy and therefore deterministic.
        self.evidence = values[-MAX_TASK_EVIDENCE:]

    def sections(self) -> list[ContextSection]:
        lines: list[str] = []
        if self.plan is None or not self.plan.steps:
            lines.append("plan: none")
        else:
            lines.append("plan:")
            lines.extend(
                f"- [{step.status.value}] {step.step}" for step in self.plan.steps
            )
        if self.evidence:
            lines.append("evidence:")
            lines.extend(
                "- "
                + "; ".join(
                    (
                        f"tool={item.tool}",
                        f"command={item.command}",
                        f"status={item.status}",
                        f"exit_code={item.exit_code}",
                        f"result_id={item.result_id or 'none'}",
                        f"timestamp={item.timestamp or 'unknown'}",
                    )
                )
                for item in self.evidence
            )
        else:
            lines.append("evidence: none")
        return [ContextSection("task_state", "\n".join(lines), stable=False)]


@dataclass(frozen=True, slots=True)
class ContextSources:
    """The stable and request-scoped sources for one model request."""

    base_system_instructions: BaseSystemInstructions
    project_instructions: ProjectInstructions
    runtime_context: RuntimeContext | None = None
    compaction_summary: CompactionSummary | None = None
    task_state: TaskState | None = None

    def sections(self) -> list[ContextSection]:
        sections = self.base_system_instructions.sections()
        sections.extend(self.project_instructions.sections())
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


class ContextRenderer:
    """Render a ContextPlan into provider-independent model messages."""

    def render(self, plan: ContextPlan) -> list[Message]:
        stable = [section for section in plan.source_sections if section.stable]
        dynamic = [section for section in plan.source_sections if not section.stable]
        stable_text = "\n\n".join(
            section.content for section in stable if section.content
        )
        system_content = [TextBlock(text=stable_text)] if stable_text else []
        dynamic_text = "\n\n".join(
            f"{section.name}:\n{section.content}"
            for section in dynamic
            if section.content
        )
        if dynamic_text:
            # Provider adapters are allowed to concatenate text blocks when
            # encoding a system message.  Keep the semantic section boundary
            # in the renderer so that serialization cannot run stable and
            # dynamic instructions together.
            if stable_text:
                dynamic_text = "\n\n" + dynamic_text
            system_content.append(TextBlock(text=dynamic_text))
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
    ) -> ContextPlan:
        if runtime_context is not None and not isinstance(runtime_context, RuntimeContext):
            raise ValueError("runtime_context must be RuntimeContext")
        if task_state is not None and not isinstance(task_state, TaskState):
            raise ValueError("task_state must be TaskState")
        current = _current_input(current_input)
        workspace = Path(runtime_context.workspace) if runtime_context is not None else Path(".")
        project = self.freeze_project_instructions(workspace)
        sources = ContextSources(
            base_system_instructions=self.base_system_instructions,
            project_instructions=project,
            runtime_context=runtime_context,
            compaction_summary=compaction_summary,
            task_state=task_state,
        )
        source_sections = sources.sections()
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
        )
        estimated = self.budget.estimate_tokens(self.renderer.render(provisional), list(tools))
        assessment = self.budget.assess(estimated)
        plan_metadata.update(
            {
                "estimated_input_tokens": assessment.estimated_input_tokens,
                "reserved_output_tokens": assessment.reserved_output_tokens,
                "safety_margin_tokens": assessment.safety_margin_tokens,
                "usable_input_tokens": assessment.usable_input_tokens,
                "soft_limit_tokens": assessment.soft_limit_tokens,
                "pressure": assessment.pressure,
                "final_fit": "fits" if assessment.fits else "overflow",
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
        )

    @staticmethod
    def _with_metadata(plan: ContextPlan, metadata: dict[str, object]) -> ContextPlan:
        merged = deepcopy(plan.decision_metadata)
        merged.update(deepcopy(metadata))
        merged["estimated_input_tokens"] = plan.estimated_tokens
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
    "AsyncHistoryCompactor",
    "AtomicHistoryParser",
    "AtomicInteractionUnit",
    "AtomicHistoryUnit",
    "CommandEvidence",
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
