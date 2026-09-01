from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from agent.core.messages import Message, TextBlock, ToolCallBlock, ToolResultBlock
from agent.runtime import (
    BaseSystemInstructions,
    CompactionCheckpoint,
    CompactionError,
    CompactionSummary,
    ContextBudget,
    ContextLimitError,
    ContextManager,
    RuntimeContext,
    TaskPlan,
    TaskState,
    ToolResultPruner,
)
from agent.runtime.context_history import (
    LLMHistoryCompactor,
    RecentRawTailSelector,
    RollingSemanticCompactor,
    canonical_history_fingerprint,
    estimate_history_tokens,
)


def _text(role: str, value: str) -> Message:
    return Message(role=role, content=[TextBlock(text=value)])


def _long_history() -> list[Message]:
    return [
        _text("system", "canonical system"),
        _text("user", "old request " * 30),
        Message(
            role="assistant",
            content=[
                ToolCallBlock(id="read-a", name="read_file", arguments={}),
                ToolCallBlock(id="read-b", name="read_file", arguments={}),
            ],
        ),
        Message(
            role="tool",
            content=[ToolResultBlock(tool_call_id="read-a", content="A" * 800)],
        ),
        Message(
            role="tool",
            content=[ToolResultBlock(tool_call_id="read-b", content="B" * 800)],
        ),
        _text("assistant", "old findings " * 30),
        _text("user", "recent exact request"),
    ]


class _RecordingCompactor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[CompactionSummary | None, list[Message]]] = []

    async def compact(self, previous_summary, compact_region):
        self.calls.append((previous_summary, deepcopy(list(compact_region))))
        if self.fail:
            raise CompactionError("synthetic failure")
        return CompactionSummary(
            "goal: retain the request\nvalidation: none\nopen work: continue"
        )


def _manager() -> ContextManager:
    return ContextManager(
        base_system_instructions=BaseSystemInstructions("stable"),
        budget=ContextBudget(
            context_window_tokens=900,
            output_tokens=100,
            soft_threshold=0.55,
        ),
        pressure_pruner=ToolResultPruner(threshold_chars=20),
    )


def test_reduction_prunes_then_compacts_without_mutating_canonical_history() -> None:
    history = _long_history()
    original = deepcopy(history)
    compactor = _RecordingCompactor()

    plan, checkpoint = asyncio.run(
        _manager().assemble_with_reduction(
            history,
            runtime_context=RuntimeContext(workspace="/workspace"),
            task_state=TaskState(
                plan=TaskPlan([{"step": "continue", "status": "in_progress"}])
            ),
            semantic_compactor=compactor,
        )
    )

    assert checkpoint is not None
    assert history == original
    assert len(compactor.calls) == 1
    assert plan.final_fit == "fits"
    assert plan.decision_metadata["tool_results_pruned"] == 2
    assert plan.decision_metadata["compaction_performed"] is True
    assert plan.decision_metadata["checkpoint_covered_through"] == checkpoint.covered_through
    assert any(section.name == "compaction_summary" for section in plan.source_sections)
    assert all(
        not (
            message.role == "tool"
            and any(
                isinstance(block, ToolResultBlock) and len(block.content) == 800
                for block in message.content
            )
        )
        for message in plan.compacted_history
    )


def test_second_reduction_rolls_previous_checkpoint_without_resummarizing_prefix() -> None:
    first_compactor = _RecordingCompactor()
    manager = _manager()
    history = _long_history()
    _, checkpoint = asyncio.run(
        manager.assemble_with_reduction(history, semantic_compactor=first_compactor)
    )
    assert checkpoint is not None

    extended = [
        *history,
        _text("assistant", "new work " * 800),
        # Keep a self-sufficient recent tail above the selector target so the
        # preceding oversized atomic unit becomes the next rolling candidate.
        _text("user", "new exact tail " * 50),
    ]
    second_compactor = _RecordingCompactor()
    _, rolled = asyncio.run(
        manager.assemble_with_reduction(
            extended,
            checkpoint=checkpoint,
            semantic_compactor=second_compactor,
        )
    )

    assert rolled is not None
    assert len(second_compactor.calls) == 1
    previous, raw = second_compactor.calls[0]
    assert previous == checkpoint.summary
    assert raw
    assert all(message not in history[: checkpoint.covered_through + 1] for message in raw)
    assert rolled.covered_through > checkpoint.covered_through


def test_compaction_failure_preserves_previous_checkpoint_and_canonical_history() -> None:
    manager = _manager()
    history = _long_history()
    _, checkpoint = asyncio.run(
        manager.assemble_with_reduction(
            history,
            semantic_compactor=_RecordingCompactor(),
        )
    )
    assert checkpoint is not None
    extended = [
        *history,
        _text("assistant", "new pressure " * 800),
        _text("user", "new exact tail " * 50),
    ]
    original = deepcopy(extended)

    with pytest.raises(CompactionError) as captured:
        asyncio.run(
            manager.assemble_with_reduction(
                extended,
                checkpoint=checkpoint,
                semantic_compactor=_RecordingCompactor(fail=True),
            )
        )

    assert captured.value.previous_checkpoint == checkpoint
    assert extended == original


def test_irreducible_current_input_returns_context_limit_after_one_pass() -> None:
    manager = ContextManager(
        base_system_instructions=BaseSystemInstructions("stable"),
        budget=ContextBudget(context_window_tokens=200, output_tokens=50),
    )

    with pytest.raises(ContextLimitError):
        asyncio.run(
        manager.assemble_with_reduction(
            [_text("system", "canonical")],
            current_input="x" * 5000,
            semantic_compactor=_RecordingCompactor(),
        )
        )


def test_checkpoint_covered_raw_units_are_not_retained_alongside_summary() -> None:
    history = [
        _text("system", "canonical system"),
        _text("user", "covered user"),
        _text("assistant", "covered answer"),
        _text("user", "new user"),
        _text("assistant", "new answer"),
    ]
    checkpoint = CompactionCheckpoint(
        CompactionSummary("synthetic covered handoff"),
        covered_through=2,
        canonical_fingerprint=canonical_history_fingerprint(history, 2),
    )

    selection = RecentRawTailSelector(usable_input_tokens=200).select(
        history,
        checkpoint=checkpoint,
    )

    assert [
        message.content[0].text
        for message in selection
        if message.role != "system"
    ] == ["new user", "new answer"]
    assert all(
        unit.end_index > checkpoint.covered_through
        or any(message.role == "system" for message in unit.messages)
        for unit in selection.retained_units
    )


def test_oversized_atomic_compaction_request_fails_without_provider_call() -> None:
    class Provider:
        def __init__(self) -> None:
            self.requests = []

        async def chat(self, request):
            self.requests.append(request)
            return type("Response", (), {"message": _text("assistant", "bad")})()

    provider = Provider()
    compactor = LLMHistoryCompactor(provider, request_budget_tokens=500)
    canonical = [_text("user", "canonical"), _text("assistant", "answer")]
    original = deepcopy(canonical)
    checkpoint = CompactionCheckpoint(
        CompactionSummary("old handoff"),
        covered_through=-1,
        canonical_fingerprint=canonical_history_fingerprint(canonical, -1),
    )

    with pytest.raises(CompactionError) as captured:
        asyncio.run(
            RollingSemanticCompactor(compactor).compact(
                canonical,
                [
                    Message(
                        role="user",
                        content=[TextBlock(text="x" * 10_000)],
                    )
                ],
                previous_checkpoint=checkpoint,
            )
        )

    assert captured.value.previous_checkpoint == checkpoint
    assert provider.requests == []
    assert canonical == original
    assert checkpoint.summary.text == "old handoff"


def test_bounded_compaction_checkpoint_covers_only_units_sent_to_provider() -> None:
    class UserTextEstimator:
        def estimate(self, messages, tools=()):
            del tools
            return sum(
                len(block.text)
                for message in messages
                if message.role == "user"
                for block in message.content
                if isinstance(block, TextBlock)
            )

    class Provider:
        def __init__(self) -> None:
            self.requests = []

        async def chat(self, request):
            self.requests.append(request)
            return type("Response", (), {"message": _text("assistant", "bounded")})()

    canonical = [
        _text("user", "one"),
        _text("assistant", "two"),
        _text("user", "three"),
        _text("assistant", "four"),
    ]
    selector = RecentRawTailSelector(usable_input_tokens=1).select(canonical)
    provider = Provider()
    compactor = LLMHistoryCompactor(provider, estimator=UserTextEstimator())
    first_two_budget = estimate_history_tokens(
        compactor._build_messages(None, canonical[:2]), UserTextEstimator()
    ) + 1
    compactor.request_budget_tokens = first_two_budget

    result = asyncio.run(
        RollingSemanticCompactor(compactor).compact(canonical, selector)
    )

    assert result is not None
    assert len(provider.requests) == 1
    assert result.checkpoint.covered_through == 1
    assert result.checkpoint.covered_through < selector.canonical_end
    assert result.metadata["source_messages"] == 2
    assert "three" not in provider.requests[0].messages[-1].content[0].text
