from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.core.messages import Message, TextBlock, ToolCallBlock, ToolResultBlock
from agent.model.provider import LLMProvider
from agent.model.types import LLMRequest, LLMResponse, Usage
from agent.runtime import ThreadBusyError, ThreadRuntime, ThreadStatus, TurnStatus
from agent.tools.registry import ToolRegistry
from agent.tools.types import ToolDefinition, ToolResult
from tests.sandbox_support import create_test_tool_registry


class ScriptedProvider(LLMProvider):
    """External-model test adapter returning prearranged complete responses."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = iter(responses)
        self.requests: list[LLMRequest] = []

    async def chat(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return next(self._responses)


class PausingProvider(LLMProvider):
    """External-model adapter that keeps one Turn active until released."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def chat(self, request: LLMRequest) -> LLMResponse:
        self.started.set()
        await self.release.wait()
        return LLMResponse(
            message=Message(
                role="assistant", content=[TextBlock(text="First turn complete.")]
            ),
            finish_reason="stop",
            usage=Usage(),
        )


def empty_tools(_: Path) -> ToolRegistry:
    return ToolRegistry()


def test_user_can_complete_a_turn_without_tool_calls(tmp_path) -> None:
    provider = ScriptedProvider(
        [
            LLMResponse(
                message=Message(
                    role="assistant", content=[TextBlock(text="Task complete.")]
                ),
                finish_reason="stop",
                usage=Usage(input_tokens=8, output_tokens=3, total_tokens=11),
            )
        ]
    )
    runtime = ThreadRuntime(provider=provider, tool_registry_factory=empty_tools)
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Inspect the project."))

    assert summary.status is TurnStatus.COMPLETED
    assert summary.final_text == "Task complete."
    assert summary.iterations == 1
    assert summary.tool_calls == 0
    snapshot = runtime.get_snapshot(thread.thread_id)
    assert snapshot.status is ThreadStatus.IDLE
    assert snapshot.active_turn_id is None
    assert snapshot.completed_turns == 1


def test_model_can_use_a_real_file_tool_and_continue_to_a_final_answer(
    tmp_path,
) -> None:
    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    provider = ScriptedProvider(
        [
            LLMResponse(
                message=Message(
                    role="assistant",
                    content=[
                        ToolCallBlock(
                            id="call_read",
                            name="read_file",
                            arguments={"path": "hello.txt"},
                            raw_arguments='{"path":"hello.txt"}',
                        )
                    ],
                ),
                finish_reason="tool_calls",
                usage=Usage(),
            ),
            LLMResponse(
                message=Message(
                    role="assistant",
                    content=[TextBlock(text="The tool returned hello.")],
                ),
                finish_reason="stop",
                usage=Usage(),
            ),
        ]
    )
    runtime = ThreadRuntime(
        provider=provider, tool_registry_factory=create_test_tool_registry
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Read hello.txt."))

    assert summary.status is TurnStatus.COMPLETED
    assert summary.final_text == "The tool returned hello."
    assert summary.iterations == 2
    assert summary.tool_calls == 1
    second_request = provider.requests[1]
    assert [message.role for message in second_request.messages] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    result_block = second_request.messages[-1].content[0]
    assert result_block == ToolResultBlock(
        tool_call_id="call_read",
        content="1: hello",
        metadata={
            "path": "hello.txt",
            "requested_offset": 1,
            "requested_limit": 200,
            "start_line": 1,
            "end_line": 1,
            "returned_lines": 1,
            "total_lines": 1,
            "truncated": False,
        },
        error_code=None,
    )


def test_additional_system_instructions_preserve_default_coding_rules(
    tmp_path,
) -> None:
    provider = ScriptedProvider(
        [
            LLMResponse(
                message=Message(
                    role="assistant", content=[TextBlock(text="Task complete.")]
                ),
                finish_reason="stop",
                usage=Usage(),
            )
        ]
    )
    runtime = ThreadRuntime(
        provider=provider,
        tool_registry_factory=empty_tools,
        additional_system_instructions="Follow the course rubric.",
    )
    thread = runtime.create_thread(tmp_path)

    asyncio.run(runtime.run_turn(thread.thread_id, "Inspect the project."))

    system_text = provider.requests[0].messages[0].content[0]
    assert isinstance(system_text, TextBlock)
    normalized_prompt = system_text.text.casefold()
    assert "workspace-relative" in normalized_prompt
    assert "prefer file tools" in normalized_prompt
    assert "handle tool errors honestly" in normalized_prompt
    assert "summarize the work and validation" in normalized_prompt
    assert system_text.text.endswith("Follow the course rubric.")


def test_thread_rejects_a_second_turn_while_one_is_running(tmp_path) -> None:
    async def scenario() -> tuple[TurnStatus, ThreadStatus]:
        provider = PausingProvider()
        runtime = ThreadRuntime(provider=provider, tool_registry_factory=empty_tools)
        thread = runtime.create_thread(tmp_path)
        first_turn = asyncio.create_task(
            runtime.run_turn(thread.thread_id, "Start the first turn.")
        )
        await provider.started.wait()
        try:
            with pytest.raises(ThreadBusyError):
                await asyncio.wait_for(
                    runtime.run_turn(thread.thread_id, "Start another turn."),
                    timeout=0.05,
                )
        finally:
            provider.release.set()
        first_summary = await first_turn
        return first_summary.status, runtime.get_snapshot(thread.thread_id).status

    turn_status, thread_status = asyncio.run(scenario())

    assert turn_status is TurnStatus.COMPLETED
    assert thread_status is ThreadStatus.IDLE


def test_multiple_tools_and_a_recoverable_error_preserve_result_order(
    tmp_path,
) -> None:
    provider = ScriptedProvider(
        [
            LLMResponse(
                message=Message(
                    role="assistant",
                    content=[
                        ToolCallBlock(
                            id="call_record",
                            name="record",
                            arguments={"value": "first"},
                        ),
                        ToolCallBlock(
                            id="call_missing",
                            name="missing",
                            arguments={},
                        ),
                    ],
                ),
                finish_reason="tool_calls",
                usage=Usage(),
            ),
            LLMResponse(
                message=Message(
                    role="assistant",
                    content=[TextBlock(text="Recovered from the missing tool.")],
                ),
                finish_reason="stop",
                usage=Usage(),
            ),
        ]
    )
    executions: list[str] = []
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="record",
            description="Record one value.",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        ),
        lambda arguments: (
            executions.append(str(arguments["value"]))
            or ToolResult(content="recorded", metadata={})
        ),
    )
    runtime = ThreadRuntime(
        provider=provider, tool_registry_factory=lambda _: registry
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Run both tools."))

    assert executions == ["first"]
    assert summary.final_text == "Recovered from the missing tool."
    assert summary.tool_calls == 2
    result_messages = provider.requests[1].messages[-2:]
    assert result_messages == [
        Message(
            role="tool",
            content=[
                ToolResultBlock(
                    tool_call_id="call_record",
                    content="recorded",
                    metadata={},
                    error_code=None,
                )
            ],
        ),
        Message(
            role="tool",
            content=[
                ToolResultBlock(
                    tool_call_id="call_missing",
                    content="unknown tool: missing",
                    metadata={"tool": "missing"},
                    error_code="UNKNOWN_TOOL",
                )
            ],
        ),
    ]
