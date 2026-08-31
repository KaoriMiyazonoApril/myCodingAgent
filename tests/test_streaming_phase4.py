from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.core.messages import Message, TextBlock, ToolCallBlock
from agent.model.openai_compatible import OpenAICompatibleProvider
from agent.model.errors import LLMResponseParseError
from agent.model.provider import LLMProvider
from agent.model.types import (
    ErrorEvent,
    LLMRequest,
    LLMResponse,
    MessageEndEvent,
    ProviderConfig,
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolCallDeltaEvent,
    Usage,
)
from agent.runtime import AllowAllPolicy, ApprovalMode, MessageAssembler
from agent.host.event_stream import EventStreamAdapter
from agent.runtime.events import EventBuffer
from agent.runtime.model_invoker import ModelInvoker
from agent.runtime import ModelSettings, ThreadRuntime
from agent.tools.registry import ToolRegistry
from agent.tools.types import ToolDefinition, ToolResult
from agent.tools.local import create_local_tool_registry
from tests.sandbox_support import DeterministicSandboxBackend
from agent.runtime.settings import TurnConfig


def _chunk(*, content=None, reasoning=None, tool_calls=None, finish=None, usage=None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish)],
        usage=usage,
    )


class _AsyncChunks:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.chunks:
            raise StopAsyncIteration
        return self.chunks.pop(0)


class _Completions:
    def __init__(self, result):
        self.result = result
        self.payload = None

    async def create(self, **payload):
        self.payload = payload
        return self.result


def _provider(stream):
    completions = _Completions(stream)
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            provider="test",
            base_url="https://example.invalid/v1",
            api_key="key",
            model="model",
        ),
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )
    return provider, completions


def test_openai_stream_converts_unicode_reasoning_and_indexed_fragments() -> None:
    stream = _AsyncChunks(
        [
            _chunk(content="你"),
            _chunk(reasoning="思"),
            _chunk(
                tool_calls=[
                    SimpleNamespace(
                        index=1,
                        id="call-b",
                        function=SimpleNamespace(name="write_file", arguments='{"b":'),
                    ),
                    SimpleNamespace(
                        index=0,
                        id="call-a",
                        function=SimpleNamespace(name="read_file", arguments='{"a":1}'),
                    ),
                ]
            ),
            _chunk(
                content="好",
                tool_calls=[
                    SimpleNamespace(
                        index=1,
                        function=SimpleNamespace(name=None, arguments='"x"}'),
                    )
                ],
                finish="tool_calls",
                usage=SimpleNamespace(
                    prompt_tokens=4,
                    completion_tokens=5,
                    total_tokens=9,
                ),
            ),
        ]
    )
    provider, completions = _provider(stream)

    async def scenario():
        return [event async for event in provider.stream(LLMRequest(messages=[]))]

    events = asyncio.run(scenario())

    assert isinstance(events[0], TextDeltaEvent)
    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [
        "你",
        "好",
    ]
    assert [event.text for event in events if isinstance(event, ReasoningDeltaEvent)] == [
        "思"
    ]
    calls = [event for event in events if isinstance(event, ToolCallDeltaEvent)]
    assert [(call.index, call.id, call.name, call.arguments_delta) for call in calls] == [
        (1, "call-b", "write_file", '{"b":'),
        (0, "call-a", "read_file", '{"a":1}'),
        (1, None, None, '"x"}'),
    ]
    assert isinstance(events[-1], MessageEndEvent)
    assert events[-1].finish_reason == "tool_calls"
    assert events[-1].usage == Usage(input_tokens=4, output_tokens=5, total_tokens=9)
    assert completions.payload["stream"] is True


def test_message_assembler_orders_calls_and_defers_argument_json() -> None:
    assembler = MessageAssembler()
    assembler.add(TextDeltaEvent(text="前"))
    assembler.add(ToolCallDeltaEvent(index=1, id="b", name="second", arguments_delta='{"x":'))
    assembler.add(ToolCallDeltaEvent(index=0, id="a", name="first", arguments_delta='{"ok":true}'))
    assembler.add(ToolCallDeltaEvent(index=1, arguments_delta="oops"))
    assembler.add(TextDeltaEvent(text="后"))
    assembler.add(MessageEndEvent(finish_reason="tool_calls"))

    response = assembler.assemble()

    assert response.message.content[0] == TextBlock(text="前后")
    first, second = response.message.content[1:]
    assert first == ToolCallBlock(
        id="a", name="first", arguments={"ok": True}, raw_arguments='{"ok":true}'
    )
    assert second.arguments is None
    assert second.arguments_error == "invalid JSON arguments"
    assert second.raw_arguments == '{"x":oops'


def test_message_assembler_rejects_id_rebinding_after_fragmentation() -> None:
    assembler = MessageAssembler()
    assembler.add(ToolCallDeltaEvent(index=0, id="a", name="read_file"))
    with pytest.raises(LLMResponseParseError, match="changed index"):
        assembler.add(ToolCallDeltaEvent(index=1, id="a"))


class _StreamingProvider(LLMProvider):
    def __init__(self, attempts):
        self.attempts = iter(attempts)
        self.calls = 0

    async def chat(self, request: LLMRequest) -> LLMResponse:
        raise AssertionError("stream should be used")

    async def stream(self, request: LLMRequest):
        self.calls += 1
        events = next(self.attempts)
        for event in events:
            yield event


def _config() -> TurnConfig:
    from agent.runtime.settings import ModelSettings

    return TurnConfig.from_model_settings(
        ModelSettings(provider_config_id="p", model="m"),
        settings_version=0,
        system_prompt="system",
        reasoning_visibility="hidden",
    )


def test_model_stream_retries_before_first_delta_but_not_after_output() -> None:
    from agent.model.errors import LLMConnectionError

    class Provider(_StreamingProvider):
        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                raise LLMConnectionError("temporary")
            yield TextDeltaEvent(text="ok")
            yield MessageEndEvent(finish_reason="stop")

    provider = Provider([])
    invoker = ModelInvoker(provider, _config(), retry_delays=(0, 0))
    emitted = []
    response = asyncio.run(invoker.stream([], [], on_event=emitted.append))
    assert response.message.content == [TextBlock(text="ok")]
    assert provider.calls == 2
    assert [type(event) for event in emitted] == [TextDeltaEvent, MessageEndEvent]

    class EventProvider(_StreamingProvider):
        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                yield ErrorEvent(
                    message="temporary",
                    error_code="LLMConnectionError",
                    retryable=True,
                )
                return
            yield TextDeltaEvent(text="recovered")
            yield MessageEndEvent(finish_reason="stop")

    event_provider = EventProvider([])
    event_emitted = []
    response = asyncio.run(
        ModelInvoker(event_provider, _config(), retry_delays=(0, 0)).stream(
            [], [], on_event=event_emitted.append
        )
    )
    assert response.message.content == [TextBlock(text="recovered")]
    assert not any(isinstance(event, ErrorEvent) for event in event_emitted)
    assert event_provider.calls == 2

    class NoRetryProvider(_StreamingProvider):
        async def stream(self, request):
            self.calls += 1
            yield TextDeltaEvent(text="partial")
            raise LLMConnectionError("after output")

    no_retry = NoRetryProvider([])
    invoker = ModelInvoker(no_retry, _config(), retry_delays=(0, 0))
    after_error_events = []
    with pytest.raises(LLMConnectionError):
        asyncio.run(invoker.stream([], [], on_event=after_error_events.append))
    assert no_retry.calls == 1
    assert any(isinstance(event, ErrorEvent) for event in after_error_events)

    class TerminalProvider(_StreamingProvider):
        async def stream(self, request):
            self.calls += 1
            raise LLMConnectionError("still unavailable")
            yield  # pragma: no cover - keeps this an async generator

    terminal = TerminalProvider([])
    terminal_events = []
    with pytest.raises(LLMConnectionError):
        asyncio.run(
            ModelInvoker(terminal, _config(), retry_delays=(0, 0)).stream(
                [], [], on_event=terminal_events.append
            )
        )
    assert terminal.calls == 3
    assert len(
        [event for event in terminal_events if isinstance(event, ErrorEvent)]
    ) == 1


def test_event_buffer_subscription_wakes_without_blocking_or_unbounded_queue() -> None:
    async def scenario():
        buffer = EventBuffer(3)
        subscription = buffer.subscribe()
        waiter = asyncio.create_task(subscription.read())
        await asyncio.sleep(0)
        first = buffer.emit_thread(thread_id="thread", event_type="one", payload={})
        batch = await asyncio.wait_for(waiter, timeout=0.5)
        assert [event.event_id for event in batch.events] == [first.event_id]
        next_wait = asyncio.create_task(subscription.read(first.event_id))
        buffer.emit_thread(thread_id="thread", event_type="two", payload={})
        buffer.emit_thread(thread_id="thread", event_type="three", payload={})
        second_batch = await asyncio.wait_for(next_wait, timeout=0.5)
        assert [event.type for event in second_batch.events] == ["two", "three"]
        assert len(buffer._subscriptions) == 1
        await subscription.aclose()
        assert not buffer._subscriptions

    asyncio.run(scenario())


class _RealtimeThreads:
    def __init__(self) -> None:
        self.thread_id = "thread"
        self.buffer = EventBuffer(8)

    def subscribe_events(self, thread_id: str, *, after_event_id=None):
        assert thread_id == self.thread_id
        return self.buffer.subscribe(after_event_id)

    def get_thread(self, thread_id: str):
        assert thread_id == self.thread_id
        return {
            "snapshot": {"status": "idle", "latest_turn": None},
            "event_cursor": self.buffer.read().latest_event_id,
        }


class _NoSubmissions:
    def inspect(self, thread_id: str):
        return None

    def inspect_failure(self, thread_id: str):
        return None


async def _false_disconnect() -> bool:
    return False


async def _true_disconnect() -> bool:
    return True


def test_realtime_sse_waits_for_event_instead_of_busy_heartbeats() -> None:
    async def scenario() -> None:
        threads = _RealtimeThreads()
        adapter = EventStreamAdapter(
            threads,
            _NoSubmissions(),
            heartbeat_interval=1.0,
        )
        stream = adapter.stream(
            threads.thread_id,
            after_event_id=None,
            disconnected=_false_disconnect,
        )
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.02)
        event = threads.buffer.emit_thread(
            thread_id=threads.thread_id,
            event_type="model_text_delta",
            payload={"text": "即时"},
        )
        frame = await asyncio.wait_for(pending, timeout=0.5)
        assert frame.startswith("event: model_text_delta\n")
        assert event.event_id in frame
        await stream.aclose()

    asyncio.run(scenario())


def test_realtime_sse_heartbeat_is_throttled() -> None:
    async def scenario() -> tuple[str, str]:
        threads = _RealtimeThreads()
        adapter = EventStreamAdapter(
            threads,
            _NoSubmissions(),
            heartbeat_interval=0.05,
        )
        stream = adapter.stream(
            threads.thread_id,
            after_event_id=None,
            disconnected=_false_disconnect,
        )
        first = await asyncio.wait_for(anext(stream), timeout=0.5)
        second = await asyncio.wait_for(anext(stream), timeout=0.5)
        await stream.aclose()
        return first, second

    first, second = asyncio.run(scenario())
    assert first == ": heartbeat\n\n"
    assert second == ": heartbeat\n\n"


def test_realtime_sse_disconnect_closes_subscription_and_reader_task() -> None:
    async def scenario() -> None:
        threads = _RealtimeThreads()
        adapter = EventStreamAdapter(threads, _NoSubmissions(), heartbeat_interval=1)
        stream = adapter.stream(
            threads.thread_id,
            after_event_id=None,
            disconnected=_true_disconnect,
        )
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert not threads.buffer._subscriptions
        await stream.aclose()

    asyncio.run(scenario())


def test_runtime_appends_only_completed_streamed_messages_and_pairs_tool_result(tmp_path) -> None:
    class Provider(LLMProvider):
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, request: LLMRequest) -> LLMResponse:
            raise AssertionError("chat fallback is not expected")

        async def stream(self, request: LLMRequest):
            self.calls += 1
            if self.calls == 1:
                yield TextDeltaEvent(text="准备")
                yield ToolCallDeltaEvent(
                    index=0,
                    id="call-read",
                    name="read_file",
                    arguments_delta='{"path":"README.md"}',
                )
                yield MessageEndEvent(finish_reason="tool_calls")
            else:
                yield TextDeltaEvent(text="完成")
                yield MessageEndEvent(
                    finish_reason="stop",
                    usage=Usage(input_tokens=1, output_tokens=2, total_tokens=3),
                )

    def tools(_: object) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="read_file",
                description="read",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            ),
            lambda arguments: ToolResult(content="contents", metadata={}),
        )
        return registry

    provider = Provider()
    runtime = ThreadRuntime(
        provider_resolver=lambda _provider, _model: provider,
        default_settings=ModelSettings(provider_config_id="p", model="m"),
        tool_registry_factory=tools,
    )
    thread = runtime.create_thread(tmp_path)
    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Inspect"))

    assert summary.status.value == "completed"
    assert [message["role"] for message in runtime.get_snapshot(thread.thread_id).messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    event_types = [event.type for event in runtime.get_events(thread.thread_id).events]
    assert event_types.index("model_text_delta") < event_types.index("model_message_end")
    assert event_types.index("model_message_end") < event_types.index("model_response")
    assert event_types[-1] == "turn_completed"


def test_streaming_cross_phase_patch_command_stdin_and_final_flow(tmp_path) -> None:
    class Provider(LLMProvider):
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, request: LLMRequest) -> LLMResponse:
            raise AssertionError("chat fallback is not expected")

        async def stream(self, request: LLMRequest):
            self.calls += 1
            if self.calls == 1:
                raw = json.dumps(
                    {
                        "patch": "*** Begin Patch\n*** Add File: artifact.txt\n"
                        "+created\n*** End Patch",
                    }
                )
                yield ToolCallDeltaEvent(
                    index=0,
                    id="call-patch",
                    name="apply_patch",
                    arguments_delta=raw[:42],
                )
                yield ToolCallDeltaEvent(
                    index=0,
                    arguments_delta=raw[42:],
                )
                yield MessageEndEvent(finish_reason="tool_calls")
                return
            if self.calls == 2:
                raw = json.dumps(
                    {"command": "read line; printf 'got:%s' \"$line\""}
                )
                yield ToolCallDeltaEvent(
                    index=0,
                    id="call-exec",
                    name="exec_command",
                    arguments_delta=raw[:35],
                )
                yield ToolCallDeltaEvent(index=0, arguments_delta=raw[35:])
                yield MessageEndEvent(finish_reason="tool_calls")
                return
            if self.calls == 3:
                tool_message = request.messages[-1]
                result = tool_message.content[0]
                session_id = result.metadata["session_id"]
                raw = json.dumps(
                    {
                        "session_id": str(session_id),
                        "chars": "hello\n",
                        "yield_time_ms": 100,
                    }
                )
                yield ToolCallDeltaEvent(
                    index=0,
                    id="call-stdin",
                    name="write_stdin",
                    arguments_delta=raw,
                )
                yield MessageEndEvent(finish_reason="tool_calls")
                return
            yield TextDeltaEvent(text="全部完成")
            yield MessageEndEvent(finish_reason="stop")

    provider = Provider()
    runtime = ThreadRuntime(
        provider_resolver=lambda _provider, _model: provider,
        default_settings=ModelSettings(provider_config_id="p", model="m"),
        tool_registry_factory=lambda workspace: create_local_tool_registry(
            workspace,
            sandbox_backend=DeterministicSandboxBackend(),
        ),
        tool_policy=AllowAllPolicy(),
    )
    thread = runtime.create_thread(tmp_path)
    summary = asyncio.run(runtime.run_turn(thread.thread_id, "完成全流程"))

    assert summary.status.value == "completed"
    assert (tmp_path / "artifact.txt").read_text(encoding="utf-8") == "created\n"
    snapshot = runtime.get_snapshot(thread.thread_id)
    assert [message["role"] for message in snapshot.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    assert any(
        block.get("tool_call_id") == "call-stdin"
        and "got:hello" in str(block.get("content"))
        for message in snapshot.messages
        for block in message.get("content", [])
        if block.get("type") == "tool_result"
    )
    events = runtime.get_events(thread.thread_id).events
    event_types = [event.type for event in events]
    assert event_types.index("model_tool_call_delta") < event_types.index("tool_requested")
    assert event_types.index("file_changed") < event_types.index("tool_finished")
    assert "command_started" in event_types
    assert "command_output_delta" in event_types
    assert event_types.index("command_output_delta") < event_types.index("tool_finished", event_types.index("command_output_delta"))
    assert event_types[-1] == "turn_completed"


def test_streaming_dangerous_command_approval_and_never_denial(tmp_path) -> None:
    class Provider(LLMProvider):
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, request: LLMRequest) -> LLMResponse:
            raise AssertionError("chat fallback is not expected")

        async def stream(self, request: LLMRequest):
            self.calls += 1
            if self.calls == 1:
                yield ToolCallDeltaEvent(
                    index=0,
                    id="call-danger",
                    name="exec_command",
                    arguments_delta=json.dumps({"command": "rm -rf build"}),
                )
                yield MessageEndEvent(finish_reason="tool_calls")
                return
            yield TextDeltaEvent(text="处理完成")
            yield MessageEndEvent(finish_reason="stop")

    def tools(_: object) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="exec_command",
                description="run",
                parameters={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                    "additionalProperties": False,
                },
            ),
            lambda arguments: ToolResult(content="executed", metadata={}),
        )
        return registry

    provider = Provider()
    runtime = ThreadRuntime(
        provider_resolver=lambda _provider, _model: provider,
        default_settings=ModelSettings(
            provider_config_id="p",
            model="m",
            approval_mode=ApprovalMode.ON_REQUEST,
        ),
        tool_registry_factory=tools,
        approval_timeout_seconds=1,
    )
    thread = runtime.create_thread(tmp_path)

    async def approve() -> None:
        active = asyncio.create_task(runtime.run_turn(thread.thread_id, "删掉构建目录"))
        approval_id = None
        for _ in range(100):
            approval = next(
                (
                    event
                    for event in runtime.get_events(thread.thread_id).events
                    if event.type == "approval_requested"
                ),
                None,
            )
            if approval is not None:
                approval_id = approval.payload["approval_id"]
                break
            await asyncio.sleep(0.01)
        assert isinstance(approval_id, str)
        assert runtime.resolve_approval(
            thread.thread_id,
            approval_id=approval_id,
            approved=True,
        )
        summary = await active
        assert summary.status.value == "completed"

    asyncio.run(approve())
    approval_events = [
        event.type for event in runtime.get_events(thread.thread_id).events
    ]
    assert "approval_requested" in approval_events
    assert "approval_resolved" in approval_events

    never_provider = Provider()
    never_runtime = ThreadRuntime(
        provider_resolver=lambda _provider, _model: never_provider,
        default_settings=ModelSettings(
            provider_config_id="p",
            model="m",
            approval_mode=ApprovalMode.NEVER,
        ),
        tool_registry_factory=tools,
    )
    never_thread = never_runtime.create_thread(tmp_path)
    summary = asyncio.run(never_runtime.run_turn(never_thread.thread_id, "删掉构建目录"))
    assert summary.status.value == "completed"
    never_events = never_runtime.get_events(never_thread.thread_id).events
    assert not any(event.type == "approval_requested" for event in never_events)
    denied = next(event for event in never_events if event.type == "tool_finished")
    assert denied.payload["result"]["error_code"] == "POLICY_DENIED"
    assert denied.payload["result"]["metadata"]["reason_code"] == "DESTRUCTIVE_COMMAND_NEVER"


def test_streaming_cancellation_does_not_commit_partial_assistant_message(tmp_path) -> None:
    class Provider(LLMProvider):
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def chat(self, request: LLMRequest) -> LLMResponse:
            raise AssertionError("chat fallback is not expected")

        async def stream(self, request: LLMRequest):
            self.started.set()
            yield TextDeltaEvent(text="半条 assistant")
            await asyncio.Event().wait()

    async def scenario():
        provider = Provider()
        runtime = ThreadRuntime(
            provider_resolver=lambda _provider, _model: provider,
            default_settings=ModelSettings(provider_config_id="p", model="m"),
            tool_registry_factory=lambda _workspace: ToolRegistry(),
        )
        thread = runtime.create_thread(tmp_path)
        active = asyncio.create_task(runtime.run_turn(thread.thread_id, "取消流式响应"))
        await provider.started.wait()
        for _ in range(100):
            if any(
                event.type == "model_text_delta"
                for event in runtime.get_events(thread.thread_id).events
            ):
                break
            await asyncio.sleep(0.01)
        assert runtime.cancel_turn(thread.thread_id) is True
        summary = await active
        return summary, runtime.get_snapshot(thread.thread_id), runtime.get_events(
            thread.thread_id
        ).events

    summary, snapshot, events = asyncio.run(scenario())
    assert summary.status.value == "cancelled"
    assert [message["role"] for message in snapshot.messages] == ["user"]
    assert not any(event.type == "model_response" for event in events)
    assert [event.type for event in events][-2:] == [
        "turn_cancel_requested",
        "turn_cancelled",
    ]
