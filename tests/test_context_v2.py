from __future__ import annotations

import asyncio

import pytest

from agent.core.messages import Message, TextBlock, ToolCallBlock, ToolResultBlock
from agent.runtime.context import ContextManager
from agent.runtime.context_types import ContextSection
from agent.runtime.evidence import (
    attach_salient_evidence,
    evidence_from_tool_execution,
    extract_salient_diagnostics,
)
from agent.runtime.history import RecentRawTailSelector, RollingSemanticCompactor, ToolResultPruner
from agent.runtime.history.compaction import CompactionSummary
from agent.runtime.runtime_environment import RuntimeContext
from agent.runtime.task_state import Evidence, EvidenceKind, PlanStepStatus, TaskPlan, TaskState
from agent.runtime.thread_runtime import ThreadRuntime
from agent.tools.types import ToolResult
from agent.tools.result_bounds import reduce_tool_result_block


def _message(role: str, text: str) -> Message:
    return Message(role=role, content=[TextBlock(text=text)])


def test_context_v2_modules_are_canonical_and_telemetry_is_not_rendered() -> None:
    environment = RuntimeContext(
        workspace="/workspace",
        cwd="/workspace/src",
        shell="/bin/bash",
        capabilities=("read_file",),
        turn_id="telemetry-only",
    )
    assert environment.turn_id == "telemetry-only"
    rendered_environment = "\n".join(
        section.content for section in environment.sections()
    )
    assert "telemetry-only" not in rendered_environment

    state = TaskState(
        plan=TaskPlan([{"step": "ship the change", "status": "in_progress"}])
    )
    manager = ContextManager()
    plan, _ = asyncio.run(
        manager.assemble_with_reduction(
            [_message("user", "original constraint")],
            current_input="current request",
            runtime_context=environment,
            task_state=state,
        )
    )
    rendered = manager.render(plan)
    assert rendered[-1].role == "system"
    late_text = "".join(
        block.text for block in rendered[-1].content if isinstance(block, TextBlock)
    )
    assert "ship the change" in late_text
    assert "telemetry-only" not in "".join(
        block.text for message in rendered for block in message.content if isinstance(block, TextBlock)
    )
    assert "task_state" not in [section.name for section in plan.source_sections]
    assert plan.late_sections[-1].name == "task_state"
    assert ContextSection.__module__ == "agent.runtime.context_types"


def test_task_plan_accepts_blocked_and_update_is_plan_only() -> None:
    state = TaskState()
    state.update_plan(
        TaskPlan(
            [
                {"step": "blocked dependency", "status": "blocked"},
                {"step": "follow up", "status": "pending"},
            ]
        )
    )
    assert state.plan is not None
    assert state.plan.steps[0].status is PlanStepStatus.BLOCKED
    assert state.evidence == ()
    try:
        TaskPlan.from_payload({"steps": [], "evidence": []})
    except ValueError as error:
        assert "unknown fields" in str(error)
    else:
        raise AssertionError("update_plan accepted an evidence field")


def test_task_state_view_prioritizes_unresolved_failure_and_supersedes_it() -> None:
    state = TaskState(
        plan=TaskPlan([{"step": "active work", "status": "in_progress"}])
    )
    call = "validation-call"
    failure = Evidence(
        kind=EvidenceKind.FAILURE,
        status="failed",
        summary="expected 2 but actual 1",
        source_tool_call_id=call,
        tool="run_command",
        command="pytest tests/test_app.py",
        exit_code=1,
        validation_key="pytest tests/test_app.py",
    )
    state.record_evidence(failure)
    view = state.view()
    assert "expected 2 but actual 1" in view.text
    success = Evidence(
        kind=EvidenceKind.VALIDATION,
        status="success",
        summary="passed",
        source_tool_call_id="validation-success",
        tool="run_command",
        command="pytest tests/test_app.py",
        exit_code=0,
        validation_key="pytest tests/test_app.py",
    )
    state.record_evidence(success)
    resolved = state.view()
    assert "expected 2 but actual 1" not in resolved.text
    assert "passed" in resolved.text

    reopened = Evidence(
        kind=EvidenceKind.FAILURE,
        status="failed",
        summary="expected 2 but actual 0",
        source_tool_call_id="validation-failure-again",
        tool="run_command",
        command="pytest tests/test_app.py",
        exit_code=1,
        validation_key="pytest tests/test_app.py",
    )
    state.record_evidence(reopened)
    reopened_view = state.view()
    assert "expected 2 but actual 0" in reopened_view.text


def test_task_state_view_keeps_only_recent_completed_milestones() -> None:
    state = TaskState(
        plan=TaskPlan(
            [
                {"step": f"completed-{index}", "status": "completed"}
                for index in range(5)
            ]
            + [{"step": "next", "status": "in_progress"}]
        )
    )

    view = state.view()

    assert "completed-0" not in view.text
    assert "completed-1" not in view.text
    for index in (2, 3, 4):
        assert f"completed-{index}" in view.text
    assert "next" in view.text


def test_cache_epoch_prefix_survives_plan_and_history_updates() -> None:
    manager = ContextManager()
    environment = RuntimeContext(
        workspace="/workspace",
        cwd="/workspace",
        shell="/bin/bash",
        turn_id="turn-a",
    )
    first_state = TaskState(
        plan=TaskPlan([{"step": "inspect", "status": "in_progress"}])
    )
    first_history = [_message("user", "original"), _message("assistant", "working")]
    first_plan, _ = asyncio.run(
        manager.assemble_with_reduction(
            first_history,
            runtime_context=environment,
            task_state=first_state,
        )
    )

    second_state = TaskState(
        plan=TaskPlan([{"step": "inspect", "status": "blocked"}])
    )
    second_history = [
        *first_history,
        _message("user", "follow up"),
        _message("assistant", "blocked by dependency"),
    ]
    second_plan, _ = asyncio.run(
        manager.assemble_with_reduction(
            second_history,
            runtime_context=RuntimeContext(
                workspace="/workspace",
                cwd="/workspace",
                shell="/bin/bash",
                turn_id="turn-b",
            ),
            task_state=second_state,
        )
    )

    first_visible = manager.render(first_plan)
    second_visible = manager.render(second_plan)
    # Removing only the late working tail leaves the cacheable system epoch
    # and the previously existing chronological history as an exact prefix.
    assert first_visible[:-1] == second_visible[: len(first_visible) - 1]
    assert first_visible[-1] != second_visible[-1]
    assert "inspect" in "".join(
        block.text
        for block in second_visible[-1].content
        if isinstance(block, TextBlock)
    )


def test_salient_extraction_keeps_middle_diagnostic_and_reads_are_excluded() -> None:
    middle = "\n".join(
        ["prefix"] * 500
        + ["AssertionError: expected 4, actual 3"]
        + ["suffix"] * 500
    )
    call = ToolCallBlock(
        id="validation-call",
        name="run_command",
        arguments={"command": "pytest -q"},
    )
    result = ToolResult(
        content=middle,
        metadata={"command": "pytest -q", "exit_code": 1},
        error_code="COMMAND_FAILED",
    )
    extraction = extract_salient_diagnostics(result, call=call)
    assert any("AssertionError" in line for line in extraction.lines)
    evidence = evidence_from_tool_execution(call, result, timestamp="now")
    assert {item.kind for item in evidence} == {
        EvidenceKind.VALIDATION,
        EvidenceKind.FAILURE,
    }
    read_call = ToolCallBlock(
        id="read-call",
        name="read_file",
        arguments={"path": "app.py"},
    )
    assert evidence_from_tool_execution(
        read_call,
        ToolResult(content="read", metadata={"path": "app.py"}),
        timestamp="now",
    ) == []


def test_failed_file_write_is_failure_evidence_without_claiming_mutation() -> None:
    call = ToolCallBlock(
        id="write-call",
        name="write_file",
        arguments={"path": "missing/file.py", "content": "x"},
    )
    evidence = evidence_from_tool_execution(
        call,
        ToolResult(
            content="parent is not a directory",
            metadata={"path": "missing/file.py"},
            error_code="IO_ERROR",
        ),
        timestamp="now",
    )
    assert [item.kind for item in evidence] == [EvidenceKind.FAILURE]


def test_salient_handoff_keeps_successful_mutation_and_artifact_provenance() -> None:
    mutation_call = ToolCallBlock(
        id="mutation-call",
        name="write_file",
        arguments={"path": "src/app.py", "content": "updated"},
    )
    mutation = ToolResult(
        content="updated file",
        metadata={"changed_paths": ["src/app.py"], "status": "success"},
    )
    attach_salient_evidence(mutation_call, mutation)
    assert mutation.metadata["salient_evidence"]["paths"] == ["src/app.py"]

    artifact_call = ToolCallBlock(
        id="artifact-call",
        name="make_report",
        arguments={},
    )
    artifact = ToolResult(
        content="report ready",
        metadata={"artifact_path": "output/report.json", "status": "success"},
    )
    attach_salient_evidence(artifact_call, artifact)
    assert artifact.metadata["salient_evidence"]["paths"] == ["output/report.json"]

    opaque = ToolResult(content="bulk payload", metadata={"status": "success"})
    attach_salient_evidence(
        ToolCallBlock(id="opaque-call", name="large_result", arguments={}),
        opaque,
    )
    assert "salient_evidence" not in opaque.metadata


def test_task_state_view_uses_one_message_estimator_and_reports_that_estimate() -> None:
    class MessageEstimator:
        def __init__(self) -> None:
            self.calls: list[tuple[list[Message], tuple[object, ...]]] = []

        def estimate(self, messages, tools=()):
            assert all(isinstance(message, Message) for message in messages)
            self.calls.append((list(messages), tuple(tools)))
            return sum(
                len(block.text)
                for message in messages
                for block in message.content
                if isinstance(block, TextBlock)
            )

    estimator = MessageEstimator()
    state = TaskState(
        plan=TaskPlan([{"step": "保留中文约束", "status": "in_progress"}])
    )

    view = state.view(budget_tokens=2_000, estimator=estimator)

    assert estimator.calls
    final_message = estimator.calls[-1][0][0]
    assert final_message.role == "system"
    assert view.estimated_tokens == estimator.estimate([final_message], ())
    assert "保留中文约束" in view.text


def test_task_state_view_does_not_silently_fallback_for_invalid_estimator() -> None:
    state = TaskState(plan=TaskPlan([{"step": "check", "status": "pending"}]))

    class InvalidEstimator:
        def estimate(self, messages, tools=()):
            del messages, tools
            return "not-an-estimate"

    with pytest.raises(ValueError, match="non-negative integer"):
        state.view(estimator=InvalidEstimator())

    with pytest.raises(ValueError, match="provide estimate"):
        state.view(estimator=object())


def test_salient_diagnostic_survives_bound_prune_compaction_and_a_fresh_turn() -> None:
    def round_messages(tag: str) -> list[Message]:
        call = ToolCallBlock(
            id=f"call-{tag}",
            name="run_command",
            arguments={"command": "pytest -q"},
        )
        raw = ToolResult(
            content=(
                f"ERROR {tag} at the head\n"
                + ("unimportant output\n" * 10_000)
                + f"AssertionError: {tag} expected 2 actual 1 critical middle diagnostic\n"
                + ("unimportant output\n" * 10_000)
                + f"ERROR {tag} tail diagnostic\n"
            ),
            metadata={
                "command": "pytest -q",
                "paths": [f"src/{tag}.py"] + [f"src/{index}.py" for index in range(100)],
            },
            error_code="COMMAND_FAILED",
        )
        attach_salient_evidence(call, raw)
        bounded = reduce_tool_result_block(raw.to_message_block(call.id))
        return [
            Message(role="assistant", content=[call]),
            Message(role="tool", content=[bounded]),
        ]

    history = [
        _message("user", "preserve the important constraint"),
        *round_messages("head"),
        *round_messages("middle"),
        *round_messages("tail"),
        _message("user", "new scope after validation failure"),
    ]
    pruned = ToolResultPruner(threshold_chars=32).prune(history)
    retained_diagnostics = [
        block.metadata["salient_evidence"]
        for message in pruned
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    assert pruned.pruned_count == 3
    assert all(
        any(tag in " ".join(item["lines"]) for tag in ("head", "middle", "tail"))
        for item in retained_diagnostics
    )
    assert all(len(item["paths"]) <= 16 and sum(map(len, item["paths"])) <= 2_048 for item in retained_diagnostics)

    class MetadataCompactor:
        async def compact(self, previous, region):
            del previous
            diagnostics = []
            for message in region:
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        evidence = block.metadata.get("salient_evidence", {})
                        diagnostics.extend(evidence.get("lines", []))
            return CompactionSummary("retained diagnostics: " + " | ".join(diagnostics))

    selection = RecentRawTailSelector(usable_input_tokens=1).select(pruned)
    compacted = asyncio.run(
        RollingSemanticCompactor(MetadataCompactor()).compact(pruned, selection)
    )
    assert compacted is not None
    assert all(tag in compacted.summary.text for tag in ("head", "middle", "tail"))
    assert compacted.checkpoint.valid_for_history(pruned)

    # A new scope starts with empty TaskState.  The bounded durable handoff in
    # the checkpoint remains visible through Context's model-visible summary.
    fresh_state = TaskState()
    manager = ContextManager()
    plan, _ = asyncio.run(
        manager.assemble_with_reduction(
            pruned,
            runtime_context=RuntimeContext(workspace="/workspace"),
            task_state=fresh_state,
            checkpoint=compacted.checkpoint,
        )
    )
    assert fresh_state.view().text == "task_state:\n- plan: none"
    rendered_text = "\n".join(
        block.text
        for message in manager.render(plan)
        for block in message.content
        if isinstance(block, TextBlock)
    )
    assert all(tag in rendered_text for tag in ("head", "middle", "tail"))
    assert rendered_text.count("unimportant output") < 10


def test_prefix_stays_canonical_when_plan_and_tool_history_change() -> None:
    manager = ContextManager()
    canonical_user = _message("user", "same canonical request")
    first_plan, _ = asyncio.run(
        manager.assemble_with_reduction(
            [canonical_user],
            current_input="same canonical request",
            runtime_context=RuntimeContext(workspace="/workspace"),
            task_state=TaskState(
                plan=TaskPlan([{"step": "inspect", "status": "in_progress"}])
            ),
        )
    )
    first_request = manager.render(first_plan)

    call = ToolCallBlock(
        id="prefix-call",
        name="run_command",
        arguments={"command": "pytest -q"},
    )
    result = Message(
        role="tool",
        content=[
            ToolResultBlock(
                tool_call_id=call.id,
                content="failed",
                metadata={"command": "pytest -q", "exit_code": 1},
                error_code="COMMAND_FAILED",
            )
        ],
    )
    history_after_tool = [
        canonical_user,
        Message(role="assistant", content=[call]),
        result,
    ]
    second_plan, _ = asyncio.run(
        manager.assemble_with_reduction(
            history_after_tool,
            runtime_context=RuntimeContext(workspace="/workspace"),
            task_state=TaskState(
                plan=TaskPlan([{"step": "repair", "status": "blocked"}])
            ),
        )
    )
    second_request = manager.render(second_plan)

    # Only the late TaskState partition changes between requests.  The
    # baseline system message and already-existing chronological history stay
    # a byte-for-byte provider request prefix through tool execution.
    assert first_request[:-1] == second_request[: len(first_request) - 1]
    assert sum(
        1
        for message in second_request
        if message.role == "user"
        and any(
            isinstance(block, TextBlock)
            and block.text == "same canonical request"
            for block in message.content
        )
    ) == 1
    assert second_plan.late_sections[-1].name == "task_state"


def test_context_diagnostics_are_bounded_and_available_outside_rendered_plan() -> None:
    raw = {
        "epoch_sections": [f"section-{index}" for index in range(100)],
        "context_epoch_sections": [f"epoch-{index}" for index in range(100)],
        "late_working_tail_sections": ["loaded_skills", "task_state"],
        "loaded_skill_names": [f"skill-{index}" for index in range(100)],
        "canonical_history_messages": 14,
        "history_estimate_tokens": 123,
        "task_state_view_estimate_tokens": 45,
        "tool_results_pruned": 3,
        "checkpoint_validation": "reused",
        "final_request_estimate_tokens": 456,
        "final_fit": "fits",
    }

    diagnostics = ThreadRuntime._context_diagnostics(raw)

    assert diagnostics["epoch_sections"] == [f"section-{index}" for index in range(32)]
    assert diagnostics["context_epoch_sections"] == [f"epoch-{index}" for index in range(32)]
    assert len(diagnostics["loaded_skill_names"]) == 32
    assert diagnostics["late_working_tail_sections"] == ["loaded_skills", "task_state"]
    assert diagnostics["final_request_estimate_tokens"] == 456
