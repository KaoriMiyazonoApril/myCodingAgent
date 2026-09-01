from __future__ import annotations

import json

import pytest

from agent.core.messages import Message, TextBlock, ToolCallBlock, ToolResultBlock
from agent.model.openai_compatible import OpenAICompatibleProvider
from agent.runtime import (
    CommandEvidence,
    MAX_EVIDENCE_COMMAND_CHARS,
    MAX_EVIDENCE_RESULT_ID_CHARS,
    MAX_EVIDENCE_STATUS_CHARS,
    MAX_EVIDENCE_TIMESTAMP_CHARS,
    MAX_EVIDENCE_TOOL_CHARS,
    MAX_PLAN_STEP_TEXT_CHARS,
    TaskPlan,
    TokenEstimator,
    ToolResultReducer,
)
from agent.runtime.context_history import ToolResultPruner
from agent.tools.types import ToolResult


def _payload_size(block: ToolResultBlock) -> int:
    return len(
        json.dumps(
            {
                "ok": block.ok,
                "content": block.content,
                "metadata": block.metadata,
                "error_code": block.error_code,
            },
            ensure_ascii=False,
        ).encode("utf-8")
    )


def test_reducer_bounds_metadata_for_estimator_and_provider() -> None:
    result = ToolResult(
        content="ok",
        metadata={
            "stdout": "x" * 300_000,
            "exit_code": 7,
            "status": "failed",
            "error": "command failed",
            "path": "/workspace/file",
        },
    )

    bounded = ToolResultReducer(max_bytes=4096).reduce(result)
    block = bounded.to_message_block("call-1")
    assert _payload_size(block) <= 4096
    assert bounded.metadata["exit_code"] == 7
    assert bounded.metadata["status"] == "failed"
    assert bounded.metadata["metadata_truncated"] is True
    assert TokenEstimator().estimate(
        [Message(role="tool", content=[block])],
        (),
    ) < 2_000

    encoded = OpenAICompatibleProvider._encode_tool_results([block])[0]
    assert len(encoded["content"].encode("utf-8")) <= 4096


def test_small_metadata_is_detached_but_unchanged() -> None:
    metadata = {"stdout": "small", "status": "ok", "exit_code": 0}
    result = ToolResult(content="ok", metadata=metadata)

    bounded = ToolResultReducer(max_bytes=4096).reduce(result)

    assert bounded.content == result.content
    assert bounded.metadata == metadata
    assert bounded.metadata is not metadata


def test_pressure_pruner_does_not_restore_unbounded_metadata() -> None:
    history = [
        Message(role="user", content=[TextBlock(text="first")]),
        Message(
            role="assistant",
            content=[ToolCallBlock(id="old-call", name="run_command", arguments={})],
        ),
        Message(
            role="tool",
            content=[
                ToolResultBlock(
                    tool_call_id="old-call",
                    content="ok",
                    metadata={"stdout": "x" * 300_000, "status": "ok"},
                )
            ],
        ),
        Message(role="user", content=[TextBlock(text="second")]),
        Message(
            role="assistant",
            content=[ToolCallBlock(id="latest-call", name="run_command", arguments={})],
        ),
        Message(
            role="tool",
            content=[ToolResultBlock(tool_call_id="latest-call", content="latest")],
        ),
    ]

    pruned = ToolResultPruner(threshold_chars=999_999).prune(history)
    old_block = pruned[2].content[0]
    latest_block = pruned[-1].content[0]
    assert isinstance(old_block, ToolResultBlock)
    assert old_block.metadata["metadata_truncated"] is True
    assert _payload_size(old_block) <= 64 * 1024
    assert latest_block.content == "latest"


def test_plan_and_evidence_string_fields_have_deterministic_limits() -> None:
    with pytest.raises(ValueError):
        TaskPlan([{"step": "x" * (MAX_PLAN_STEP_TEXT_CHARS + 1)}])

    for field, value in (
        ("tool", "x" * (MAX_EVIDENCE_TOOL_CHARS + 1)),
        ("command", "x" * (MAX_EVIDENCE_COMMAND_CHARS + 1)),
        ("status", "x" * (MAX_EVIDENCE_STATUS_CHARS + 1)),
        ("result_id", "x" * (MAX_EVIDENCE_RESULT_ID_CHARS + 1)),
        ("timestamp", "x" * (MAX_EVIDENCE_TIMESTAMP_CHARS + 1)),
    ):
        values = {
            "tool": "run_command",
            "command": "echo ok",
            "status": "ok",
            "result_id": "result",
            "timestamp": "2026-01-01T00:00:00Z",
        }
        values[field] = value
        with pytest.raises(ValueError):
            CommandEvidence(**values)
