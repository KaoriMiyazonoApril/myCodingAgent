from __future__ import annotations

import asyncio

from agent.core.messages import Message, TextBlock, ToolCallBlock, ToolResultBlock
from agent.runtime.context_history import (
    CompactionCheckpoint,
    CompactionError,
    CompactionSummary,
    LLMHistoryCompactor,
    RecentRawTailSelector,
    RollingSemanticCompactor,
    ToolResultPruner,
    canonical_history_fingerprint,
    parse_atomic_history,
)
from agent.runtime.history.compaction import _units_matching_source


def _text(role: str, value: str) -> Message:
    return Message(role=role, content=[TextBlock(text=value)])


def _tool_round(name: str, call_id: str, result: str) -> list[Message]:
    return [
        Message(
            role="assistant",
            content=[
                ToolCallBlock(
                    id=call_id,
                    name=name,
                    arguments={"path": name},
                )
            ],
        ),
        Message(
            role="tool",
            content=[ToolResultBlock(tool_call_id=call_id, content=result)],
        ),
    ]


def test_atomic_parser_keeps_multi_call_interaction_together() -> None:
    history = [
        _text("user", "inspect files"),
        Message(
            role="assistant",
            content=[
                ToolCallBlock(id="call-a", name="read_file", arguments={}),
                ToolCallBlock(id="call-b", name="read_file", arguments={}),
            ],
        ),
        Message(
            role="tool",
            content=[ToolResultBlock(tool_call_id="call-a", content="A")],
        ),
        Message(
            role="tool",
            content=[ToolResultBlock(tool_call_id="call-b", content="B")],
        ),
        _text("assistant", "Both files were inspected."),
    ]

    units = parse_atomic_history(history)

    assert [len(unit.messages) for unit in units] == [1, 3, 1]
    assert units[1].tool_call_ids == ("call-a", "call-b")
    assert units[1].result_ids == ("call-a", "call-b")
    assert units[1].complete is True
    assert units[1].open is False


def test_selector_keeps_crossing_unit_and_open_interaction() -> None:
    open_call = Message(
        role="assistant",
        content=[ToolCallBlock(id="pending", name="run_command", arguments={})],
    )
    history = [
        _text("user", "old " * 10),
        *_tool_round("old-read", "old-call", "old result " * 10),
        _text("user", "recent " * 10),
        open_call,
    ]

    result = RecentRawTailSelector(usable_input_tokens=80, target_ratio=0.2).select(
        history
    )

    selected_ids = [
        block.id
        for message in result
        if message.role == "assistant"
        for block in message.content
        if isinstance(block, ToolCallBlock)
    ]
    assert "pending" in selected_ids
    assert result.compact_candidates
    assert result.retained_tokens >= result.target_tokens
    assert result.boundary == min(unit.start_index for unit in result.retained_units)

    # The candidate region never contains an orphan tool result or tool call.
    candidate_ids = {
        block.tool_call_id
        for message in result.compact_candidates
        if message.role == "tool"
        for block in message.content
        if isinstance(block, ToolResultBlock)
    }
    candidate_calls = {
        block.id
        for message in result.compact_candidates
        if message.role == "assistant"
        for block in message.content
        if isinstance(block, ToolCallBlock)
    }
    assert candidate_ids == candidate_calls


def test_pressure_pruner_replaces_only_old_large_closed_result() -> None:
    old = _tool_round("old-read", "old-call", "0123456789" * 4)
    latest = _tool_round("new-read", "new-call", "latest output")
    history = [_text("user", "start"), *old, *latest]

    result = ToolResultPruner(threshold_chars=10).prune(history)

    old_block = result[2].content[0]
    latest_block = result[4].content[0]
    assert isinstance(old_block, ToolResultBlock)
    assert old_block.content.startswith("[tool result pruned")
    assert old_block.metadata["pruned"] is True
    assert old_block.metadata["original_size"] == 40
    assert result.pruned_count == 1
    assert latest_block.content == "latest output"
    assert [message.content for message in history] != [message.content for message in result]


def test_llm_compactor_emits_synthetic_summary_and_prompt_requirements() -> None:
    class Provider:
        def __init__(self) -> None:
            self.requests = []

        async def chat(self, request):
            self.requests.append(request)
            return type(
                "Response",
                (),
                {"message": _text("assistant", "goal: inspect; open work: fix tests")},
            )()

    provider = Provider()
    compactor = LLMHistoryCompactor(provider)
    summary = asyncio.run(
        compactor.compact(
            None,
            [_text("user", "inspect app.py")],
        )
    )

    assert isinstance(summary, CompactionSummary)
    assert summary.synthetic is True
    assert "goal: inspect" in summary.text
    prompt = provider.requests[0].messages[0].content[0].text
    for marker in ("goal", "constraints", "completed work", "decisions", "files", "findings", "validation", "open work"):
        assert marker in prompt.casefold()


def test_rolling_compaction_reuses_checkpoint_and_does_not_resend_covered_units() -> None:
    class FakeCompactor:
        def __init__(self) -> None:
            self.calls = []

        async def compact(self, previous, region):
            self.calls.append((previous, list(region)))
            return CompactionSummary("rolling handoff")

    history = [
        _text("user", "first"),
        _text("assistant", "first answer"),
        _text("user", "second"),
        _text("assistant", "second answer"),
    ]
    selector = RecentRawTailSelector(usable_input_tokens=8, target_ratio=0.2)
    first = selector.select(history)
    fake = FakeCompactor()
    rolling = RollingSemanticCompactor(fake)
    first_result = asyncio.run(rolling.compact(history, first))

    assert first_result is not None
    assert first_result.checkpoint.covered_through == first.canonical_end
    assert first_result.checkpoint.canonical_fingerprint == canonical_history_fingerprint(
        history, first.canonical_end
    )
    assert first_result.checkpoint.valid_for_history(
        [*history, _text("user", "appended")]
    )
    assert fake.calls[0][0] is None

    newer_history = [*history, _text("user", "third"), _text("assistant", "third answer")]
    newer_selection = RecentRawTailSelector(usable_input_tokens=14, target_ratio=0.2).select(
        newer_history
    )
    second_result = asyncio.run(
        rolling.compact(
            newer_history,
            newer_selection,
            previous_checkpoint=first_result.checkpoint,
        )
    )

    assert second_result is not None
    assert len(fake.calls) == 2
    assert fake.calls[1][0].text == "rolling handoff"
    assert all(
        not (
            isinstance(message.content[0], TextBlock)
            and message.content[0].text in {"first", "first answer"}
        )
        for message in fake.calls[1][1]
    )
    assert second_result.checkpoint.covered_through >= first_result.checkpoint.covered_through


def test_failed_rolling_compaction_reports_previous_checkpoint_without_mutation() -> None:
    class FailingCompactor:
        async def compact(self, previous, region):
            raise RuntimeError("provider unavailable")

    history = [
        _text("user", "old"),
        _text("assistant", "answer"),
        _text("user", "new detail"),
        _text("assistant", "new answer"),
    ]
    checkpoint = CompactionCheckpoint(
        CompactionSummary("old handoff"),
        covered_through=1,
        canonical_fingerprint=canonical_history_fingerprint(history, 1),
    )
    selection = RecentRawTailSelector(usable_input_tokens=8).select(history)

    try:
        asyncio.run(
            RollingSemanticCompactor(FailingCompactor()).compact(
                history,
                selection,
                previous_checkpoint=checkpoint,
            )
        )
    except CompactionError as error:
        assert error.previous_checkpoint == checkpoint
    else:
        raise AssertionError("expected semantic compaction failure")
    assert checkpoint.summary.text == "old handoff"


def test_missing_checkpoint_fingerprint_cannot_hide_canonical_history() -> None:
    history = [
        _text("user", "first request"),
        _text("assistant", "first answer"),
        _text("user", "new request"),
    ]
    legacy = CompactionCheckpoint(
        CompactionSummary("legacy handoff"),
        covered_through=1,
    )

    assert legacy.valid_for_history(history) is False
    selection = RecentRawTailSelector(usable_input_tokens=1).select(
        history,
        checkpoint=legacy,
    )

    assert selection.metadata["covered_through"] is None
    assert selection.compact_candidates
    assert selection.compact_candidates[0] == history[0]
    assert selection.compact_candidates[1] == history[1]


def test_compactor_source_matching_requires_a_contiguous_complete_prefix() -> None:
    history = [
        _text("user", "first"),
        _text("assistant", "first answer"),
        _text("user", "second"),
        *_tool_round("run_command", "call-2", "failure"),
    ]
    units = parse_atomic_history(history)

    assert _units_matching_source(units, history[:2]) == units[:2]
    # A detached source that starts at the second candidate is not a prefix;
    # accepting it would advance a rolling checkpoint over omitted history.
    assert _units_matching_source(units, history[2:]) == []
    # A partial assistant/tool interaction is never a complete source unit.
    assert _units_matching_source(units, history[:4]) == []


def test_checkpoint_coverage_hint_cannot_exceed_bounded_compactor_source() -> None:
    class BoundedCompactor:
        last_compaction_source: list[Message] | None = None

        async def compact(self, previous, region):
            del previous
            self.last_compaction_source = list(region[:2])
            return CompactionSummary("bounded handoff")

    history = [
        _text("user", "one"),
        _text("assistant", "two"),
        _text("user", "three"),
        _text("assistant", "four"),
    ]
    selection = RecentRawTailSelector(usable_input_tokens=1).select(history)
    compactor = BoundedCompactor()

    result = asyncio.run(
        RollingSemanticCompactor(compactor).compact(
            history,
            selection,
            coverage_end=10_000,
        )
    )

    assert result is not None
    assert result.checkpoint.covered_through == 1
    assert result.checkpoint.covered_through < 10_000
