from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
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
from agent.model.errors import LLMAuthenticationError, LLMConnectionError
from agent.model.provider import LLMProvider
from agent.model.types import (
    LLMRequest,
    LLMResponse,
    ProviderCapabilities,
    ProviderConfig,
    ReasoningRetention,
    ThinkingCapabilities,
    Usage,
)
from agent.runtime import (
    AgentLimits,
    ModelSettings,
    SettingsConflictError,
    ThinkingKeep,
    ThinkingSettings,
    ThreadBusyError,
    ThreadRuntime,
    ThreadStatus,
    TurnSettingsOverride,
    TurnStatus,
    UnsupportedModelSettingError,
    WorkspaceBusyError,
)
from agent.runtime.run_controller import RunController
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


class ConcurrentProvider(LLMProvider):
    """Hold any number of model calls while exposing how many started."""

    def __init__(self) -> None:
        self.started_count = 0
        self.changed = asyncio.Event()
        self.release = asyncio.Event()

    async def chat(self, request: LLMRequest) -> LLMResponse:
        self.started_count += 1
        self.changed.set()
        await self.release.wait()
        return final_response("Concurrent turn complete.")

    async def wait_for_started(self, count: int) -> None:
        while self.started_count < count:
            self.changed.clear()
            await self.changed.wait()


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
    **runtime_options,
) -> ThreadRuntime:
    return ThreadRuntime(
        provider_resolver=lambda _config_id, _model: provider,
        default_settings=default_settings
        or ModelSettings(provider_config_id="test-provider", model="test-model"),
        tool_registry_factory=tool_registry_factory,
        additional_system_instructions=additional_system_instructions,
        **runtime_options,
    )


@pytest.mark.parametrize(
    "runtime_option",
    [
        {"event_buffer_capacity": 0},
        {"event_buffer_capacity": True},
        {"reasoning_visibility": "public"},
    ],
)
def test_public_event_configuration_fails_closed(runtime_option) -> None:
    with pytest.raises(ValueError):
        ThreadRuntime(
            provider_resolver=lambda _config_id, _model: ScriptedProvider([]),
            default_settings=ModelSettings(
                provider_config_id="test-provider", model="test-model"
            ),
            tool_registry_factory=empty_tools,
            **runtime_option,
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
    provider.capabilities = ProviderCapabilities(
        thinking=ThinkingCapabilities(
            supported=True,
            supports_budget_tokens=True,
            supported_keep_values=("all",),
        )
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
    override = TurnSettingsOverride(
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


def test_one_turn_override_can_change_only_temperature_and_inherit_defaults(
    tmp_path,
) -> None:
    provider = ScriptedProvider(
        [
            LLMResponse(
                message=Message(role="assistant", content=[TextBlock(text="Done.")]),
                finish_reason="stop",
                usage=Usage(),
            )
        ]
    )
    resolved: list[tuple[str, str]] = []
    runtime = ThreadRuntime(
        provider_resolver=lambda config_id, model: (
            resolved.append((config_id, model)) or provider
        ),
        default_settings=ModelSettings(
            provider_config_id="provider-a",
            model="model-a",
            temperature=0.2,
            max_tokens=1000,
        ),
        tool_registry_factory=empty_tools,
    )
    thread = runtime.create_thread(tmp_path)

    asyncio.run(
        runtime.run_turn(
            thread.thread_id,
            "Use a temporary temperature.",
            settings_override=TurnSettingsOverride(temperature=0.9),
        )
    )

    assert resolved == [("provider-a", "model-a")]
    assert provider.requests[0].temperature == 0.9
    assert provider.requests[0].max_tokens == 1000
    assert runtime.get_snapshot(thread.thread_id).settings.temperature == 0.2


def test_one_turn_override_can_explicitly_disable_an_inherited_optional_value(
    tmp_path,
) -> None:
    provider = ScriptedProvider(
        [
            LLMResponse(
                message=Message(role="assistant", content=[TextBlock(text="Done.")]),
                finish_reason="stop",
                usage=Usage(),
            )
        ]
    )
    provider.capabilities = ProviderCapabilities(
        thinking=ThinkingCapabilities(supported=True)
    )
    runtime = runtime_for_provider(
        provider,
        default_settings=ModelSettings(
            provider_config_id="provider",
            model="thinking-model",
            thinking=ThinkingSettings(enabled=True),
        ),
    )
    thread = runtime.create_thread(tmp_path)

    asyncio.run(
        runtime.run_turn(
            thread.thread_id,
            "Do not think for this Turn.",
            settings_override=TurnSettingsOverride(thinking=None),
        )
    )

    assert provider.requests[0].extra_body is None
    assert runtime.get_snapshot(thread.thread_id).settings.thinking == ThinkingSettings(
        enabled=True
    )


def test_thinking_settings_are_rejected_when_selected_model_does_not_support_them(
    tmp_path,
) -> None:
    provider = ScriptedProvider([])
    runtime = runtime_for_provider(
        provider,
        default_settings=ModelSettings(
            provider_config_id="provider",
            model="model-without-thinking",
            thinking=ThinkingSettings(enabled=True),
        ),
    )
    thread = runtime.create_thread(tmp_path)

    with pytest.raises(UnsupportedModelSettingError, match="does not support thinking"):
        asyncio.run(runtime.run_turn(thread.thread_id, "Think."))

    assert provider.requests == []


def test_thinking_option_is_checked_against_selected_model_capabilities(
    tmp_path,
) -> None:
    provider = ScriptedProvider([])
    provider.capabilities = ProviderCapabilities(
        thinking=ThinkingCapabilities(supported=True),
    )
    runtime = runtime_for_provider(
        provider,
        default_settings=ModelSettings(
            provider_config_id="provider",
            model="thinking-model",
            thinking=ThinkingSettings(enabled=True, budget_tokens=512),
        ),
    )
    thread = runtime.create_thread(tmp_path)

    with pytest.raises(UnsupportedModelSettingError, match="budget_tokens"):
        asyncio.run(runtime.run_turn(thread.thread_id, "Think longer."))

    assert provider.requests == []


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


def test_snapshot_and_summary_are_versioned_safe_json_with_public_history(
    tmp_path,
) -> None:
    provider = ScriptedProvider(
        [
            LLMResponse(
                message=Message(
                    role="assistant",
                    content=[
                        ReasoningBlock(text="private reasoning must stay hidden"),
                        ToolCallBlock(
                            id="call_missing",
                            name="missing",
                            arguments={"value": "public argument"},
                            raw_arguments='{"value":"public argument"}',
                        ),
                    ],
                ),
                finish_reason="tool_calls",
                usage=Usage(input_tokens=4, output_tokens=2, total_tokens=6),
            ),
            LLMResponse(
                message=Message(
                    role="assistant",
                    content=[
                        ReasoningBlock(text="another private thought"),
                        TextBlock(text="Finished safely."),
                    ],
                ),
                finish_reason="stop",
                usage=Usage(input_tokens=8, output_tokens=3, total_tokens=11),
            ),
        ]
    )
    runtime = runtime_for_provider(
        provider,
        additional_system_instructions="internal system instruction",
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Do the work."))
    snapshot = runtime.get_snapshot(thread.thread_id)
    encoded = json.dumps(snapshot.to_dict(), allow_nan=False)

    assert snapshot.schema_version == 1
    assert summary.schema_version == 1
    assert summary.stop_reason == "completed"
    assert summary.usage == {
        "input_tokens": 12,
        "output_tokens": 5,
        "total_tokens": 17,
    }
    assert snapshot.latest_turn == summary
    assert [message["role"] for message in snapshot.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert "private reasoning" not in encoded
    assert "private thought" not in encoded
    assert "internal system instruction" not in encoded
    assert "traceback" not in encoded.casefold()
    assert snapshot.created_at.endswith("Z")
    assert snapshot.updated_at.endswith("Z")
    assert summary.started_at.endswith("Z")
    assert summary.ended_at.endswith("Z")


def test_turn_events_are_ordered_complete_and_json_compatible(tmp_path) -> None:
    provider = ScriptedProvider(
        [
            LLMResponse(
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
            ),
            LLMResponse(
                message=Message(
                    role="assistant", content=[TextBlock(text="Recovered.")]
                ),
                finish_reason="stop",
                usage=Usage(),
            ),
        ]
    )
    runtime = runtime_for_provider(provider)
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Use a tool."))
    batch = runtime.get_events(thread.thread_id)
    encoded = json.dumps(batch.to_dict(), allow_nan=False)

    assert not batch.cursor_expired
    assert [event.type for event in batch.events] == [
        "turn_started",
        "model_response",
        "tool_requested",
        "tool_started",
        "tool_finished",
        "model_response",
        "turn_completed",
    ]
    assert [event.sequence for event in batch.events] == list(range(1, 8))
    assert len({event.event_id for event in batch.events}) == 7
    assert {event.thread_id for event in batch.events} == {thread.thread_id}
    assert {event.turn_id for event in batch.events} == {summary.turn_id}
    assert all(event.schema_version == 1 for event in batch.events)
    assert batch.latest_event_id == batch.events[-1].event_id
    assert "UNKNOWN_TOOL" in encoded

    batch.events[0].payload["user_message"] = "consumer mutation"
    fresh_batch = runtime.get_events(thread.thread_id)
    assert fresh_batch.events[0].payload["user_message"] == "Use a tool."


def test_event_ring_buffer_drops_old_events_and_marks_expired_cursor(
    tmp_path,
) -> None:
    async def scenario():
        provider = PausingProvider()
        runtime = ThreadRuntime(
            provider_resolver=lambda _config_id, _model: provider,
            default_settings=ModelSettings(
                provider_config_id="test-provider", model="test-model"
            ),
            tool_registry_factory=empty_tools,
            event_buffer_capacity=2,
        )
        thread = runtime.create_thread(tmp_path)
        active = asyncio.create_task(runtime.run_turn(thread.thread_id, "Wait."))
        await provider.started.wait()
        first_event_id = runtime.get_events(thread.thread_id).events[0].event_id
        provider.release.set()
        await active
        return runtime, thread.thread_id, first_event_id

    runtime, thread_id, expired_event_id = asyncio.run(scenario())
    retained = runtime.get_events(thread_id)
    expired = runtime.get_events(thread_id, after_event_id=expired_event_id)

    assert len(retained.events) == 2
    assert [event.type for event in retained.events] == [
        "model_response",
        "turn_completed",
    ]
    assert expired.cursor_expired
    assert expired.events == []
    assert runtime.get_snapshot(thread_id).latest_turn is not None


@pytest.mark.parametrize(
    ("visibility", "reasoning_event_count"),
    [("hidden", 0), ("debug", 1)],
)
def test_reasoning_is_only_emitted_as_a_dedicated_debug_event(
    tmp_path, visibility, reasoning_event_count
) -> None:
    provider = ScriptedProvider(
        [
            LLMResponse(
                message=Message(
                    role="assistant",
                    content=[
                        ReasoningBlock(text="sensitive chain"),
                        TextBlock(text="Public answer."),
                    ],
                ),
                finish_reason="stop",
                usage=Usage(),
            )
        ]
    )
    runtime = ThreadRuntime(
        provider_resolver=lambda _config_id, _model: provider,
        default_settings=ModelSettings(
            provider_config_id="test-provider", model="test-model"
        ),
        tool_registry_factory=empty_tools,
        reasoning_visibility=visibility,
    )
    thread = runtime.create_thread(tmp_path)

    asyncio.run(runtime.run_turn(thread.thread_id, "Answer."))
    events = runtime.get_events(thread.thread_id).events
    reasoning_events = [event for event in events if event.type == "model_reasoning"]
    snapshot_json = json.dumps(runtime.get_snapshot(thread.thread_id).to_dict())

    assert len(reasoning_events) == reasoning_event_count
    assert "sensitive chain" not in snapshot_json
    model_payload = next(
        event.payload for event in events if event.type == "model_response"
    )
    assert "sensitive chain" not in json.dumps(model_payload)
    if visibility == "debug":
        assert reasoning_events[0].payload == {
            "iteration": 1,
            "text": "sensitive chain",
        }


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


def tool_response(*calls: ToolCallBlock) -> LLMResponse:
    return LLMResponse(
        message=Message(role="assistant", content=list(calls)),
        finish_reason="tool_calls",
        usage=Usage(input_tokens=2, output_tokens=1, total_tokens=3),
    )


def final_response(text: str = "Done.") -> LLMResponse:
    return LLMResponse(
        message=Message(role="assistant", content=[TextBlock(text=text)]),
        finish_reason="stop",
        usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5),
    )


def test_retryable_model_errors_recover_within_three_attempts(tmp_path) -> None:
    class RecoveringProvider(LLMProvider):
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, request: LLMRequest) -> LLMResponse:
            self.calls += 1
            if self.calls < 3:
                raise LLMConnectionError("temporary provider outage")
            return final_response("Recovered.")

    provider = RecoveringProvider()
    runtime = runtime_for_provider(provider, model_retry_delays=())
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Retry safely."))

    assert provider.calls == 3
    assert summary.status is TurnStatus.COMPLETED
    assert summary.final_text == "Recovered."
    assert summary.iterations == 1


def test_non_retryable_model_error_fails_immediately_without_leaking_details(
    tmp_path,
) -> None:
    class AuthenticationFailureProvider(LLMProvider):
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, request: LLMRequest) -> LLMResponse:
            self.calls += 1
            raise LLMAuthenticationError("secret-key was rejected")

    provider = AuthenticationFailureProvider()
    runtime = runtime_for_provider(provider, model_retry_delays=())
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Do not retry auth."))

    assert provider.calls == 1
    assert summary.status is TurnStatus.FAILED
    assert summary.stop_reason == "model_error"
    assert summary.error == {"code": "LLM_ERROR", "message": "model request failed"}
    assert "secret-key" not in json.dumps(runtime.get_snapshot(thread.thread_id).to_dict())


def test_iteration_budget_stops_before_an_extra_model_request(tmp_path) -> None:
    provider = ScriptedProvider(
        [
            tool_response(
                ToolCallBlock(id="missing-1", name="missing", arguments={})
            ),
            final_response("must not be requested"),
        ]
    )
    runtime = runtime_for_provider(
        provider,
        default_settings=ModelSettings(
            provider_config_id="test-provider",
            model="test-model",
            limits=AgentLimits(max_iterations=1),
        ),
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Stop after one model call."))

    assert len(provider.requests) == 1
    assert summary.status is TurnStatus.LIMIT_REACHED
    assert summary.stop_reason == "max_iterations"
    assert summary.iterations == 1
    assert summary.tool_calls == 1


def test_tool_budget_preserves_a_result_for_every_unexecuted_call(tmp_path) -> None:
    provider = ScriptedProvider(
        [
            tool_response(
                ToolCallBlock(id="first", name="missing", arguments={}),
                ToolCallBlock(id="second", name="missing", arguments={}),
            )
        ]
    )
    runtime = runtime_for_provider(
        provider,
        default_settings=ModelSettings(
            provider_config_id="test-provider",
            model="test-model",
            limits=AgentLimits(max_tool_calls=1),
        ),
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Bound the tool batch."))

    assert summary.status is TurnStatus.LIMIT_REACHED
    assert summary.stop_reason == "max_tool_calls"
    assert summary.tool_calls == 1
    public_results = [
        block
        for message in runtime.get_snapshot(thread.thread_id).messages
        for block in message["content"]
        if block["type"] == "tool_result"
    ]
    assert [result["tool_call_id"] for result in public_results] == ["first", "second"]
    assert public_results[1]["error_code"] == "LIMIT_REACHED"
    assert public_results[1]["metadata"] == {"executed": False}


def test_three_identical_consecutive_tool_failures_stop_the_turn(tmp_path) -> None:
    provider = ScriptedProvider(
        [
            tool_response(
                ToolCallBlock(id=f"missing-{index}", name="missing", arguments={"b": 2, "a": 1})
            )
            for index in range(3)
        ]
    )
    runtime = runtime_for_provider(provider)
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Avoid a failure loop."))

    assert summary.status is TurnStatus.LIMIT_REACHED
    assert summary.stop_reason == "repeated_tool_failure"
    assert summary.iterations == 3
    assert summary.tool_calls == 3


def test_different_failed_arguments_do_not_trigger_repeated_failure_limit(
    tmp_path,
) -> None:
    provider = ScriptedProvider(
        [
            tool_response(
                ToolCallBlock(
                    id=f"missing-{index}",
                    name="missing",
                    arguments={"attempt": index},
                )
            )
            for index in range(3)
        ]
        + [final_response("Changed approach.")]
    )
    runtime = runtime_for_provider(provider)
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Try distinct arguments."))

    assert summary.status is TurnStatus.COMPLETED
    assert summary.final_text == "Changed approach."
    assert summary.iterations == 4
    assert summary.tool_calls == 3


def test_execution_deadline_cancels_a_slow_model_call(tmp_path) -> None:
    runtime = runtime_for_provider(
        PausingProvider(),
        default_settings=ModelSettings(
            provider_config_id="test-provider",
            model="test-model",
            limits=AgentLimits(max_execution_seconds=0.02),
        ),
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Time-box this."))

    assert summary.status is TurnStatus.LIMIT_REACHED
    assert summary.stop_reason == "execution_timeout"
    assert runtime.get_snapshot(thread.thread_id).status is ThreadStatus.IDLE


def test_approval_pause_rearms_an_already_active_execution_deadline() -> None:
    async def scenario() -> str:
        controller = RunController(AgentLimits(max_execution_seconds=0.03))

        async def operation() -> str:
            await asyncio.sleep(0.01)
            controller.pause_deadline()
            await asyncio.sleep(0.05)
            controller.resume_deadline()
            await asyncio.sleep(0.005)
            return "approved"

        return await controller.wait(operation())

    assert asyncio.run(scenario()) == "approved"


def test_cancelling_an_active_model_call_returns_a_cancelled_summary(tmp_path) -> None:
    async def scenario():
        provider = PausingProvider()
        runtime = runtime_for_provider(provider)
        thread = runtime.create_thread(tmp_path)
        active = asyncio.create_task(runtime.run_turn(thread.thread_id, "Cancel me."))
        await provider.started.wait()

        assert runtime.cancel_turn(thread.thread_id) is True
        assert runtime.cancel_turn(thread.thread_id) is False
        summary = await active
        return runtime, thread.thread_id, summary

    runtime, thread_id, summary = asyncio.run(scenario())

    assert summary.status is TurnStatus.CANCELLED
    assert summary.stop_reason == "cancelled"
    assert runtime.get_snapshot(thread_id).status is ThreadStatus.IDLE
    assert [event.type for event in runtime.get_events(thread_id).events][-2:] == [
        "turn_cancel_requested",
        "turn_cancelled",
    ]


def test_cancelling_run_command_terminates_its_process_group(tmp_path) -> None:
    async def scenario():
        provider = ScriptedProvider(
            [
                tool_response(
                    ToolCallBlock(
                        id="long-command",
                        name="run_command",
                        arguments={
                            "command": "printf '%s' $$ > command.pid; sleep 30",
                            "timeout_ms": 60_000,
                        },
                    )
                )
            ]
        )
        runtime = runtime_for_provider(
            provider,
            tool_registry_factory=create_test_tool_registry,
        )
        thread = runtime.create_thread(tmp_path)
        active = asyncio.create_task(runtime.run_turn(thread.thread_id, "Run then cancel."))
        pid_file = tmp_path / "command.pid"
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        assert pid_file.exists()
        process_id = int(pid_file.read_text(encoding="utf-8"))
        assert runtime.cancel_turn(thread.thread_id) is True
        summary = await active
        return summary, process_id

    summary, process_id = asyncio.run(scenario())

    assert summary.status is TurnStatus.CANCELLED
    with pytest.raises(ProcessLookupError):
        os.kill(process_id, 0)


def test_turns_in_unrelated_workspaces_run_concurrently(tmp_path) -> None:
    async def scenario():
        provider = ConcurrentProvider()
        runtime = runtime_for_provider(provider)
        first_workspace = tmp_path / "first"
        second_workspace = tmp_path / "second"
        first_workspace.mkdir()
        second_workspace.mkdir()
        first = runtime.create_thread(first_workspace)
        second = runtime.create_thread(second_workspace)

        first_turn = asyncio.create_task(runtime.run_turn(first.thread_id, "First."))
        second_turn = asyncio.create_task(runtime.run_turn(second.thread_id, "Second."))
        await asyncio.wait_for(provider.wait_for_started(2), timeout=0.5)
        provider.release.set()
        return await asyncio.gather(first_turn, second_turn)

    summaries = asyncio.run(scenario())

    assert [summary.status for summary in summaries] == [
        TurnStatus.COMPLETED,
        TurnStatus.COMPLETED,
    ]


@pytest.mark.parametrize("relationship", ["same", "parent", "child"])
def test_overlapping_workspace_turns_fail_immediately_with_workspace_busy(
    tmp_path,
    relationship,
) -> None:
    async def scenario():
        provider = PausingProvider()
        runtime = runtime_for_provider(provider)
        parent = tmp_path / "workspace"
        child = parent / "nested"
        child.mkdir(parents=True)
        if relationship == "same":
            first_path, second_path = parent, parent
        elif relationship == "parent":
            first_path, second_path = child, parent
        else:
            first_path, second_path = parent, child
        first = runtime.create_thread(first_path)
        second = runtime.create_thread(second_path)
        active = asyncio.create_task(runtime.run_turn(first.thread_id, "Hold lease."))
        await provider.started.wait()
        try:
            with pytest.raises(WorkspaceBusyError) as captured:
                await runtime.run_turn(second.thread_id, "Must fail immediately.")
            assert captured.value.code == "WORKSPACE_BUSY"
        finally:
            provider.release.set()
            await active

    asyncio.run(scenario())


def test_global_active_turn_limit_is_configurable_and_fails_immediately(
    tmp_path,
) -> None:
    async def scenario():
        provider = ConcurrentProvider()
        runtime = runtime_for_provider(provider, max_active_turns=2)
        threads = []
        for name in ("one", "two", "three"):
            workspace = tmp_path / name
            workspace.mkdir()
            threads.append(runtime.create_thread(workspace))

        active = [
            asyncio.create_task(runtime.run_turn(thread.thread_id, "Hold capacity."))
            for thread in threads[:2]
        ]
        await asyncio.wait_for(provider.wait_for_started(2), timeout=0.5)
        try:
            with pytest.raises(WorkspaceBusyError, match="capacity") as captured:
                await runtime.run_turn(threads[2].thread_id, "No capacity.")
            assert captured.value.code == "WORKSPACE_BUSY"
        finally:
            provider.release.set()
            await asyncio.gather(*active)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("first_responses", "limits", "expected_status"),
    [
        ([final_response("Completed.")], AgentLimits(), TurnStatus.COMPLETED),
        (
            [
                tool_response(
                    ToolCallBlock(id="limited", name="missing", arguments={})
                )
            ],
            AgentLimits(max_iterations=1),
            TurnStatus.LIMIT_REACHED,
        ),
    ],
)
def test_terminal_turn_releases_workspace_lease(
    tmp_path,
    first_responses,
    limits,
    expected_status,
) -> None:
    provider = ScriptedProvider(first_responses + [final_response("Next thread.")])
    runtime = runtime_for_provider(
        provider,
        default_settings=ModelSettings(
            provider_config_id="test-provider",
            model="test-model",
            limits=limits,
        ),
    )
    first = runtime.create_thread(tmp_path)
    second = runtime.create_thread(tmp_path)

    first_summary = asyncio.run(runtime.run_turn(first.thread_id, "First turn."))
    second_summary = asyncio.run(runtime.run_turn(second.thread_id, "Reuse workspace."))

    assert first_summary.status is expected_status
    assert second_summary.status is TurnStatus.COMPLETED


def test_cancelled_turn_releases_workspace_lease(tmp_path) -> None:
    async def scenario():
        provider = PausingProvider()
        runtime = runtime_for_provider(provider)
        first = runtime.create_thread(tmp_path)
        second = runtime.create_thread(tmp_path)
        active = asyncio.create_task(runtime.run_turn(first.thread_id, "Cancel."))
        await provider.started.wait()
        runtime.cancel_turn(first.thread_id)
        cancelled = await active
        provider.release.set()
        after_cancel = await runtime.run_turn(second.thread_id, "After cancellation.")
        return cancelled, after_cancel

    cancelled, after_cancel = asyncio.run(scenario())

    assert cancelled.status is TurnStatus.CANCELLED
    assert after_cancel.status is TurnStatus.COMPLETED


def test_failed_turn_releases_workspace_lease(tmp_path) -> None:
    class FailOnceProvider(LLMProvider):
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, request: LLMRequest) -> LLMResponse:
            self.calls += 1
            if self.calls == 1:
                raise LLMAuthenticationError("invalid credential")
            return final_response("Recovered on another Thread.")

    runtime = runtime_for_provider(FailOnceProvider())
    first = runtime.create_thread(tmp_path)
    second = runtime.create_thread(tmp_path)

    failed = asyncio.run(runtime.run_turn(first.thread_id, "Fail."))
    after_failure = asyncio.run(runtime.run_turn(second.thread_id, "Try again."))

    assert failed.status is TurnStatus.FAILED
    assert after_failure.status is TurnStatus.COMPLETED


@pytest.mark.parametrize("max_active_turns", [0, True, 33])
def test_global_active_turn_limit_fails_closed(max_active_turns) -> None:
    with pytest.raises(ValueError, match="max_active_turns"):
        ThreadRuntime(
            provider_resolver=lambda _config_id, _model: ScriptedProvider([]),
            default_settings=ModelSettings(
                provider_config_id="test-provider", model="test-model"
            ),
            tool_registry_factory=empty_tools,
            max_active_turns=max_active_turns,
        )
