from __future__ import annotations

import asyncio

from agent.core.messages import Message, TextBlock, ToolCallBlock
from agent.runtime.context import ContextManager
from agent.runtime.context_types import ContextSection
from agent.runtime.evidence import evidence_from_tool_execution, extract_salient_diagnostics
from agent.runtime.runtime_environment import RuntimeContext
from agent.runtime.task_state import Evidence, EvidenceKind, PlanStepStatus, TaskPlan, TaskState
from agent.tools.types import ToolResult


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
