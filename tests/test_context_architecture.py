from __future__ import annotations

import pytest

from agent.core.messages import Message, TextBlock
from agent.runtime import (
    BaseSystemInstructions,
    ContextBudget,
    ContextLimitError,
    ContextManager,
    ContextRenderer,
    NoOpHistoryCompactor,
    NoOpHistorySelector,
    ProjectInstructions,
    StaticProjectInstructionsProvider,
    RuntimeContext,
    TaskPlan,
    TaskState,
)
from agent.runtime.prompt import DEFAULT_SYSTEM_PROMPT


def _history() -> list[Message]:
    return [
        Message(role="system", content=[TextBlock(text="canonical system")]),
        Message(role="user", content=[TextBlock(text="old question")]),
        Message(role="assistant", content=[TextBlock(text="old answer")]),
    ]


def test_noop_context_plan_renders_sources_and_detached_history() -> None:
    history = _history()
    manager = ContextManager(
        base_system_instructions=BaseSystemInstructions("stable rules"),
        project_instructions_provider=StaticProjectInstructionsProvider(
            ProjectInstructions("project rules")
        ),
        budget=ContextBudget(context_window_tokens=4096, output_tokens=128),
    )
    plan = manager.assemble(
        history,
        current_input="new question",
        runtime_context=RuntimeContext(
            workspace="/workspace",
            cwd="/workspace/src",
            shell="/bin/bash",
            capabilities=("read_file",),
        ),
        task_state=TaskState(
            plan=TaskPlan(
                [
                    {"step": "read the source", "status": "completed"},
                    {"step": "finish the task", "status": "in_progress"},
                ]
            ),
        ),
    )

    assert isinstance(manager.history_selector, NoOpHistorySelector)
    assert isinstance(manager.history_compactor, NoOpHistoryCompactor)
    assert plan.selected_history == history
    assert plan.compacted_history == history
    assert plan.current_input == Message(
        role="user", content=[TextBlock(text="new question")]
    )
    assert plan.budget_status == "fits"
    assert plan.estimated_tokens > 0
    assert [section.name for section in plan.source_sections] == [
        "base_system_instructions",
        "project_instructions",
        "runtime_context",
        "task_state",
    ]

    rendered = ContextRenderer().render(plan)
    assert [message.role for message in rendered] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    stable = rendered[0].content[0]
    dynamic = rendered[0].content[1]
    assert isinstance(stable, TextBlock)
    assert isinstance(dynamic, TextBlock)
    assert stable.text == "stable rules\n\nproject rules"
    assert "/workspace/src" in dynamic.text
    assert "finish the task" in dynamic.text

    # Context assembly works from detached snapshots and cannot mutate the
    # canonical history when a caller changes either input or rendered output.
    rendered[-1].content[0].text = "changed model copy"
    history[-1].content[0].text = "changed caller copy"
    assert plan.selected_history[-1].content[0].text == "old answer"


def test_noop_context_limit_is_explicit_and_preserves_canonical_history() -> None:
    history = _history()
    original = [message.content[0].text for message in history]
    manager = ContextManager(
        base_system_instructions=BaseSystemInstructions("x" * 5000),
        budget=ContextBudget(context_window_tokens=256, output_tokens=32),
    )

    with pytest.raises(ContextLimitError) as captured:
        manager.assemble(history, current_input="new question")

    assert captured.value.code == "CONTEXT_LIMIT"
    assert [message.content[0].text for message in history] == original


def test_runtime_context_is_collected_afresh_for_each_assembly() -> None:
    contexts = iter(
        [
            RuntimeContext(workspace="/one", cwd="/one"),
            RuntimeContext(workspace="/two", cwd="/two"),
        ]
    )
    manager = ContextManager()

    first = manager.assemble(_history(), runtime_context=next(contexts))
    second = manager.assemble(_history(), runtime_context=next(contexts))

    first_runtime = next(
        section
        for section in first.source_sections
        if section.name == "runtime_context"
    )
    second_runtime = next(
        section
        for section in second.source_sections
        if section.name == "runtime_context"
    )
    assert "/one" in first_runtime.content
    assert "/two" in second_runtime.content


def test_default_system_prompt_is_complete_lightweight_and_static() -> None:
    normalized = DEFAULT_SYSTEM_PROMPT.casefold()
    for principle in (
        "root cause",
        "focused changes",
        "existing conventions",
        "search and read",
        "before guessing",
        "structured patches",
        "shell validation",
        "process sessions",
        "diagnose failures",
        "unresolved failures truthfully",
        "approval and sandbox boundaries",
        "never bypass",
        "progress updates concise",
    ):
        assert principle in normalized

    # Stable instructions describe agent behavior only.  Tool schemas and
    # per-request environment/task facts belong to ContextManager sources.
    assert "run_command" not in normalized
    assert "parameters" not in normalized
    for dynamic_marker in (
        "runtime_context:",
        "task_state:",
        "workspace:",
        "cwd:",
        "approval_mode:",
        "capabilities:",
    ):
        assert dynamic_marker not in normalized
