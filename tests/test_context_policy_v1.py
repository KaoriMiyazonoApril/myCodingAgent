from __future__ import annotations

from copy import deepcopy
import json

import pytest

from agent.core.messages import Message, TextBlock, ToolCallBlock, ToolResultBlock
from agent.model.openai_compatible import OpenAICompatibleProvider
from agent.model.types import LLMRequest, ProviderConfig
from agent.runtime import (
    BaseSystemInstructions,
    ContextBudgetPolicy,
    ContextManager,
    ContextRenderer,
    PlanStep,
    ProjectInstructions,
    RootProjectInstructionsProvider,
    RuntimeContext,
    TaskPlan,
    TaskState,
    TokenEstimator,
)
from agent.tools.result_bounds import bound_text
from agent.tools.types import ToolDefinition


def _message(role: str, text: str) -> Message:
    return Message(role=role, content=[TextBlock(text=text)])


def test_root_project_instructions_exist_and_missing_is_empty(tmp_path) -> None:
    provider = RootProjectInstructionsProvider()
    assert provider.load(tmp_path) == ProjectInstructions()

    (tmp_path / "AGENTS.md").write_text("root rules", encoding="utf-8")
    loaded = provider.load(tmp_path)

    assert loaded.text == "root rules"
    assert loaded.loaded is True
    assert loaded.truncated is False
    assert loaded.metadata["source"] == "AGENTS.md"


def test_project_instructions_ignore_parent_and_nested_files(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("parent rules", encoding="utf-8")
    (nested / "AGENTS.md").write_text("nested rules", encoding="utf-8")

    loaded = RootProjectInstructionsProvider().load(workspace)

    assert loaded == ProjectInstructions()


def test_large_project_instructions_are_deterministically_bounded(tmp_path) -> None:
    source = "规则abc" * 100
    (tmp_path / "AGENTS.md").write_text(source, encoding="utf-8")
    provider = RootProjectInstructionsProvider(max_bytes=128)

    first = provider.load(tmp_path)
    second = provider.load(tmp_path)

    assert first == second
    assert first.truncated is True
    assert len(first.text.encode("utf-8")) <= 128
    assert "AGENTS.md truncated" in first.text
    assert first.omitted_bytes > 0
    assert first.diagnostics == ("AGENTS_TRUNCATED",)


def test_project_instructions_are_frozen_per_manager_and_refresh_next_turn(tmp_path) -> None:
    instructions = tmp_path / "AGENTS.md"
    instructions.write_text("first turn", encoding="utf-8")
    provider = RootProjectInstructionsProvider()
    first_turn = ContextManager(project_instructions_provider=provider)
    first = first_turn.assemble(
        [_message("system", "canonical")],
        runtime_context=RuntimeContext(workspace=tmp_path),
    )
    instructions.write_text("second turn", encoding="utf-8")
    same_turn = first_turn.assemble(
        [_message("system", "canonical")],
        runtime_context=RuntimeContext(workspace=tmp_path),
    )
    next_turn = ContextManager(project_instructions_provider=provider).assemble(
        [_message("system", "canonical")],
        runtime_context=RuntimeContext(workspace=tmp_path),
    )

    assert first.source_sections[1].content == "first turn"
    assert same_turn.source_sections[1].content == "first turn"
    assert next_turn.source_sections[1].content == "second turn"


def test_token_estimator_handles_english_chinese_mixed_tools_and_results() -> None:
    estimator = TokenEstimator()
    english = "coding agent context " * 100
    chinese = "上下文管理" * 100
    mixed = english + chinese
    english_estimate = estimator.estimate([_message("user", english)])
    chinese_estimate = estimator.estimate([_message("user", chinese)])
    mixed_estimate = estimator.estimate([_message("user", mixed)])
    tool = ToolDefinition(
        "search",
        "search files",
        {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    )
    with_schema = estimator.estimate([_message("user", "search")], [tool])
    without_schema = estimator.estimate([_message("user", "search")])
    large_result = Message(
        role="tool",
        content=[ToolResultBlock(tool_call_id="call", content="x" * 20_000)],
    )

    assert 400 < english_estimate < len(english.encode("utf-8"))
    assert 300 < chinese_estimate < len(chinese.encode("utf-8"))
    assert mixed_estimate > english_estimate
    assert mixed_estimate > chinese_estimate
    assert with_schema > without_schema
    assert estimator.estimate([large_result]) > 4_500
    assert english_estimate != len(english.encode("utf-8"))
    assert chinese_estimate != len(chinese.encode("utf-8"))


def test_budget_policy_separates_reserve_margin_soft_and_hard_limits() -> None:
    policy = ContextBudgetPolicy(
        context_window_tokens=1_000,
        output_reserve_tokens=200,
        safety_margin_tokens=100,
        soft_threshold=0.8,
    )

    assert policy.usable_input_tokens == 700
    assert policy.soft_limit_tokens == 560
    assert policy.assess(559).pressure == "normal"
    assert policy.assess(560).pressure == "soft"
    assert policy.assess(700).pressure == "hard"
    assert policy.assess(700).fits is True
    assert policy.assess(701).pressure == "hard"
    assert policy.assess(701).fits is False


def test_task_plan_is_optional_bounded_and_has_one_in_progress() -> None:
    assert TaskState().plan is None
    plan = TaskPlan(
        [
            PlanStep("inspect", "completed"),
            PlanStep("implement", "in_progress"),
            PlanStep("validate", "pending"),
        ]
    )
    state = TaskState(plan=plan)
    history = [_message("user", "original goal")]
    before = deepcopy(history)

    state.replace_plan(TaskPlan([PlanStep("done", "completed")]))

    assert state.plan is not None
    assert state.plan.steps[0].status.value == "completed"
    assert history == before
    with pytest.raises(ValueError, match="status"):
        PlanStep("invalid", "blocked")
    with pytest.raises(ValueError, match="one in_progress"):
        TaskPlan(
            [
                PlanStep("one", "in_progress"),
                PlanStep("two", "in_progress"),
            ]
        )
    with pytest.raises(ValueError, match="at most 20"):
        TaskPlan([PlanStep(str(index)) for index in range(21)])


def test_tool_result_hard_bound_preserves_head_tail_marker_and_metadata() -> None:
    small, small_metadata = bound_text("small", max_bytes=100)
    source = "HEAD-" + "x" * 500 + "-TAIL"
    bounded, metadata = bound_text(source, max_bytes=160)

    assert small == "small"
    assert small_metadata == {}
    assert bounded.startswith("HEAD-")
    assert bounded.endswith("-TAIL")
    assert "tool result truncated" in bounded
    assert metadata["partial"] is True
    assert metadata["original_bytes"] == len(source.encode("utf-8"))
    assert metadata["omitted_bytes"] > 0
    assert len(bounded.encode("utf-8")) <= 160


def test_rendered_system_section_boundary_survives_provider_serialization() -> None:
    manager = ContextManager(
        base_system_instructions=BaseSystemInstructions("stable final response."),
    )
    plan = manager.assemble(
        [_message("system", "canonical")],
        runtime_context=RuntimeContext(workspace="/workspace"),
    )
    messages = ContextRenderer().render(plan)
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            provider="deepseek",
            base_url="https://example.invalid/v1",
            api_key="test",
            model="model",
        ),
        client=object(),
    )

    payload = provider._build_request_payload(
        LLMRequest(messages=messages),
        stream=False,
    )
    serialized_system = payload["messages"][0]["content"]

    assert serialized_system.startswith("stable final response.\n\nruntime_context:")
    assert "response.runtime_context:" not in serialized_system
    assert json.dumps(payload, ensure_ascii=False)
