"""Turn-local task bookkeeping and deterministic model projection.

The canonical conversation remains the source of user intent and historical
progress.  This module only owns the small, ephemeral state that is useful
while a Turn is executing: a model-proposed plan and objective facts recorded
by the Harness after real tool execution.  In particular, callers cannot put
arbitrary claims into ``evidence`` through the ``update_plan`` capability.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
import re

from .context_types import ContextSection


MAX_TASK_PLAN_STEPS = 20
MAX_TASK_EVIDENCE = 100
MAX_PLAN_STEP_TEXT_CHARS = 2_000
MAX_TASK_PLAN_STEP_TEXT_CHARS = MAX_PLAN_STEP_TEXT_CHARS
MAX_EVIDENCE_TOOL_CHARS = 128
MAX_EVIDENCE_COMMAND_CHARS = 8_192
MAX_EVIDENCE_STATUS_CHARS = 128
MAX_EVIDENCE_RESULT_ID_CHARS = 256
MAX_EVIDENCE_TIMESTAMP_CHARS = 128
MAX_EVIDENCE_SUMMARY_CHARS = 1_024
MAX_EVIDENCE_KIND_CHARS = 32
MAX_EVIDENCE_CALL_ID_CHARS = 256
MAX_EVIDENCE_STEP_CHARS = MAX_PLAN_STEP_TEXT_CHARS
MAX_TASK_STATE_VIEW_TOKENS = 2_048
MAX_TASK_STATE_VIEW_CHARS = MAX_TASK_STATE_VIEW_TOKENS * 4
# A plan can legally contain twenty steps, but a completed history of every
# one is not useful in the working tail.  Keep a small deterministic milestone
# window and let the canonical transcript/compaction retain the full past.
MAX_RECENT_COMPLETED_MILESTONES = 3
MAX_COMMAND_EVIDENCE_STRING_CHARS = MAX_EVIDENCE_COMMAND_CHARS


class PlanStepStatus(str, Enum):
    """Statuses accepted by the model-maintained plan."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True, init=False)
class PlanStep:
    """One bounded plan step.

    ``description`` is retained as a compatibility alias for older callers;
    the wire shape is intentionally just ``step`` and ``status``.
    """

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
        if description is not None and not isinstance(description, str):
            raise ValueError("plan step description must be text")
        if not isinstance(step, str) or not step.strip():
            raise ValueError("plan step text must be non-empty")
        if len(step) > MAX_PLAN_STEP_TEXT_CHARS:
            raise ValueError(
                f"plan step text must be at most {MAX_PLAN_STEP_TEXT_CHARS} characters"
            )
        try:
            normalized = status if isinstance(status, PlanStepStatus) else PlanStepStatus(status)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "plan step status must be pending, in_progress, completed, or blocked"
            ) from error
        object.__setattr__(self, "step", step)
        object.__setattr__(self, "status", normalized)

    @property
    def description(self) -> str:
        return self.step


@dataclass(frozen=True, slots=True, init=False)
class TaskPlan:
    """An atomically replaceable plan with bounded invariants."""

    steps: tuple[PlanStep, ...]

    def __init__(self, steps: Sequence[PlanStep | Mapping[str, object]] = ()) -> None:
        if isinstance(steps, (str, bytes)):
            raise ValueError("plan steps must be a sequence")
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
                unknown = set(value) - {"step", "status"}
                if unknown:
                    names = ", ".join(sorted(map(str, unknown)))
                    raise ValueError(f"plan step {index} has unknown fields: {names}")
                if "step" not in value or "status" not in value:
                    raise ValueError(f"plan step {index} requires step and status")
                step = PlanStep(value["step"], value["status"])  # type: ignore[arg-type]
            else:
                raise ValueError(f"plan step {index} must be a PlanStep or object")
            normalized.append(step)
        if sum(item.status is PlanStepStatus.IN_PROGRESS for item in normalized) > 1:
            raise ValueError("task plan may contain at most one in_progress step")
        object.__setattr__(self, "steps", tuple(normalized))

    @classmethod
    def from_payload(cls, value: object) -> TaskPlan:
        """Parse the exact ``update_plan`` payload.

        The outer object may only contain ``steps`` and every step object may
        only contain ``step`` and ``status``.  Rejecting extra fields here is
        important because ToolRegistry validation is intentionally a small
        JSON-Schema subset and this is the Harness trust boundary.
        """

        if isinstance(value, TaskPlan):
            return cls(value.steps)
        if isinstance(value, Mapping):
            unknown = set(value) - {"steps"}
            if unknown:
                names = ", ".join(sorted(map(str, unknown)))
                raise ValueError(f"update_plan has unknown fields: {names}")
            if "steps" not in value:
                raise ValueError("update_plan requires steps")
            value = value["steps"]
        if value is None:
            return cls()
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise ValueError("update_plan steps must be an array")
        return cls(value)

    def to_dict(self) -> dict[str, object]:
        return {
            "steps": [
                {"step": item.step, "status": item.status.value}
                for item in self.steps
            ]
        }


class EvidenceKind(str, Enum):
    """Objective facts worth carrying into the working tail."""

    MUTATION = "mutation"
    VALIDATION = "validation"
    FAILURE = "failure"
    ARTIFACT = "artifact"


class EvidenceStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    RUNNING = "running"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


def _bounded_text(value: object, *, name: str, maximum: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"evidence {name} must be text")
    if required and not value.strip():
        raise ValueError(f"evidence {name} must be non-empty text")
    if len(value) > maximum:
        raise ValueError(f"evidence {name} must be at most {maximum} characters")
    return value


@dataclass(frozen=True, slots=True, init=False)
class Evidence:
    """Harness-owned, provenance-carrying objective fact.

    The custom initializer accepts both the V2 names (``summary``,
    ``source_tool_call_id``) and the V1 command-evidence fields.  This keeps
    old embedders source-compatible while allowing mutation, validation,
    failure and artifact facts to share one projection pipeline.
    """

    kind: EvidenceKind
    status: str
    summary: str
    source_tool_call_id: str
    related_step: str | None
    tool: str
    command: str
    exit_code: int | None
    result_id: str
    timestamp: str
    paths: tuple[str, ...]
    validation_key: str | None
    metadata: dict[str, object]

    def __init__(
        self,
        kind: EvidenceKind | str = EvidenceKind.VALIDATION,
        status: str = "completed",
        summary: str = "",
        source_tool_call_id: str = "",
        related_step: str | None = None,
        result_id: str = "",
        timestamp: str = "",
        *,
        tool: str = "",
        command: str = "",
        exit_code: int | None = None,
        paths: Sequence[str] = (),
        validation_key: str | None = None,
        metadata: Mapping[str, object] | None = None,
        # Compatibility aliases used by likely downstream integrations.
        source_call_id: str | None = None,
        tool_call_id: str | None = None,
        evidence_kind: EvidenceKind | str | None = None,
        text: str | None = None,
    ) -> None:
        # V1 exposed ``CommandEvidence(tool, command, status, exit_code,
        # result_id, timestamp)``.  Accept that positional shape as well as
        # the V2 kind/summary/provenance shape.
        if (
            isinstance(kind, str)
            and kind in {"run_command", "exec_command", "write_stdin"}
            and not tool
            and not command
        ):
            legacy_tool = kind
            legacy_command = status
            legacy_status = summary
            legacy_exit = source_tool_call_id
            legacy_result = related_step if isinstance(related_step, str) else result_id
            kind = EvidenceKind.VALIDATION
            tool = legacy_tool
            command = legacy_command
            status = legacy_status
            exit_code = legacy_exit if isinstance(legacy_exit, int) and not isinstance(legacy_exit, bool) else exit_code
            result_id = legacy_result if isinstance(legacy_result, str) else result_id
            # In the six-positional V1 form the sixth argument has already
            # landed in ``result_id`` and the seventh in ``timestamp``.
            summary = f"{legacy_status}: {legacy_command}"
            source_tool_call_id = result_id or "legacy-command"
            related_step = None
        elif tool and command and not summary and not source_tool_call_id:
            # Keyword form of the same V1 constructor.
            summary = f"{status}: {command}"
            source_tool_call_id = result_id or "legacy-command"
        if evidence_kind is not None:
            kind = evidence_kind
        if text is not None and not summary:
            summary = text
        if source_call_id is not None:
            source_tool_call_id = source_call_id
        if tool_call_id is not None:
            source_tool_call_id = tool_call_id
        try:
            normalized_kind = kind if isinstance(kind, EvidenceKind) else EvidenceKind(kind)
        except (TypeError, ValueError) as error:
            raise ValueError("evidence kind must be mutation, validation, failure, or artifact") from error
        if isinstance(status, EvidenceStatus):
            status = status.value
        status = _bounded_text(status, name="status", maximum=MAX_EVIDENCE_STATUS_CHARS, required=True)
        summary = _bounded_text(summary, name="summary", maximum=MAX_EVIDENCE_SUMMARY_CHARS, required=True)
        source_tool_call_id = _bounded_text(
            source_tool_call_id,
            name="source_tool_call_id",
            maximum=MAX_EVIDENCE_CALL_ID_CHARS,
            required=True,
        )
        tool = _bounded_text(tool, name="tool", maximum=MAX_EVIDENCE_TOOL_CHARS)
        command = _bounded_text(command, name="command", maximum=MAX_EVIDENCE_COMMAND_CHARS)
        result_id = _bounded_text(result_id, name="result_id", maximum=MAX_EVIDENCE_RESULT_ID_CHARS)
        timestamp = _bounded_text(timestamp, name="timestamp", maximum=MAX_EVIDENCE_TIMESTAMP_CHARS)
        if related_step is not None:
            related_step = _bounded_text(
                related_step,
                name="related_step",
                maximum=MAX_EVIDENCE_STEP_CHARS,
                required=True,
            )
        if validation_key is not None:
            validation_key = _bounded_text(
                validation_key,
                name="validation_key",
                maximum=MAX_EVIDENCE_COMMAND_CHARS,
            )
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            raise ValueError("evidence exit_code must be an integer or None")
        normalized_paths: list[str] = []
        for path in paths:
            normalized_paths.append(
                _bounded_text(path, name="path", maximum=MAX_EVIDENCE_COMMAND_CHARS, required=True)
            )
        if metadata is not None and not isinstance(metadata, Mapping):
            raise ValueError("evidence metadata must be an object")
        object.__setattr__(self, "kind", normalized_kind)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "summary", summary)
        object.__setattr__(self, "source_tool_call_id", source_tool_call_id)
        object.__setattr__(self, "related_step", related_step)
        object.__setattr__(self, "tool", tool)
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "exit_code", exit_code)
        object.__setattr__(self, "result_id", result_id)
        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "paths", tuple(normalized_paths))
        object.__setattr__(self, "validation_key", validation_key)
        object.__setattr__(self, "metadata", dict(metadata or {}))

    # V1 compatibility accessors.
    @property
    def tool_name(self) -> str:
        return self.tool

    @property
    def source_call_id(self) -> str:
        return self.source_tool_call_id

    @property
    def tool_call_id(self) -> str:
        return self.source_tool_call_id

    @property
    def evidence_kind(self) -> EvidenceKind:
        return self.kind

    @property
    def text(self) -> str:
        return self.summary

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "status": self.status,
            "summary": self.summary,
            "source_tool_call_id": self.source_tool_call_id,
            "related_step": self.related_step,
            "tool": self.tool,
            "command": self.command,
            "exit_code": self.exit_code,
            "result_id": self.result_id,
            "timestamp": self.timestamp,
            "paths": list(self.paths),
            "validation_key": self.validation_key,
            "metadata": deepcopy(self.metadata),
        }


# Existing public names intentionally continue to mean an Evidence value.
CommandEvidence = Evidence
HarnessEvidence = Evidence
TaskEvidence = Evidence


@dataclass(frozen=True, slots=True)
class TaskStateView:
    """Bounded deterministic projection rendered after chronological history."""

    text: str
    estimated_tokens: int
    budget_tokens: int = MAX_TASK_STATE_VIEW_TOKENS
    item_count: int = 0
    omitted_items: int = 0

    @property
    def content(self) -> str:
        return self.text

    @property
    def estimate(self) -> int:
        return self.estimated_tokens

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "estimated_tokens": self.estimated_tokens,
            "budget_tokens": self.budget_tokens,
            "item_count": self.item_count,
            "omitted_items": self.omitted_items,
        }


def _estimate_tokens(text: str) -> int:
    # Keep this local and dependency-free; ContextManager replaces the value
    # in diagnostics with its provider-independent estimator when available.
    ascii_count = sum(ord(char) < 128 for char in text)
    non_ascii = len(text) - ascii_count
    return max(0, (ascii_count + 3) // 4 + (non_ascii + 1) // 2)


def _validation_key(evidence: Evidence) -> str | None:
    if evidence.kind not in {EvidenceKind.VALIDATION, EvidenceKind.FAILURE}:
        return None
    if evidence.validation_key:
        return evidence.validation_key.casefold().strip()
    if evidence.command:
        return re.sub(r"\s+", " ", evidence.command.casefold().strip())
    return None


def _is_success(evidence: Evidence) -> bool:
    return evidence.status.casefold() in {"ok", "success", "completed", "passed", "pass", "0"}


def _is_failure(evidence: Evidence) -> bool:
    return evidence.kind is EvidenceKind.FAILURE or evidence.status.casefold() in {
        "failed", "failure", "error", "timed_out", "timeout", "nonzero", "unknown",
    }


@dataclass(slots=True, init=False)
class TaskState:
    """Ephemeral Turn state with Harness-only evidence recording."""

    plan: TaskPlan | None
    evidence: tuple[Evidence, ...]

    def __init__(
        self,
        plan: TaskPlan | Sequence[PlanStep | Mapping[str, object]] | None = None,
        evidence: Sequence[Evidence] = (),
        *,
        task_plan: TaskPlan | Sequence[PlanStep | Mapping[str, object]] | None = None,
        command_evidence: Sequence[Evidence] | None = None,
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
        if any(not isinstance(item, Evidence) for item in evidence_values):
            raise ValueError("task evidence must contain Evidence values")
        self.plan = normalized_plan
        self.evidence = evidence_values

    @property
    def task_plan(self) -> TaskPlan | None:
        return self.plan

    @property
    def command_evidence(self) -> tuple[Evidence, ...]:
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

    update_plan = replace_plan

    def record_evidence(self, evidence: Evidence) -> None:
        if not isinstance(evidence, Evidence):
            raise ValueError("task evidence must be Evidence")
        self.evidence = (*self.evidence, evidence)[-MAX_TASK_EVIDENCE:]

    def _resolved_validation_keys(self) -> set[str]:
        latest: dict[str, Evidence] = {}
        for item in self.evidence:
            key = _validation_key(item)
            if key is not None:
                # The newest fact for a validation key is authoritative. A
                # later failure must reopen a key that an older success had
                # resolved; success only supersedes an earlier failure.
                latest[key] = item
        return {key for key, item in latest.items() if _is_success(item)}

    def projected_items(self) -> list[tuple[int, str, object]]:
        """Return deterministic priority-tagged items for projection tests."""

        resolved = self._resolved_validation_keys()
        items: list[tuple[int, str, object]] = []
        # Unresolved failures are always first, newest first.
        for index in range(len(self.evidence) - 1, -1, -1):
            item = self.evidence[index]
            key = _validation_key(item)
            if _is_failure(item) and (key is None or key not in resolved):
                items.append((0, f"failure:{len(self.evidence) - index:04d}", item))
        # Keep the latest validation for each deterministic key.
        latest: dict[str, tuple[int, Evidence]] = {}
        for index, item in enumerate(self.evidence):
            if item.kind is not EvidenceKind.VALIDATION:
                continue
            key = _validation_key(item) or f"__validation_{index}"
            latest[key] = (index, item)
        items.extend(
            (1, f"validation:{len(self.evidence) - index:04d}", item)
            for index, item in sorted(latest.values(), key=lambda pair: pair[0], reverse=True)
        )
        if self.plan is not None:
            completed_indexes = [
                index
                for index, step in enumerate(self.plan.steps)
                if step.status is PlanStepStatus.COMPLETED
            ]
            recent_completed = set(
                completed_indexes[-MAX_RECENT_COMPLETED_MILESTONES:]
            )
            for index, step in enumerate(self.plan.steps):
                if (
                    step.status is PlanStepStatus.COMPLETED
                    and index not in recent_completed
                ):
                    continue
                priority = 2 if step.status is PlanStepStatus.IN_PROGRESS else 3 if step.status in {
                    PlanStepStatus.PENDING, PlanStepStatus.BLOCKED
                } else 4
                items.append((priority, f"plan:{index:04d}", step))
        for index in range(len(self.evidence) - 1, -1, -1):
            item = self.evidence[index]
            if item.kind is EvidenceKind.MUTATION:
                items.append((5, f"mutation:{len(self.evidence) - index:04d}", item))
            elif item.kind is EvidenceKind.ARTIFACT:
                items.append((6, f"artifact:{len(self.evidence) - index:04d}", item))
            elif item.kind is not EvidenceKind.VALIDATION and not _is_failure(item):
                items.append((7, f"other:{len(self.evidence) - index:04d}", item))
            elif item.kind is EvidenceKind.VALIDATION and _is_success(item):
                items.append((8, f"success:{len(self.evidence) - index:04d}", item))
        return sorted(items, key=lambda value: (value[0], value[1]))

    @staticmethod
    def _item_line(value: object) -> str:
        if isinstance(value, PlanStep):
            return f"plan [{value.status.value}] {value.step}"
        assert isinstance(value, Evidence)
        fields = [f"kind={value.kind.value}", f"status={value.status}"]
        if value.tool:
            fields.append(f"tool={value.tool}")
        if value.command:
            fields.append(f"command={value.command}")
        if value.exit_code is not None:
            fields.append(f"exit_code={value.exit_code}")
        if value.paths:
            fields.append(f"paths={','.join(value.paths)}")
        fields.append(f"summary={value.summary}")
        fields.append(f"source={value.source_tool_call_id}")
        return "evidence " + "; ".join(fields)

    def view(
        self,
        *,
        budget_tokens: int = MAX_TASK_STATE_VIEW_TOKENS,
        max_chars: int = MAX_TASK_STATE_VIEW_CHARS,
        estimator: object | None = None,
    ) -> TaskStateView:
        if isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int) or budget_tokens < 1:
            raise ValueError("budget_tokens must be a positive integer")
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
            raise ValueError("max_chars must be a positive integer")
        lines = ["task_state:"]
        omitted = 0
        selected = 0
        # A fixed hard character bound is applied in addition to the token
        # budget to make output deterministic across estimators/providers.
        hard_chars = min(max_chars, max(1, budget_tokens * 4))
        for _, _, item in self.projected_items():
            line = "- " + self._item_line(item)
            candidate = "\n".join((*lines, line))
            if len(candidate) > hard_chars:
                omitted += 1
                continue
            if estimator is not None:
                method = getattr(estimator, "estimate", None)
                try:
                    estimate = method([candidate], ()) if callable(method) else _estimate_tokens(candidate)
                except Exception:
                    estimate = _estimate_tokens(candidate)
            else:
                estimate = _estimate_tokens(candidate)
            if estimate > budget_tokens:
                omitted += 1
                continue
            lines.append(line)
            selected += 1
        if selected == 0:
            lines.append("- plan: none" if self.plan is None else "- task state has no fitting items")
        if omitted:
            marker = f"- omitted_items={omitted}"
            if len("\n".join((*lines, marker))) <= hard_chars:
                lines.append(marker)
        text = "\n".join(lines)
        return TaskStateView(
            text=text,
            estimated_tokens=_estimate_tokens(text),
            budget_tokens=budget_tokens,
            item_count=selected,
            omitted_items=omitted,
        )

    project = view
    project_view = view

    def sections(self) -> list[ContextSection]:
        return [ContextSection("task_state", self.view().text, stable=False)]


__all__ = [
    "CommandEvidence",
    "Evidence",
    "EvidenceKind",
    "EvidenceStatus",
    "HarnessEvidence",
    "MAX_COMMAND_EVIDENCE_STRING_CHARS",
    "MAX_EVIDENCE_CALL_ID_CHARS",
    "MAX_EVIDENCE_COMMAND_CHARS",
    "MAX_EVIDENCE_KIND_CHARS",
    "MAX_EVIDENCE_RESULT_ID_CHARS",
    "MAX_EVIDENCE_STATUS_CHARS",
    "MAX_EVIDENCE_STEP_CHARS",
    "MAX_EVIDENCE_SUMMARY_CHARS",
    "MAX_EVIDENCE_TIMESTAMP_CHARS",
    "MAX_EVIDENCE_TOOL_CHARS",
    "MAX_PLAN_STEP_TEXT_CHARS",
    "MAX_RECENT_COMPLETED_MILESTONES",
    "MAX_TASK_EVIDENCE",
    "MAX_TASK_PLAN_STEPS",
    "MAX_TASK_PLAN_STEP_TEXT_CHARS",
    "MAX_TASK_STATE_VIEW_CHARS",
    "MAX_TASK_STATE_VIEW_TOKENS",
    "PlanStep",
    "PlanStepStatus",
    "TaskEvidence",
    "TaskPlan",
    "TaskState",
    "TaskStateView",
]
