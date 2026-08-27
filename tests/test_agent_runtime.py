from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.core.messages import (
    Message,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from agent.model.openai_compatible import OpenAICompatibleProvider
from agent.model.provider import LLMProvider
from agent.model.types import (
    LLMRequest,
    LLMResponse,
    ProviderCapabilities,
    ProviderConfig,
    ReasoningRetention,
    Usage,
)
from agent.runtime import (
    ModelSettings,
    SettingsConflictError,
    ThinkingKeep,
    ThinkingSettings,
    ThreadBusyError,
    ThreadRuntime,
    ThreadStatus,
    TurnStatus,
)
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


class PausingToolProvider(LLMProvider):
    """Pause the first request, then require one tool iteration."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[LLMRequest] = []

    async def chat(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            self.started.set()
            await self.release.wait()
            return LLMResponse(
                message=Message(
                    role="assistant",
                    content=[
                        ToolCallBlock(
                            id="call_missing",
                            name="missing",
                            arguments={},
                        )
                    ],
                ),
                finish_reason="tool_calls",
                usage=Usage(),
            )
        answer = "First turn complete." if len(self.requests) == 2 else "Next turn."
        return LLMResponse(
            message=Message(role="assistant", content=[TextBlock(text=answer)]),
            finish_reason="stop",
            usage=Usage(),
        )


class RecordingCompletions:
    """External SDK boundary fake that records encoded request payloads."""

    def __init__(self, responses: list[object]) -> None:
        self._responses = iter(responses)
        self.requests: list[dict[str, object]] = []

    async def create(self, **payload):
        self.requests.append(payload)
        return next(self._responses)


def sdk_response(
    text: str,
    *,
    reasoning_content: str | None = None,
) -> object:
    message = SimpleNamespace(
        role="assistant",
        content=text,
        tool_calls=None,
        reasoning_content=reasoning_content,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=None,
    )


def empty_tools(_: Path) -> ToolRegistry:
    return ToolRegistry()


def runtime_for_provider(
    provider: LLMProvider,
    *,
    tool_registry_factory=empty_tools,
    default_settings: ModelSettings | None = None,
    additional_system_instructions: str | None = None,
) -> ThreadRuntime:
    return ThreadRuntime(
        provider_resolver=lambda _config_id, _model: provider,
        default_settings=default_settings
        or ModelSettings(provider_config_id="test-provider", model="test-model"),
        tool_registry_factory=tool_registry_factory,
        additional_system_instructions=additional_system_instructions,
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ModelSettings(provider_config_id="   ", model="model"),
        lambda: ModelSettings(provider_config_id="provider", model=1),
        lambda: ModelSettings(
            provider_config_id="provider", model="model", temperature=True
        ),
        lambda: ModelSettings(
            provider_config_id="provider", model="model", max_tokens=0
        ),
        lambda: ModelSettings(
            provider_config_id="provider",
            model="model",
            thinking=ThinkingSettings(enabled=False, budget_tokens=1),
        ),
    ],
)
def test_public_model_settings_fail_closed(factory) -> None:
    with pytest.raises(ValueError):
        factory()


def test_completed_thread_accepts_a_second_turn_with_preserved_history(
    tmp_path,
) -> None:
    provider = ScriptedProvider(
        [
            LLMResponse(
                message=Message(
                    role="assistant", content=[TextBlock(text="First answer.")]
                ),
                finish_reason="stop",
                usage=Usage(),
            ),
            LLMResponse(
                message=Message(
                    role="assistant", content=[TextBlock(text="Second answer.")]
                ),
                finish_reason="stop",
                usage=Usage(),
            ),
        ]
    )
    resolved: list[tuple[str, str]] = []

    def resolve_provider(provider_config_id: str, model: str) -> LLMProvider:
        resolved.append((provider_config_id, model))
        return provider

    runtime = ThreadRuntime(
        provider_resolver=resolve_provider,
        default_settings=ModelSettings(
            provider_config_id="course-provider",
            model="course-model",
        ),
        tool_registry_factory=empty_tools,
    )
    thread = runtime.create_thread(tmp_path)

    first = asyncio.run(runtime.run_turn(thread.thread_id, "First question."))
    second = asyncio.run(runtime.run_turn(thread.thread_id, "Second question."))

    assert first.final_text == "First answer."
    assert second.final_text == "Second answer."
    assert resolved == [
        ("course-provider", "course-model"),
        ("course-provider", "course-model"),
    ]
    assert provider.requests[1].messages == [
        provider.requests[0].messages[0],
        Message(role="user", content=[TextBlock(text="First question.")]),
        Message(role="assistant", content=[TextBlock(text="First answer.")]),
        Message(role="user", content=[TextBlock(text="Second question.")]),
    ]


def test_thread_settings_reject_a_stale_version_without_overwriting(
    tmp_path,
) -> None:
    provider = ScriptedProvider([])
    runtime = runtime_for_provider(
        provider,
        default_settings=ModelSettings(
            provider_config_id="provider-a",
            model="model-a",
            temperature=0.2,
            max_tokens=1000,
        ),
    )
    thread = runtime.create_thread(tmp_path)

    updated = runtime.update_settings(
        thread.thread_id,
        expected_version=0,
        settings=ModelSettings(
            provider_config_id="provider-b",
            model="model-b",
            temperature=0.7,
            max_tokens=2000,
        ),
    )

    assert updated.version == 1
    assert updated.provider_config_id == "provider-b"
    assert runtime.get_snapshot(thread.thread_id).settings == updated
    with pytest.raises(SettingsConflictError) as captured:
        runtime.update_settings(
            thread.thread_id,
            expected_version=0,
            settings=ModelSettings(
                provider_config_id="stale-provider",
                model="stale-model",
            ),
        )
    assert captured.value.code == "SETTINGS_CONFLICT"
    assert runtime.get_snapshot(thread.thread_id).settings == updated


def test_active_turn_freezes_settings_until_its_tool_chain_finishes(
    tmp_path,
) -> None:
    async def scenario() -> tuple[list[LLMRequest], float | None]:
        provider = PausingToolProvider()
        runtime = runtime_for_provider(
            provider,
            default_settings=ModelSettings(
                provider_config_id="provider",
                model="model",
                temperature=0.2,
                max_tokens=1000,
            ),
        )
        thread = runtime.create_thread(tmp_path)
        active = asyncio.create_task(
            runtime.run_turn(thread.thread_id, "Start tool work.")
        )
        await provider.started.wait()
        runtime.update_settings(
            thread.thread_id,
            expected_version=0,
            settings=ModelSettings(
                provider_config_id="provider",
                model="model",
                temperature=0.8,
                max_tokens=2000,
            ),
        )
        provider.release.set()
        await active
        await runtime.run_turn(thread.thread_id, "Use the new settings.")
        return provider.requests, runtime.get_snapshot(thread.thread_id).settings.temperature

    requests, current_temperature = asyncio.run(scenario())

    assert [request.temperature for request in requests] == [0.2, 0.2, 0.8]
    assert [request.max_tokens for request in requests] == [1000, 1000, 2000]
    assert current_temperature == 0.8


def test_one_turn_override_changes_provider_and_thinking_without_rewriting_defaults(
    tmp_path,
) -> None:
    provider = ScriptedProvider(
        [
            LLMResponse(
                message=Message(role="assistant", content=[TextBlock(text="Override.")]),
                finish_reason="stop",
                usage=Usage(),
            ),
            LLMResponse(
                message=Message(role="assistant", content=[TextBlock(text="Default.")]),
                finish_reason="stop",
                usage=Usage(),
            ),
        ]
    )
    resolved: list[tuple[str, str]] = []

    def resolve_provider(provider_config_id: str, model: str) -> LLMProvider:
        resolved.append((provider_config_id, model))
        return provider

    defaults = ModelSettings(
        provider_config_id="provider-a",
        model="model-a",
        temperature=0.2,
        max_tokens=1000,
    )
    override = ModelSettings(
        provider_config_id="provider-b",
        model="model-b",
        temperature=0.9,
        max_tokens=3000,
        thinking=ThinkingSettings(
            enabled=True,
            budget_tokens=512,
            keep=ThinkingKeep.ALL,
        ),
    )
    runtime = ThreadRuntime(
        provider_resolver=resolve_provider,
        default_settings=defaults,
        tool_registry_factory=empty_tools,
    )
    thread = runtime.create_thread(tmp_path)

    asyncio.run(
        runtime.run_turn(
            thread.thread_id,
            "Use an override.",
            settings_override=override,
        )
    )
    asyncio.run(runtime.run_turn(thread.thread_id, "Use defaults."))

    assert resolved == [("provider-b", "model-b"), ("provider-a", "model-a")]
    assert provider.requests[0].temperature == 0.9
    assert provider.requests[0].max_tokens == 3000
    assert provider.requests[0].extra_body == {
        "thinking": {
            "type": "enabled",
            "budget_tokens": 512,
            "keep": "all",
        }
    }
    assert provider.requests[1].temperature == 0.2
    assert provider.requests[1].extra_body is None
    snapshot = runtime.get_snapshot(thread.thread_id)
    assert snapshot.settings.provider_config_id == "provider-a"
    assert snapshot.settings.version == 0


def test_switching_models_reencodes_old_reasoning_and_keeps_secrets_private(
    tmp_path,
) -> None:
    first_completions = RecordingCompletions(
        [sdk_response("First answer.", reasoning_content="private chain")]
    )
    second_completions = RecordingCompletions([sdk_response("Second answer.")])
    first_provider = OpenAICompatibleProvider(
        ProviderConfig(
            provider="first",
            base_url="https://secret-first.invalid/v1",
            api_key="first-secret-key",
            model="model-a",
            capabilities=ProviderCapabilities(
                reasoning_retention=ReasoningRetention.ALWAYS,
                reasoning_input_field="reasoning_content",
            ),
        ),
        client=SimpleNamespace(
            chat=SimpleNamespace(completions=first_completions)
        ),
    )
    second_provider = OpenAICompatibleProvider(
        ProviderConfig(
            provider="second",
            base_url="https://secret-second.invalid/v1",
            api_key="second-secret-key",
            model="model-b",
            capabilities=ProviderCapabilities(
                reasoning_retention=ReasoningRetention.NEVER,
                reasoning_input_field=None,
            ),
        ),
        client=SimpleNamespace(
            chat=SimpleNamespace(completions=second_completions)
        ),
    )
    providers = {"config-a": first_provider, "config-b": second_provider}
    runtime = ThreadRuntime(
        provider_resolver=lambda config_id, _model: providers[config_id],
        default_settings=ModelSettings(
            provider_config_id="config-a",
            model="model-a",
        ),
        tool_registry_factory=empty_tools,
    )
    thread = runtime.create_thread(tmp_path)

    asyncio.run(runtime.run_turn(thread.thread_id, "First question."))
    runtime.update_settings(
        thread.thread_id,
        expected_version=0,
        settings=ModelSettings(
            provider_config_id="config-b",
            model="model-b",
        ),
    )
    asyncio.run(runtime.run_turn(thread.thread_id, "Second question."))

    assert first_completions.requests[0]["model"] == "model-a"
    second_payload = second_completions.requests[0]
    assert second_payload["model"] == "model-b"
    first_assistant = second_payload["messages"][2]
    assert first_assistant["content"] == "First answer."
    assert "reasoning_content" not in first_assistant
    public_snapshot = repr(asdict(runtime.get_snapshot(thread.thread_id)))
    assert "first-secret-key" not in public_snapshot
    assert "second-secret-key" not in public_snapshot
    assert "secret-first.invalid" not in public_snapshot
    assert "secret-second.invalid" not in public_snapshot


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
    runtime = runtime_for_provider(provider)
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
    runtime = runtime_for_provider(
        provider,
        tool_registry_factory=create_test_tool_registry,
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
    runtime = runtime_for_provider(
        provider,
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
        runtime = runtime_for_provider(provider)
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
    runtime = runtime_for_provider(
        provider,
        tool_registry_factory=lambda _: registry,
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
