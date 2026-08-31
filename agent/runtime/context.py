"""Provider-independent model context assembly.

``Conversation`` owns the durable, complete Thread transcript.  This module
turns a detached transcript plus request-scoped sources into the messages a
model sees.  The default history policies deliberately do nothing so that
future selection and compaction cannot silently change durable history.
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Protocol

from agent.core.messages import Message, TextBlock
from agent.tools.types import ToolDefinition

from .context_budget import ContextBudget
from .prompt import DEFAULT_SYSTEM_PROMPT
from .settings import ApprovalMode


DEFAULT_CONTEXT_WINDOW_TOKENS = 32_000


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

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("project instructions must be text")

    def sections(self) -> list[ContextSection]:
        if not self.text:
            return []
        return [ContextSection("project_instructions", self.text)]


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


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Cheap environment facts collected for each model request."""

    workspace: str | Path
    cwd: str | Path | None = None
    shell: str | None = None
    approval_mode: ApprovalMode = ApprovalMode.ON_REQUEST
    capabilities: tuple[str, ...] = ()

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
                    )
                ),
                stable=False,
            )
        ]


@dataclass(frozen=True, slots=True)
class TaskState:
    """Turn goal and progress facts kept separate from runtime facts."""

    goal: str = ""
    constraints: tuple[str, ...] = ()
    progress: tuple[str, ...] = ()
    validation_state: str = "unknown"
    checkpoints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "goal", _text_value(self.goal, "goal"))
        object.__setattr__(
            self, "constraints", _text_tuple(self.constraints, "constraints")
        )
        object.__setattr__(self, "progress", _text_tuple(self.progress, "progress"))
        object.__setattr__(
            self,
            "validation_state",
            _text_value(self.validation_state, "validation_state"),
        )
        object.__setattr__(
            self, "checkpoints", _text_tuple(self.checkpoints, "checkpoints")
        )

    def sections(self) -> list[ContextSection]:
        return [
            ContextSection(
                "task_state",
                "\n".join(
                    (
                        f"goal: {self.goal or 'none'}",
                        "constraints: "
                        + (", ".join(self.constraints) if self.constraints else "none"),
                        "progress: "
                        + (", ".join(self.progress) if self.progress else "none"),
                        f"validation: {self.validation_state}",
                        "checkpoints: "
                        + (", ".join(self.checkpoints) if self.checkpoints else "none"),
                    )
                ),
                stable=False,
            )
        ]


@dataclass(frozen=True, slots=True)
class ContextSources:
    """The stable and request-scoped sources for one model request."""

    base_system_instructions: BaseSystemInstructions
    project_instructions: ProjectInstructions
    runtime_context: RuntimeContext | None = None
    task_state: TaskState | None = None

    def sections(self) -> list[ContextSection]:
        sections = self.base_system_instructions.sections()
        sections.extend(self.project_instructions.sections())
        if self.runtime_context is not None:
            sections.extend(self.runtime_context.sections())
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

    def __post_init__(self) -> None:
        if self.budget_status not in {"fits", "exceeds"}:
            raise ValueError("budget_status must be 'fits' or 'exceeds'")
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
        budget: ContextBudget | None = None,
        history_selector: HistorySelector | None = None,
        history_compactor: HistoryCompactor | None = None,
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
        self.renderer = renderer or ContextRenderer()

    def assemble(
        self,
        canonical_history: Sequence[Message],
        *,
        current_input: str | Message | None = None,
        runtime_context: RuntimeContext | None = None,
        task_state: TaskState | None = None,
        tools: Sequence[ToolDefinition] = (),
    ) -> ContextPlan:
        """Build a detached plan and reject an oversized request explicitly."""

        snapshot = _messages(canonical_history, "canonical history")
        selected = _messages(
            self.history_selector.select(deepcopy(snapshot)),
            "selected history",
        )
        compacted = _messages(
            self.history_compactor.compact(deepcopy(selected)),
            "compacted history",
        )
        if runtime_context is not None and not isinstance(
            runtime_context, RuntimeContext
        ):
            raise ValueError("runtime_context must be RuntimeContext")
        if task_state is not None and not isinstance(task_state, TaskState):
            raise ValueError("task_state must be TaskState")
        current = _current_input(current_input)
        workspace = (
            Path(runtime_context.workspace)
            if runtime_context is not None
            else Path(".")
        )
        project = self.project_instructions_provider.load(workspace)
        if not isinstance(project, ProjectInstructions):
            raise ValueError(
                "project instructions provider must return ProjectInstructions"
            )
        sources = ContextSources(
            base_system_instructions=self.base_system_instructions,
            project_instructions=project,
            runtime_context=runtime_context,
            task_state=task_state,
        )
        tool_snapshot = list(tools)
        metadata = {
            "selector": type(self.history_selector).__name__,
            "compactor": type(self.history_compactor).__name__,
            "canonical_history_messages": len(snapshot),
            "selected_history_messages": len(selected),
            "compacted_history_messages": len(compacted),
        }
        source_sections = sources.sections()
        provisional = ContextPlan(
            source_sections=source_sections,
            selected_history=selected,
            compacted_history=compacted,
            current_input=current,
            estimated_tokens=0,
            input_budget_tokens=self.budget.input_budget_tokens,
            budget_status="fits",
            decision_metadata=metadata,
        )
        rendered = self.renderer.render(provisional)
        estimated = self.budget.estimate_tokens(rendered, tool_snapshot)
        plan = ContextPlan(
            source_sections=source_sections,
            selected_history=selected,
            compacted_history=compacted,
            current_input=current,
            estimated_tokens=estimated,
            input_budget_tokens=self.budget.input_budget_tokens,
            budget_status=(
                "fits" if estimated <= self.budget.input_budget_tokens else "exceeds"
            ),
            decision_metadata=metadata,
        )
        self.budget.ensure_fits(rendered, tool_snapshot)
        return plan

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


def _text_value(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    return value


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
    "ContextManager",
    "ContextPlan",
    "ContextRenderer",
    "ContextSection",
    "ContextSource",
    "ContextSources",
    "HistoryCompactor",
    "HistorySelector",
    "NoOpHistoryCompactor",
    "NoOpHistorySelector",
    "ProjectInstructions",
    "ProjectInstructionsProvider",
    "RuntimeContext",
    "StaticProjectInstructionsProvider",
    "TaskState",
]
