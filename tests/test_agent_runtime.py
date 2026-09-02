from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, asdict
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from agent.core.messages import (
    Message,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from agent.model.openai_compatible import (
    OpenAICompatibleClientPool,
    OpenAICompatibleProvider,
)
from agent.model.errors import LLMAuthenticationError, LLMConnectionError
from agent.model.provider import LLMProvider
from agent.model.types import (
    LLMRequest,
    LLMResponse,
    MessageEndEvent,
    ProviderCapabilities,
    ProviderConfig,
    ReasoningDeltaEvent,
    ReasoningRetention,
    TextDeltaEvent,
    ThinkingCapabilities,
    Usage,
    WorkingTailMode,
)
from agent.runtime import (
    AgentLimits,
    AllowAllPolicy,
    CompactionCheckpoint,
    CompactionSummary,
    canonical_history_fingerprint,
    ContextLimitError,
    IdempotencyConflictError,
    InMemoryThreadStore,
    ModelSettings,
    PolicyDecision,
    SettingsConflictError,
    ThinkingKeep,
    ThinkingSettings,
    ThreadBusyError,
    ThreadClosedError,
    ThreadRuntime,
    ThreadSettings,
    ThreadSnapshot,
    ThreadStatus,
    TurnConfig,
    TurnSettingsOverride,
    TurnStatus,
    UnsupportedModelSettingError,
    WorkspaceBusyError,
)
from agent.runtime.run_controller import RunController
from agent.tools.registry import ToolRegistry
from agent.tools.filesystem import content_fingerprint
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


@pytest.mark.parametrize("context_window", [0, True, 10_000_001])
def test_default_context_window_configuration_fails_closed(context_window) -> None:
    with pytest.raises(ValueError, match="default_context_window_tokens"):
        runtime_for_provider(
            ScriptedProvider([]),
            default_context_window_tokens=context_window,
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
    assert provider.requests[1].messages[1:] == [
        Message(role="user", content=[TextBlock(text="First question.")]),
        Message(role="assistant", content=[TextBlock(text="First answer.")]),
        Message(role="user", content=[TextBlock(text="Second question.")]),
    ]
    assert provider.requests[1].messages[0].content[0] == (
        provider.requests[0].messages[0].content[0]
    )
    # Runtime telemetry such as turn_id is intentionally excluded from the
    # model-visible stable epoch, so equivalent Turns share this block.
    assert provider.requests[0].messages[0].content[1] == (
        provider.requests[1].messages[0].content[1]
    )
    assert "turn_id" not in provider.requests[0].messages[0].content[1].text
    assert "runtime_context:" in provider.requests[0].messages[0].content[1].text
    assert "task_state:" in provider.requests[0].messages[0].content[1].text


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
    settings_events = [
        event
        for event in runtime.get_events(thread.thread_id).events
        if event.type == "settings_updated"
    ]
    assert len(settings_events) == 1
    assert settings_events[0].turn_id is None
    assert settings_events[0].sequence == 1
    assert settings_events[0].payload == {
        "settings_version": updated.version,
        "provider_config_id": "provider-b",
        "model": "model-b",
    }
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
    assert len(runtime.get_events(thread.thread_id).events) == 1


def test_runtime_capability_preview_uses_candidate_provider_and_model(tmp_path) -> None:
    capabilities = {
        ("provider-a", "model-a"): ProviderCapabilities(
            thinking=ThinkingCapabilities(
                supported=True,
                supports_budget_tokens=True,
                supported_keep_values=("all",),
            )
        ),
        ("provider-b", "model-b"): ProviderCapabilities(),
    }

    def resolve(provider_id: str, model: str) -> LLMProvider:
        del provider_id, model
        raise AssertionError("candidate capability preview must not create provider")

    resolve.capabilities_for = lambda provider_id, model: capabilities[(  # type: ignore[attr-defined]
        provider_id,
        model,
    )]
    runtime = ThreadRuntime(
        provider_resolver=resolve,
        default_settings=ModelSettings(
            provider_config_id="provider-a",
            model="model-a",
        ),
        tool_registry_factory=empty_tools,
    )
    thread = runtime.create_thread(tmp_path)

    first = runtime.capabilities_for(thread.thread_id)
    candidate = runtime.capabilities_for(
        thread.thread_id,
        provider_config_id="provider-b",
        model="model-b",
    )

    assert first["thinking_supported"] is True
    assert first["supports_thinking_budget"] is True
    assert candidate["thinking_supported"] is False
    assert candidate["supports_thinking_budget"] is False

    # Empty drafts are invalid candidates, not a request to inherit the
    # current Thread values.  A capability preview must fail closed before a
    # later Settings save can accidentally target the old provider/model.
    empty = runtime.capabilities_for(
        thread.thread_id,
        provider_config_id="",
        model="",
    )
    unknown = runtime.capabilities_for(
        thread.thread_id,
        provider_config_id="provider-missing",
        model="model-missing",
    )
    assert empty["thinking_supported"] is False
    assert empty["supports_thinking_budget"] is False
    assert unknown["thinking_supported"] is False
    assert unknown["supports_thinking_budget"] is False


def test_checkpoint_save_rolls_back_memory_when_persistence_fails(tmp_path) -> None:
    runtime = runtime_for_provider(ScriptedProvider([]))
    thread = runtime.create_thread(tmp_path)
    record = runtime._threads[thread.thread_id]  # type: ignore[attr-defined]
    fingerprint = canonical_history_fingerprint([], -1)
    previous = CompactionCheckpoint(
        CompactionSummary("previous handoff"),
        covered_through=-1,
        canonical_fingerprint=fingerprint,
    )
    replacement = CompactionCheckpoint(
        CompactionSummary("replacement handoff"),
        covered_through=-1,
        canonical_fingerprint=fingerprint,
    )
    record.compaction_checkpoint = previous
    runtime._persist_record(record)

    def fail_persist(_record) -> None:
        raise OSError("store unavailable")

    runtime._store.save_thread = fail_persist  # type: ignore[method-assign, attr-defined]
    with pytest.raises(OSError, match="store unavailable"):
        runtime._save_checkpoint(record, replacement)  # type: ignore[attr-defined]

    assert record.compaction_checkpoint == previous
    stored = runtime._store.get_thread(thread.thread_id)  # type: ignore[attr-defined]
    assert stored is not None
    assert stored.compaction_checkpoint == previous


def test_runtime_public_defaults_are_explicit_and_versioned(tmp_path) -> None:
    runtime = runtime_for_provider(ScriptedProvider([]))

    snapshot = runtime.create_thread(tmp_path)

    assert snapshot.settings.version == 0
    assert snapshot.settings.limits.max_iterations == 20
    assert snapshot.settings.limits.max_tool_calls == 50
    assert snapshot.settings.limits.max_execution_seconds == 900


def test_thread_creation_can_freeze_optional_initial_settings(tmp_path) -> None:
    runtime = runtime_for_provider(
        ScriptedProvider([]),
        default_settings=ModelSettings(
            provider_config_id="default-provider",
            model="default-model",
        ),
    )
    initial = ModelSettings(
        provider_config_id="web-provider",
        model="web-model",
        temperature=0.4,
        max_tokens=2048,
    )

    configured = runtime.create_thread(tmp_path, settings=initial)
    unchanged = runtime.create_thread(tmp_path)

    assert configured.settings == ThreadSettings.from_model_settings(
        initial,
        version=0,
    )
    assert unchanged.settings.provider_config_id == "default-provider"
    assert unchanged.settings.model == "default-model"
    assert unchanged.settings.version == 0


def test_turn_config_validates_and_freezes_reasoning_visibility() -> None:
    settings = ModelSettings(provider_config_id="provider", model="model")

    config = TurnConfig.from_model_settings(
        settings,
        settings_version=2,
        system_prompt="System prompt",
        reasoning_visibility="debug",
    )

    assert config.reasoning_visibility == "debug"
    with pytest.raises(FrozenInstanceError):
        config.reasoning_visibility = "hidden"
    with pytest.raises(ValueError, match="reasoning_visibility"):
        TurnConfig.from_model_settings(
            settings,
            settings_version=2,
            system_prompt="System prompt",
            reasoning_visibility="public",
        )


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
        turn_started = runtime.get_events(thread.thread_id).events[-1]
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
        settings_batch = runtime.get_events(
            thread.thread_id,
            after_event_id=turn_started.event_id,
        )
        assert not settings_batch.cursor_expired
        assert [event.type for event in settings_batch.events] == [
            "settings_updated"
        ]
        assert settings_batch.events[0].turn_id is None
        assert settings_batch.events[0].payload["settings_version"] == 1
        assert runtime.get_snapshot(thread.thread_id).settings.version == 1
        provider.release.set()
        await active
        await runtime.run_turn(thread.thread_id, "Use the new settings.")
        event_types = [
            event.type for event in runtime.get_events(thread.thread_id).events
        ]
        assert event_types.index("turn_started") < event_types.index(
            "settings_updated"
        ) < event_types.index("model_response")
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
    assert provider.requests[0].thinking is not None
    assert provider.requests[0].thinking.enabled is True
    assert provider.requests[0].thinking.budget_tokens == 512
    assert provider.requests[0].thinking.keep == "all"
    assert provider.requests[1].temperature == 0.2
    assert provider.requests[1].thinking is None
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


def test_invalid_raw_provider_arguments_remain_consistent_through_runtime(
    tmp_path,
) -> None:
    raw_arguments = '{"path":'
    invalid_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id="call_invalid",
                            function=SimpleNamespace(
                                name="read_file",
                                arguments=raw_arguments,
                            ),
                        )
                    ],
                ),
                finish_reason="tool_calls",
            )
        ],
        usage=None,
    )
    completions = RecordingCompletions(
        [invalid_response, sdk_response("Recovered from invalid arguments.")]
    )
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            provider="test",
            base_url="https://example.invalid/v1",
            api_key="test-key",
            model="test-model",
        ),
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )
    runtime = runtime_for_provider(
        provider,
        tool_registry_factory=create_test_tool_registry,
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Read a file."))
    snapshot = runtime.get_snapshot(thread.thread_id)
    events = runtime.get_events(thread.thread_id).events

    assert summary.status is TurnStatus.COMPLETED
    assert snapshot.latest_turn == summary
    assert [message["role"] for message in snapshot.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    public_call = snapshot.messages[1]["content"][0]
    public_result = snapshot.messages[2]["content"][0]
    assert public_call["raw_arguments"] == raw_arguments
    assert public_call["arguments"] is None
    assert public_call["arguments_error"] == "invalid JSON arguments"
    assert public_result["tool_call_id"] == "call_invalid"
    assert public_result["error_code"] == "INVALID_ARGUMENTS"

    second_request_history = completions.requests[1]["messages"]
    assert second_request_history[2]["tool_calls"][0]["function"][
        "arguments"
    ] == raw_arguments
    encoded_tool_result = json.loads(second_request_history[3]["content"])
    assert encoded_tool_result["error_code"] == "INVALID_ARGUMENTS"
    assert encoded_tool_result["ok"] is False

    requested = next(event for event in events if event.type == "tool_requested")
    finished = next(event for event in events if event.type == "tool_finished")
    completed = next(event for event in events if event.type == "turn_completed")
    assert requested.payload["tool_call"]["raw_arguments"] == raw_arguments
    assert requested.payload["tool_call"]["arguments_error"] == (
        "invalid JSON arguments"
    )
    assert finished.payload["result"]["tool_call_id"] == "call_invalid"
    assert finished.payload["result"]["error_code"] == "INVALID_ARGUMENTS"
    assert completed.payload["summary"] == summary.to_dict()


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
    ("visibility", "reasoning_event_count", "preview_present"),
    [("hidden", 0, False), ("visible", 0, True), ("debug", 1, True)],
)
def test_reasoning_is_only_emitted_as_a_dedicated_debug_event(
    tmp_path, visibility, reasoning_event_count, preview_present
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
    # The canonical conversation never carries reasoning, regardless of mode.
    assert "sensitive chain" not in snapshot_json
    model_payload = next(
        event.payload for event in events if event.type == "model_response"
    )
    if preview_present:
        assert model_payload["reasoning_preview"] == {
            "text": "sensitive chain",
            "truncated": False,
            "total_chars": 15,
        }
    else:
        assert "reasoning_preview" not in model_payload
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
                "line_truncated": False,
                "returned_bytes": len("1: hello".encode("utf-8")),
                "original_selected_bytes": len("hello\n".encode("utf-8")),
                "content_fingerprint": content_fingerprint(b"hello\n"),
            },
        error_code=None,
    )


def test_acceptance_read_edit_test_and_diff_loop_uses_real_local_tools(
    tmp_path,
) -> None:
    source = tmp_path / "app.py"
    source.write_text("def answer():\n    return 1\n", encoding="utf-8")
    provider = ScriptedProvider(
        [
            tool_response(
                ToolCallBlock(
                    id="accept-read",
                    name="read_file",
                    arguments={"path": "app.py"},
                )
            ),
            tool_response(
                ToolCallBlock(
                    id="accept-edit",
                    name="edit_file",
                    arguments={
                        "path": "app.py",
                        "old_string": "return 1",
                        "new_string": "return 2",
                    },
                )
            ),
            tool_response(
                ToolCallBlock(
                    id="accept-test",
                    name="run_command",
                    arguments={
                        "command": (
                            "python3 -c \"import app; "
                            "assert app.answer() == 2; print('passed')\""
                        )
                    },
                )
            ),
            final_response("Updated app.py and validation passed."),
        ]
    )
    runtime = runtime_for_provider(
        provider,
        tool_registry_factory=create_test_tool_registry,
        tool_policy=AllowAllPolicy(),
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Change the answer."))

    assert summary.status is TurnStatus.COMPLETED
    assert summary.final_text == "Updated app.py and validation passed."
    assert summary.iterations == 4
    assert summary.tool_calls == 3
    assert source.read_text(encoding="utf-8") == "def answer():\n    return 2\n"
    assert summary.modified_files == ["app.py"]
    assert "-    return 1" in summary.file_diffs[0]["diff"]
    assert "+    return 2" in summary.file_diffs[0]["diff"]
    assert summary.diff_complete is False
    command_result = next(
        block
        for message in provider.requests[3].messages
        if message.role == "tool"
        for block in message.content
        if isinstance(block, ToolResultBlock)
        and block.tool_call_id == "accept-test"
    )
    assert isinstance(command_result, ToolResultBlock)
    assert command_result.error_code is None
    assert command_result.metadata["exit_code"] == 0
    assert command_result.metadata["stdout"] == "passed\n"


def test_acceptance_failed_validation_is_returned_to_the_model_and_reported(
    tmp_path,
) -> None:
    provider = ScriptedProvider(
        [
            tool_response(
                ToolCallBlock(
                    id="failed-test",
                    name="run_command",
                    arguments={
                        "command": (
                            "python3 -c \"import sys; "
                            "print('test failed', file=sys.stderr); sys.exit(3)\""
                        )
                    },
                )
            ),
            final_response("Validation failed with exit status 3; no success claimed."),
        ]
    )
    runtime = runtime_for_provider(
        provider,
        tool_registry_factory=create_test_tool_registry,
        tool_policy=AllowAllPolicy(),
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Run validation."))

    result = next(
        block
        for message in provider.requests[1].messages
        if message.role == "tool"
        for block in message.content
        if isinstance(block, ToolResultBlock)
        and block.tool_call_id == "failed-test"
    )
    assert isinstance(result, ToolResultBlock)
    assert result.error_code == "COMMAND_FAILED"
    assert result.metadata["exit_code"] == 3
    assert result.metadata["stderr"] == "test failed\n"
    assert summary.status is TurnStatus.COMPLETED
    assert summary.final_text == (
        "Validation failed with exit status 3; no success claimed."
    )
    assert summary.diff_complete is False


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


def test_closing_an_idle_thread_preserves_snapshot_and_rejects_mutation(
    tmp_path,
) -> None:
    closed: list[str] = []
    runtime = runtime_for_provider(
        ScriptedProvider([final_response("Finished.")]),
        tool_registry_factory=lambda _: ToolRegistry(
            on_close=lambda: closed.append("closed")
        ),
    )
    thread = runtime.create_thread(tmp_path)
    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Finish once."))

    assert runtime.close_thread(thread.thread_id) is True
    snapshot = runtime.get_snapshot(thread.thread_id)

    assert snapshot.status is ThreadStatus.CLOSED
    assert snapshot.latest_turn == summary
    assert len(snapshot.messages) == 2
    assert closed == ["closed"]
    assert runtime.close_thread(thread.thread_id) is False
    assert closed == ["closed"]
    with pytest.raises(ThreadClosedError) as turn_error:
        asyncio.run(runtime.run_turn(thread.thread_id, "Too late."))
    with pytest.raises(ThreadClosedError) as settings_error:
        runtime.update_settings(
            thread.thread_id,
            expected_version=0,
            settings=ModelSettings(
                provider_config_id="test-provider",
                model="another-model",
            ),
        )
    assert turn_error.value.code == "THREAD_CLOSED"
    assert settings_error.value.code == "THREAD_CLOSED"


def test_closing_an_active_thread_cancels_it_and_releases_workspace(
    tmp_path,
) -> None:
    async def scenario() -> tuple[
        TurnSummary,
        ThreadSnapshot,
        TurnSummary,
        list[str],
    ]:
        provider = PausingProvider()
        closed_registries: list[int] = []
        registry_index = 0

        def tools(_: Path) -> ToolRegistry:
            nonlocal registry_index
            current = registry_index
            registry_index += 1
            return ToolRegistry(
                on_close=lambda: closed_registries.append(current)
            )

        runtime = runtime_for_provider(provider, tool_registry_factory=tools)
        closing_thread = runtime.create_thread(tmp_path)
        next_thread = runtime.create_thread(tmp_path)
        active = asyncio.create_task(
            runtime.run_turn(closing_thread.thread_id, "Keep running.")
        )
        await provider.started.wait()
        assert runtime.close_thread(closing_thread.thread_id) is True
        assert closed_registries == []
        cancelled = await active
        assert closed_registries == [0]
        closed_snapshot = runtime.get_snapshot(closing_thread.thread_id)
        event_types = [
            event.type
            for event in runtime.get_events(closing_thread.thread_id).events
        ]
        provider.release.set()
        next_summary = await runtime.run_turn(next_thread.thread_id, "Continue.")
        assert closed_registries == [0]
        return cancelled, closed_snapshot, next_summary, event_types

    cancelled, closed_snapshot, next_summary, event_types = asyncio.run(scenario())

    assert cancelled.status is TurnStatus.CANCELLED
    assert closed_snapshot.status is ThreadStatus.CLOSED
    assert closed_snapshot.active_turn_id is None
    assert next_summary.status is TurnStatus.COMPLETED
    assert event_types.index("thread_close_requested") < event_types.index(
        "turn_cancelled"
    )


def test_oversized_first_request_is_rejected_without_mutating_history(tmp_path) -> None:
    provider = ScriptedProvider([final_response()])
    provider.capabilities = ProviderCapabilities(context_window_tokens=2_000)
    runtime = runtime_for_provider(provider)
    thread = runtime.create_thread(tmp_path)

    with pytest.raises(ContextLimitError) as captured:
        asyncio.run(runtime.run_turn(thread.thread_id, "x" * 5_000))

    snapshot = runtime.get_snapshot(thread.thread_id)
    assert captured.value.code == "CONTEXT_LIMIT"
    assert provider.requests == []
    assert snapshot.status is ThreadStatus.IDLE
    assert snapshot.completed_turns == 0
    assert snapshot.messages == []
    rejected = runtime.get_events(thread.thread_id).events
    assert len(rejected) == 1
    assert rejected[0].type == "turn_rejected"
    assert rejected[0].payload == {
        "error": {
            "code": "CONTEXT_LIMIT",
            "message": "Turn could not start",
            "detail": "conversation exceeds the configured model context budget",
        }
    }


def test_turn_request_max_tokens_matches_resolved_output_limit(tmp_path) -> None:
    # The ContextBudget reserve and the provider request both derive from one
    # resolved output limit: official maximum only clamps, the Harness default
    # request policy applies only without an explicit thread override.
    async def scenario(capabilities, *, max_tokens=None):
        provider = ScriptedProvider([final_response()])
        provider.capabilities = capabilities
        runtime = runtime_for_provider(
            provider,
            default_settings=ModelSettings(
                provider_config_id="test-provider",
                model="test-model",
                max_tokens=max_tokens,
            ),
        )
        thread = runtime.create_thread(tmp_path)
        await runtime.run_turn(thread.thread_id, "Work.")
        return provider.requests[0].max_tokens

    deepseek_like = ProviderCapabilities(
        context_window_tokens=1_000_000,
        model_max_output_tokens=384_000,
        default_request_max_tokens=131_072,
    )
    assert asyncio.run(scenario(deepseek_like)) == 131_072
    # Explicit overrides are honored and clamped by the official maximum.
    assert asyncio.run(scenario(deepseek_like, max_tokens=500_000)) == 384_000
    assert asyncio.run(scenario(deepseek_like, max_tokens=100_000)) == 100_000
    # Unverified capabilities: omit max_tokens and let the provider default.
    assert asyncio.run(scenario(ProviderCapabilities())) is None


@pytest.mark.parametrize("visibility", ["hidden", "visible", "debug"])
def test_reasoning_delta_gating_follows_visibility(tmp_path, visibility) -> None:
    class DeltaProvider(LLMProvider):
        capabilities = ProviderCapabilities(
            thinking=ThinkingCapabilities(supported=True, default_enabled=True),
            reasoning_output_fields=("reasoning_content",),
        )

        async def chat(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover
            raise AssertionError("stream should be used")

        async def stream(self, request: LLMRequest):
            yield ReasoningDeltaEvent(text="private reasoning text")
            yield TextDeltaEvent(text="Public answer.")
            yield MessageEndEvent(
                finish_reason="stop",
                usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5),
            )

    provider = DeltaProvider()
    runtime = runtime_for_provider(
        provider,
        reasoning_visibility=visibility,
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Answer."))

    assert summary.status is TurnStatus.COMPLETED
    events = runtime.get_events(thread.thread_id).events
    event_types = [event.type for event in events]
    if visibility == "hidden":
        assert "model_reasoning_delta" not in event_types
    else:
        reasoning_deltas = [
            event.payload["text"]
            for event in events
            if event.type == "model_reasoning_delta"
        ]
        assert reasoning_deltas == ["private reasoning text"]
    # Activity feedback is phase-change-only and carries no reasoning text:
    # one "thinking", one "writing" (first text), one "idle" (response end).
    activity_phases = [
        event.payload["phase"]
        for event in events
        if event.type == "model_activity"
    ]
    assert activity_phases == ["thinking", "writing", "idle"]
    assert all(
        "text" not in event.payload for event in events if event.type == "model_activity"
    )
    # Provider continuity survives the public hiding: the next-turn canonical
    # history keeps the ReasoningBlock even under "hidden".
    turn2 = asyncio.run(runtime.run_turn(thread.thread_id, "Continue."))
    assert turn2.status is TurnStatus.COMPLETED
    if visibility == "hidden":
        assert "private reasoning text" not in json.dumps(
            runtime.get_snapshot(thread.thread_id).to_dict()
        )


def test_preflight_validation_is_async_and_cancellation_releases_lease(
    tmp_path,
    monkeypatch,
) -> None:
    async def scenario() -> tuple[list[str], list[dict[str, object]]]:
        started = threading.Event()
        release = threading.Event()
        provider = ScriptedProvider([final_response("Recovered.")])
        runtime = runtime_for_provider(provider)
        thread = runtime.create_thread(tmp_path)

        def blocking_validation(workspace: Path) -> None:
            started.set()
            release.wait(timeout=2)

        monkeypatch.setattr(
            runtime._workspace_validator,
            "validate",
            blocking_validation,
        )
        active = asyncio.create_task(runtime.run_turn(thread.thread_id, "Wait."))
        assert await asyncio.to_thread(started.wait, 1)
        await asyncio.sleep(0)
        before_cancel = runtime.get_snapshot(thread.thread_id)
        assert before_cancel.status is ThreadStatus.IDLE
        assert before_cancel.messages == []

        active.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await active

        rejected = runtime.get_events(thread.thread_id).events
        completed = await runtime.run_turn(thread.thread_id, "Try again.")
        assert completed.status is TurnStatus.COMPLETED
        return (
            [event.type for event in rejected],
            [event.payload for event in rejected],
        )

    event_types, payloads = asyncio.run(scenario())

    assert event_types == ["turn_rejected"]
    assert payloads == [
        {
            "error": {
                "code": "TURN_CANCELLED_BEFORE_START",
                "message": "Turn was cancelled before it started",
                "detail": None,
            }
        }
    ]


def test_token_estimation_allows_large_ascii_tool_result_that_safely_fits(tmp_path) -> None:
    provider = ScriptedProvider(
        [
            tool_response(
                ToolCallBlock(
                    id="large-result",
                    name="large_result",
                    arguments={},
                )
            ),
            final_response(),
        ]
    )
    provider.capabilities = ProviderCapabilities(context_window_tokens=4_000)

    def tools(_: Path) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="large_result",
                description="Return a deliberately large result",
                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            ),
            lambda _arguments: ToolResult(content="x" * 5_000, metadata={}),
        )
        return registry

    runtime = runtime_for_provider(provider, tool_registry_factory=tools)
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Use the tool."))

    assert summary.status is TurnStatus.COMPLETED
    assert summary.stop_reason == "completed"
    assert summary.error is None
    # The V1 estimator no longer treats each ASCII byte as a token, so this
    # result remains safely inside a 4k model window with output reserve.
    assert len(provider.requests) == 2
    assert len(runtime.get_snapshot(thread.thread_id).messages) == 4


def test_long_thread_reduces_context_and_persists_rolling_checkpoint(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("project rule sentinel", encoding="utf-8")

    class ReductionProvider(LLMProvider):
        capabilities = ProviderCapabilities(
            context_window_tokens=6_500,
            # Preserve this test's historical late-system request budget while
            # the provider-aware default is covered by context policy tests.
            working_tail_mode=WorkingTailMode.LATE_SYSTEM,
        )

        def __init__(self) -> None:
            self.model_requests: list[LLMRequest] = []
            self.compaction_requests: list[LLMRequest] = []

        async def chat(self, request: LLMRequest) -> LLMResponse:
            system_text = "".join(
                block.text
                for block in request.messages[0].content
                if isinstance(block, TextBlock)
            )
            if "semantic history compactor" in system_text:
                self.compaction_requests.append(request)
                return final_response(
                    "goal: long context task\n"
                    "completed work: inspected three outputs\n"
                    "validation: none\n"
                    "open work: finish"
                )
            self.model_requests.append(request)
            if len(self.model_requests) == 1:
                return LLMResponse(
                    message=Message(
                        role="assistant",
                        content=[
                            ToolCallBlock(
                                id=f"large-{index}",
                                name="large_result",
                                arguments={"value": str(index)},
                            )
                            for index in range(3)
                        ]
                        + [
                            ToolCallBlock(
                                id="plan",
                                name="update_plan",
                                arguments={
                                    "steps": [
                                        {
                                            "step": "finish long task",
                                            "status": "in_progress",
                                        }
                                    ]
                                },
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                    usage=Usage(),
                )
            if len(self.model_requests) == 2:
                return tool_response(
                    ToolCallBlock(id="small", name="small_result", arguments={})
                )
            return final_response("Long task complete.")

    def tools(_: Path) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="large_result",
                description="Return pressure-sized deterministic output",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            ),
            lambda arguments: ToolResult(
                content=str(arguments["value"]) * 5_000,
                metadata={"kind": "large"},
            ),
        )
        registry.register(
            ToolDefinition(
                name="small_result",
                description="Return a small current interaction",
                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            ),
            lambda _: ToolResult(content="current exact output", metadata={}),
        )
        return registry

    provider = ReductionProvider()
    store = InMemoryThreadStore()
    runtime = runtime_for_provider(
        provider,
        tool_registry_factory=tools,
        tool_policy=AllowAllPolicy(),
        store=store,
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "long context task"))

    assert summary.status is TurnStatus.COMPLETED
    assert len(provider.compaction_requests) == 1
    assert len(provider.model_requests) == 3
    final_request = provider.model_requests[-1]
    final_system = "".join(
        block.text
        for message in final_request.messages
        if message.role == "system"
        for block in message.content
        if isinstance(block, TextBlock)
    )
    assert "project rule sentinel" in final_system
    assert "runtime_context:" in final_system
    assert "compaction_summary:" in final_system
    assert "goal: long context task" in final_system
    final_working_tail = "\n".join(
        block.text
        for message in final_request.messages
        if message.role == "user"
        for block in message.content
        if isinstance(block, TextBlock)
    )
    assert "finish long task" in (final_system + final_working_tail)

    visible_results = [
        block
        for message in final_request.messages
        if message.role == "tool"
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    assert sum(block.metadata.get("pruned") is True for block in visible_results) == 3
    assert any(block.content == "current exact output" for block in visible_results)
    visible_calls = {
        block.id
        for message in final_request.messages
        if message.role == "assistant"
        for block in message.content
        if isinstance(block, ToolCallBlock)
    }
    assert {block.tool_call_id for block in visible_results} == visible_calls
    assert not any(
        message.role == "user"
        and any(
            isinstance(block, TextBlock) and block.text == "long context task"
            for block in message.content
        )
        for message in final_request.messages
    )

    snapshot = runtime.get_snapshot(thread.thread_id)
    canonical_large_results = [
        block
        for message in snapshot.messages
        if message["role"] == "tool"
        for block in message["content"]
        if block["tool_call_id"].startswith("large-")
    ]
    assert len(canonical_large_results) == 3
    assert all(len(block["content"]) == 5_000 for block in canonical_large_results)
    persisted = store.get_thread(thread.thread_id)
    assert persisted is not None
    assert persisted.checkpoint is not None
    assert persisted.checkpoint.summary.synthetic is True


def test_completed_idempotent_submission_returns_the_original_summary(tmp_path) -> None:
    provider = ScriptedProvider([final_response("Only once.")])
    runtime = runtime_for_provider(provider)
    thread = runtime.create_thread(tmp_path)

    first = asyncio.run(
        runtime.run_turn(
            thread.thread_id,
            "Do this once.",
            idempotency_key="request-1",
        )
    )
    duplicate = asyncio.run(
        runtime.run_turn(
            thread.thread_id,
            "Do this once.",
            idempotency_key="request-1",
        )
    )

    snapshot = runtime.get_snapshot(thread.thread_id)
    assert duplicate == first
    assert duplicate is not first
    assert len(provider.requests) == 1
    assert snapshot.completed_turns == 1
    assert len(snapshot.messages) == 2


def test_concurrent_idempotent_submission_joins_the_active_turn(tmp_path) -> None:
    async def scenario() -> tuple[TurnSummary, TurnSummary, int]:
        provider = PausingProvider()
        runtime = runtime_for_provider(provider)
        thread = runtime.create_thread(tmp_path)
        first = asyncio.create_task(
            runtime.run_turn(
                thread.thread_id,
                "Do this once.",
                idempotency_key="request-1",
            )
        )
        await provider.started.wait()
        duplicate = asyncio.create_task(
            runtime.run_turn(
                thread.thread_id,
                "Do this once.",
                idempotency_key="request-1",
            )
        )
        await asyncio.sleep(0)
        provider.release.set()
        first_summary, duplicate_summary = await asyncio.gather(first, duplicate)
        return (
            first_summary,
            duplicate_summary,
            runtime.get_snapshot(thread.thread_id).completed_turns,
        )

    first, duplicate, completed_turns = asyncio.run(scenario())

    assert duplicate == first
    assert duplicate.turn_id == first.turn_id
    assert completed_turns == 1


def test_reusing_an_idempotency_key_for_different_input_is_rejected(tmp_path) -> None:
    runtime = runtime_for_provider(ScriptedProvider([final_response()]))
    thread = runtime.create_thread(tmp_path)
    asyncio.run(
        runtime.run_turn(
            thread.thread_id,
            "Original input.",
            idempotency_key="request-1",
        )
    )

    with pytest.raises(IdempotencyConflictError) as captured:
        asyncio.run(
            runtime.run_turn(
                thread.thread_id,
                "Different input.",
                idempotency_key="request-1",
            )
        )

    assert captured.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("key", ["", "   ", "x" * 201, 1])
def test_invalid_idempotency_keys_fail_before_starting_a_turn(tmp_path, key) -> None:
    provider = ScriptedProvider([final_response()])
    runtime = runtime_for_provider(provider)
    thread = runtime.create_thread(tmp_path)

    with pytest.raises(ValueError, match="idempotency_key"):
        asyncio.run(
            runtime.run_turn(
                thread.thread_id,
                "Do work.",
                idempotency_key=key,
            )
        )

    assert provider.requests == []


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
                    metadata={
                        "tool": "missing",
                        "salient_evidence": {
                            "command": "",
                            "status": "UNKNOWN_TOOL",
                            "exit_code": None,
                            "lines": ["unknown tool: missing"],
                            "summary": (
                                "status=UNKNOWN_TOOL; diagnostics=unknown tool: missing"
                            ),
                            "validation_key": None,
                            "paths": [],
                            "tool": "missing",
                            "source_tool_call_id": "call_missing",
                        },
                    },
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


def test_truncated_model_response_without_tool_calls_stops_as_output_length(
    tmp_path,
) -> None:
    provider = ScriptedProvider(
        [
            LLMResponse(
                message=Message(
                    role="assistant",
                    content=[TextBlock(text="partial answer")],
                ),
                finish_reason="length",
                usage=Usage(output_tokens=65535),
            )
        ]
    )
    runtime = runtime_for_provider(provider)
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Write the maze."))

    assert summary.status is TurnStatus.LIMIT_REACHED
    assert summary.stop_reason == "output_length"
    assert summary.final_text == "partial answer"
    assert summary.error == {
        "code": "OUTPUT_TRUNCATED",
        "message": "model output was truncated before the response completed",
    }
    events = [event.type for event in runtime.get_events(thread.thread_id).events]
    assert events[-1] == "turn_limit_reached"


def test_empty_model_response_stops_as_empty_response(tmp_path) -> None:
    provider = ScriptedProvider(
        [
            LLMResponse(
                message=Message(role="assistant", content=[]),
                finish_reason="stop",
                usage=Usage(),
            )
        ]
    )
    runtime = runtime_for_provider(provider)
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Answer."))

    assert summary.status is TurnStatus.LIMIT_REACHED
    assert summary.stop_reason == "empty_response"
    assert summary.final_text == ""
    assert summary.error == {
        "code": "EMPTY_RESPONSE",
        "message": "model finished without any text or tool calls",
    }


def test_truncated_response_with_tool_calls_still_executes_tools(tmp_path) -> None:
    provider = ScriptedProvider(
        [
            LLMResponse(
                message=Message(
                    role="assistant",
                    content=[
                        ToolCallBlock(
                            id="call_mk",
                            name="write_file",
                            arguments={"path": "test.txt", "content": "123"},
                            raw_arguments='{"path": "test.txt", "content": "123"}',
                        )
                    ],
                ),
                finish_reason="length",
                usage=Usage(output_tokens=65535),
            ),
            final_response("Done."),
        ]
    )
    runtime = runtime_for_provider(
        provider, tool_registry_factory=create_test_tool_registry
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Write it."))

    assert summary.status is TurnStatus.COMPLETED
    assert (tmp_path / "test.txt").read_text(encoding="utf-8") == "123"
    assert summary.final_text == "Done."


def test_visible_reasoning_preview_is_bounded_on_model_response(tmp_path) -> None:
    long_reasoning = "r" * 5000
    provider = ScriptedProvider(
        [
            LLMResponse(
                message=Message(
                    role="assistant",
                    content=[
                        ReasoningBlock(text=long_reasoning),
                        TextBlock(text="Answer."),
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
        reasoning_visibility="visible",
    )
    thread = runtime.create_thread(tmp_path)

    asyncio.run(runtime.run_turn(thread.thread_id, "Explain."))
    events = runtime.get_events(thread.thread_id).events
    model_payload = next(
        event.payload for event in events if event.type == "model_response"
    )

    preview = model_payload["reasoning_preview"]
    assert preview["truncated"] is True
    assert preview["total_chars"] == 5000
    assert len(preview["text"]) == 3000


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


def test_provider_resolution_is_skipped_for_context_limit_and_workspace_busy(
    tmp_path,
) -> None:
    resolutions: list[tuple[str, str]] = []

    def resolve(provider_id: str, model: str) -> LLMProvider:
        resolutions.append((provider_id, model))
        raise AssertionError("provider transport must not be created")

    resolve.capabilities_for = lambda provider_id, model: ProviderCapabilities(  # type: ignore[attr-defined]
        context_window_tokens=1
    )
    context_runtime = ThreadRuntime(
        provider_resolver=resolve,
        default_settings=ModelSettings(
            provider_config_id="test-provider",
            model="test-model",
        ),
        tool_registry_factory=empty_tools,
    )
    context_thread = context_runtime.create_thread(tmp_path)

    with pytest.raises(ContextLimitError):
        asyncio.run(context_runtime.run_turn(context_thread.thread_id, "too large"))

    busy_runtime = ThreadRuntime(
        provider_resolver=resolve,
        default_settings=ModelSettings(
            provider_config_id="test-provider",
            model="test-model",
        ),
        tool_registry_factory=empty_tools,
    )
    busy_thread = busy_runtime.create_thread(tmp_path)
    held = busy_runtime._workspace_leases.acquire(tmp_path)
    try:
        with pytest.raises(WorkspaceBusyError):
            asyncio.run(busy_runtime.run_turn(busy_thread.thread_id, "blocked"))
    finally:
        busy_runtime._workspace_leases.release(held)

    assert resolutions == []


@pytest.mark.parametrize("model_error", [False, True])
def test_turn_releases_provider_adapter_after_success_or_model_error(
    tmp_path,
    model_error: bool,
) -> None:
    class CloseTrackingProvider(LLMProvider):
        def __init__(self) -> None:
            self.close_calls = 0

        async def chat(self, request: LLMRequest) -> LLMResponse:
            if model_error:
                raise LLMConnectionError("failed", retryable=False)
            return final_response("done")

        async def close(self) -> None:
            self.close_calls += 1

    provider = CloseTrackingProvider()
    runtime = runtime_for_provider(provider)
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "run"))

    assert summary.status is (TurnStatus.FAILED if model_error else TurnStatus.COMPLETED)
    assert provider.close_calls == 1


def test_many_turns_reuse_one_production_transport(monkeypatch, tmp_path) -> None:
    class Client:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(
                completions=RecordingCompletions(
                    [sdk_response("done") for _ in range(20)]
                )
            )
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    clients: list[Client] = []

    def create_client(config: ProviderConfig) -> Client:
        del config
        client = Client()
        clients.append(client)
        return client

    monkeypatch.setattr(
        OpenAICompatibleProvider,
        "_create_client",
        staticmethod(create_client),
    )
    pool = OpenAICompatibleClientPool()

    def resolve(provider_id: str, model: str) -> LLMProvider:
        return pool.resolve(
            ProviderConfig(
                provider=provider_id,
                base_url="https://example.invalid/v1",
                api_key="test-key",
                model=model,
            )
        )

    resolve.capabilities_for = lambda provider_id, model: ProviderCapabilities(  # type: ignore[attr-defined]
        context_window_tokens=32_000
    )
    runtime = ThreadRuntime(
        provider_resolver=resolve,
        default_settings=ModelSettings(
            provider_config_id="deepseek",
            model="model-a",
        ),
        tool_registry_factory=empty_tools,
    )
    thread = runtime.create_thread(tmp_path)

    async def scenario() -> list[TurnSummary]:
        summaries = [
            await runtime.run_turn(thread.thread_id, f"turn {index}")
            for index in range(20)
        ]
        assert pool.client_count == 1
        await runtime.aclose()
        await pool.aclose()
        return summaries

    summaries = asyncio.run(scenario())

    assert all(summary.status is TurnStatus.COMPLETED for summary in summaries)
    assert len(clients) == 1
    assert clients[0].close_calls == 1


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


def test_cancelling_model_call_releases_turn_provider_adapter(tmp_path) -> None:
    class CloseTrackingProvider(PausingProvider):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    async def scenario() -> tuple[TurnSummary, CloseTrackingProvider]:
        provider = CloseTrackingProvider()
        runtime = runtime_for_provider(provider)
        thread = runtime.create_thread(tmp_path)
        active = asyncio.create_task(runtime.run_turn(thread.thread_id, "cancel"))
        await provider.started.wait()
        assert runtime.cancel_turn(thread.thread_id)
        return await active, provider

    summary, provider = asyncio.run(scenario())

    assert summary.status is TurnStatus.CANCELLED
    assert provider.close_calls == 1


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
            tool_policy=AllowAllPolicy(),
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


def test_cancelling_turn_during_next_model_call_reaps_its_persistent_session(
    tmp_path,
) -> None:
    class ProcessThenPauseProvider(LLMProvider):
        def __init__(self) -> None:
            self.calls = 0
            self.second_call_started = asyncio.Event()

        async def chat(self, request: LLMRequest) -> LLMResponse:
            self.calls += 1
            if self.calls == 1:
                return tool_response(
                    ToolCallBlock(
                        id="persistent-command",
                        name="exec_command",
                        arguments={
                            "command": (
                                "printf '%s' $$ > persistent.pid; read line"
                            ),
                            "yield_time_ms": 0,
                            "timeout_ms": 60_000,
                        },
                    )
                )
            self.second_call_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def scenario():
        provider = ProcessThenPauseProvider()
        runtime = runtime_for_provider(
            provider,
            tool_registry_factory=create_test_tool_registry,
            tool_policy=AllowAllPolicy(),
        )
        thread = runtime.create_thread(tmp_path)
        active = asyncio.create_task(runtime.run_turn(thread.thread_id, "Run."))
        await asyncio.wait_for(provider.second_call_started.wait(), timeout=2)
        pid_file = tmp_path / "persistent.pid"
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        assert pid_file.exists()
        process_id = int(pid_file.read_text(encoding="utf-8"))

        assert runtime.cancel_turn(thread.thread_id) is True
        summary = await active
        process_alive = True
        for _ in range(100):
            try:
                os.kill(process_id, 0)
            except ProcessLookupError:
                process_alive = False
                break
            await asyncio.sleep(0.01)
        await runtime.aclose()
        return summary, process_alive

    summary, process_alive = asyncio.run(scenario())

    assert summary.status is TurnStatus.CANCELLED
    assert process_alive is False


def test_cancelling_a_sync_file_tool_keeps_lease_until_changes_are_tracked(
    tmp_path,
) -> None:
    async def scenario() -> tuple[TurnSummary, TurnSummary]:
        started = threading.Event()
        release = threading.Event()
        target = tmp_path / "result.txt"

        def slow_write(arguments: dict[str, object]) -> ToolResult:
            started.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test did not release slow write")
            content = str(arguments["content"])
            target.write_text(content, encoding="utf-8")
            return ToolResult(
                content="write completed",
                metadata={"path": "result.txt", "bytes_written": len(content)},
            )

        def tools(_: Path) -> ToolRegistry:
            registry = ToolRegistry()
            registry.register(
                ToolDefinition(
                    name="write_file",
                    description="Write one file slowly",
                    parameters={
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                        "additionalProperties": False,
                    },
                ),
                slow_write,
            )
            return registry

        provider = ScriptedProvider(
            [
                tool_response(
                    ToolCallBlock(
                        id="slow-write",
                        name="write_file",
                        arguments={"path": "result.txt", "content": "done\n"},
                    )
                ),
                final_response("Next turn completed."),
            ]
        )
        runtime = runtime_for_provider(provider, tool_registry_factory=tools)
        first = runtime.create_thread(tmp_path)
        second = runtime.create_thread(tmp_path)
        active = asyncio.create_task(runtime.run_turn(first.thread_id, "Write slowly."))
        while not started.is_set():
            await asyncio.sleep(0.005)
        assert runtime.cancel_turn(first.thread_id)
        try:
            await asyncio.sleep(0)
            assert not active.done()
            with pytest.raises(WorkspaceBusyError):
                await runtime.run_turn(second.thread_id, "Must remain blocked.")
        finally:
            release.set()
        cancelled = await active
        next_turn = await runtime.run_turn(second.thread_id, "Now continue.")
        return cancelled, next_turn

    cancelled, next_turn = asyncio.run(scenario())

    assert cancelled.status is TurnStatus.CANCELLED
    assert cancelled.modified_files == ["result.txt"]
    assert cancelled.diff_complete is True
    assert "+done" in cancelled.file_diffs[0]["diff"]
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "done\n"
    assert next_turn.status is TurnStatus.COMPLETED


def test_sync_file_tool_timeout_waits_for_quiescence_and_tracks_final_change(
    tmp_path,
) -> None:
    async def scenario() -> tuple[TurnSummary, TurnSummary]:
        started = threading.Event()
        release = threading.Event()
        target = tmp_path / "timed.txt"

        def slow_write(arguments: dict[str, object]) -> ToolResult:
            started.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test did not release timed write")
            content = str(arguments["content"])
            target.write_text(content, encoding="utf-8")
            return ToolResult(
                content="write completed",
                metadata={"path": "timed.txt", "bytes_written": len(content)},
            )

        def tools(_: Path) -> ToolRegistry:
            registry = ToolRegistry()
            registry.register(
                ToolDefinition(
                    name="write_file",
                    description="Write one file after the deadline",
                    parameters={
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                        "additionalProperties": False,
                    },
                ),
                slow_write,
            )
            return registry

        provider = ScriptedProvider(
            [
                tool_response(
                    ToolCallBlock(
                        id="timed-write",
                        name="write_file",
                        arguments={"path": "timed.txt", "content": "late\n"},
                    )
                ),
                final_response("Next turn completed."),
            ]
        )
        runtime = runtime_for_provider(
            provider,
            tool_registry_factory=tools,
            default_settings=ModelSettings(
                provider_config_id="test-provider",
                model="test-model",
                limits=AgentLimits(max_execution_seconds=0.02),
            ),
        )
        first = runtime.create_thread(tmp_path)
        second = runtime.create_thread(tmp_path)
        active = asyncio.create_task(runtime.run_turn(first.thread_id, "Write late."))
        while not started.is_set():
            await asyncio.sleep(0.005)
        try:
            await asyncio.sleep(0.04)
            assert not active.done()
            with pytest.raises(WorkspaceBusyError):
                await runtime.run_turn(second.thread_id, "Must remain blocked.")
        finally:
            release.set()
        limited = await active
        next_turn = await runtime.run_turn(
            second.thread_id,
            "Now continue.",
            settings_override=TurnSettingsOverride(
                limits=AgentLimits(max_execution_seconds=1),
            ),
        )
        return limited, next_turn

    limited, next_turn = asyncio.run(scenario())

    assert limited.status is TurnStatus.LIMIT_REACHED
    assert limited.stop_reason == "execution_timeout"
    assert limited.modified_files == ["timed.txt"]
    assert limited.diff_complete is True
    assert "+late" in limited.file_diffs[0]["diff"]
    assert (tmp_path / "timed.txt").read_text(encoding="utf-8") == "late\n"
    assert next_turn.status is TurnStatus.COMPLETED


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


def test_global_active_turn_limit_defaults_to_four(tmp_path) -> None:
    async def scenario() -> None:
        provider = ConcurrentProvider()
        runtime = runtime_for_provider(provider)
        threads = []
        for index in range(5):
            workspace = tmp_path / f"workspace-{index}"
            workspace.mkdir()
            threads.append(runtime.create_thread(workspace))

        active = [
            asyncio.create_task(runtime.run_turn(thread.thread_id, "Hold capacity."))
            for thread in threads[:4]
        ]
        await asyncio.wait_for(provider.wait_for_started(4), timeout=0.5)
        try:
            with pytest.raises(WorkspaceBusyError, match="capacity") as captured:
                await runtime.run_turn(threads[4].thread_id, "Default is full.")
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


class FixedPolicy:
    def __init__(self, decision: PolicyDecision) -> None:
        self.decision = decision
        self.calls: list[ToolCallBlock] = []

    def decide(self, call: ToolCallBlock) -> PolicyDecision:
        self.calls.append(call)
        return self.decision


def recording_registry(executions: list[str]) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="record",
            description="Record a value.",
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
    return registry


def test_default_policy_allows_valid_tools(tmp_path) -> None:
    executions: list[str] = []
    provider = ScriptedProvider(
        [
            tool_response(
                ToolCallBlock(
                    id="allowed", name="record", arguments={"value": "yes"}
                )
            ),
            final_response("Allowed by default."),
        ]
    )
    runtime = runtime_for_provider(
        provider,
        tool_registry_factory=lambda _: recording_registry(executions),
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Use the tool."))

    assert executions == ["yes"]
    assert summary.status is TurnStatus.COMPLETED


def test_policy_denial_returns_structured_result_and_continues(tmp_path) -> None:
    executions: list[str] = []
    policy = FixedPolicy(PolicyDecision.DENY)
    provider = ScriptedProvider(
        [
            tool_response(
                ToolCallBlock(
                    id="denied", name="record", arguments={"value": "never"}
                )
            ),
            final_response("Adapted after denial."),
        ]
    )
    runtime = runtime_for_provider(
        provider,
        tool_registry_factory=lambda _: recording_registry(executions),
        tool_policy=policy,
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Respect policy."))

    assert executions == []
    assert len(policy.calls) == 1
    assert summary.status is TurnStatus.COMPLETED
    denied_result = provider.requests[1].messages[-1].content[0]
    assert isinstance(denied_result, ToolResultBlock)
    assert denied_result.error_code == "POLICY_DENIED"


def test_policy_failure_preserves_complete_tool_history_for_the_next_turn(
    tmp_path,
) -> None:
    class BrokenPolicy:
        def decide(self, call: ToolCallBlock) -> PolicyDecision:
            raise RuntimeError("private policy failure")

    provider = ScriptedProvider(
        [
            tool_response(
                ToolCallBlock(id="broken-first", name="missing", arguments={}),
                ToolCallBlock(id="broken-second", name="missing", arguments={}),
            ),
            final_response("A later Turn still works."),
        ]
    )
    runtime = runtime_for_provider(provider, tool_policy=BrokenPolicy())
    thread = runtime.create_thread(tmp_path)

    failed = asyncio.run(runtime.run_turn(thread.thread_id, "Policy fails."))
    recovered = asyncio.run(runtime.run_turn(thread.thread_id, "Continue safely."))

    assert failed.status is TurnStatus.FAILED
    assert recovered.status is TurnStatus.COMPLETED
    tool_results = [
        message.content[0]
        for message in provider.requests[1].messages
        if message.role == "tool"
    ]
    assert [result.tool_call_id for result in tool_results] == [
        "broken-first",
        "broken-second",
    ]
    assert all(result.error_code == "INTERNAL_ERROR" for result in tool_results)
    assert "private policy failure" not in json.dumps(
        runtime.get_snapshot(thread.thread_id).to_dict()
    )


@pytest.mark.parametrize("approved", [True, False])
def test_external_approval_resumes_with_execution_or_policy_denial(
    tmp_path,
    approved,
) -> None:
    async def scenario():
        executions: list[str] = []
        provider = ScriptedProvider(
            [
                tool_response(
                    ToolCallBlock(
                        id="approval-call",
                        name="record",
                        arguments={"value": "approved"},
                    )
                ),
                final_response("Approval resolved."),
            ]
        )
        runtime = runtime_for_provider(
            provider,
            tool_registry_factory=lambda _: recording_registry(executions),
            tool_policy=FixedPolicy(PolicyDecision.REQUIRE_APPROVAL),
        )
        thread = runtime.create_thread(tmp_path)
        active = asyncio.create_task(runtime.run_turn(thread.thread_id, "Ask first."))
        for _ in range(100):
            if runtime.get_snapshot(thread.thread_id).status is ThreadStatus.WAITING_APPROVAL:
                break
            await asyncio.sleep(0.005)
        snapshot = runtime.get_snapshot(thread.thread_id)
        assert snapshot.status is ThreadStatus.WAITING_APPROVAL
        assert snapshot.pending_approval is not None
        assert snapshot.pending_approval["approval_id"]
        assert snapshot.pending_approval["tool_call"]["id"] == "approval-call"
        assert snapshot.pending_approval["reason_code"]
        assert snapshot.pending_approval["execution_profile"] == "workspace_write"
        approval_event = next(
            event
            for event in runtime.get_events(thread.thread_id).events
            if event.type == "approval_requested"
        )
        approval_id = approval_event.payload["approval_id"]
        assert runtime.resolve_approval(
            thread.thread_id,
            approval_id=approval_id,
            approved=approved,
        ) is True
        summary = await active
        return runtime, thread.thread_id, summary, executions, provider

    runtime, thread_id, summary, executions, provider = asyncio.run(scenario())

    assert summary.status is TurnStatus.COMPLETED
    assert executions == (["approved"] if approved else [])
    result = provider.requests[1].messages[-1].content[0]
    assert isinstance(result, ToolResultBlock)
    assert result.error_code is (None if approved else "POLICY_DENIED")
    event_types = [event.type for event in runtime.get_events(thread_id).events]
    assert event_types.index("approval_requested") < event_types.index(
        "approval_resolved"
    )


def test_approval_wait_pauses_later_tools_and_execution_deadline(tmp_path) -> None:
    async def scenario():
        executions: list[str] = []
        provider = ScriptedProvider(
            [
                tool_response(
                    ToolCallBlock(
                        id="first-approved",
                        name="record",
                        arguments={"value": "first"},
                    ),
                    ToolCallBlock(
                        id="second-approved",
                        name="record",
                        arguments={"value": "second"},
                    ),
                ),
                final_response("Both completed."),
            ]
        )
        runtime = runtime_for_provider(
            provider,
            tool_registry_factory=lambda _: recording_registry(executions),
            tool_policy=FixedPolicy(PolicyDecision.REQUIRE_APPROVAL),
            approval_timeout_seconds=1,
            default_settings=ModelSettings(
                provider_config_id="test-provider",
                model="test-model",
                limits=AgentLimits(max_execution_seconds=0.03),
            ),
        )
        thread = runtime.create_thread(tmp_path)
        active = asyncio.create_task(runtime.run_turn(thread.thread_id, "Approve both."))
        approval_ids: list[str] = []
        for expected_count in (1, 2):
            for _ in range(100):
                requested = [
                    event
                    for event in runtime.get_events(thread.thread_id).events
                    if event.type == "approval_requested"
                ]
                if len(requested) >= expected_count:
                    break
                await asyncio.sleep(0.005)
            assert executions == (["first"] if expected_count == 2 else [])
            approval_id = requested[-1].payload["approval_id"]
            approval_ids.append(approval_id)
            await asyncio.sleep(0.04)
            assert runtime.resolve_approval(
                thread.thread_id,
                approval_id=approval_id,
                approved=True,
            )
        return await active, executions, approval_ids

    summary, executions, approval_ids = asyncio.run(scenario())

    assert summary.status is TurnStatus.COMPLETED
    assert executions == ["first", "second"]
    assert len(set(approval_ids)) == 2


def test_approval_timeout_fails_turn_with_safe_reason(tmp_path) -> None:
    provider = ScriptedProvider(
        [
            tool_response(
                ToolCallBlock(id="timeout", name="missing", arguments={})
            )
        ]
    )
    runtime = runtime_for_provider(
        provider,
        tool_policy=FixedPolicy(PolicyDecision.REQUIRE_APPROVAL),
        approval_timeout_seconds=0.02,
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Let approval expire."))

    assert summary.status is TurnStatus.FAILED
    assert summary.stop_reason == "approval_timeout"
    assert summary.error == {
        "code": "APPROVAL_TIMEOUT",
        "message": "tool approval timed out",
    }


def test_cancelling_approval_wait_cancels_turn_and_releases_workspace(
    tmp_path,
) -> None:
    async def scenario():
        provider = ScriptedProvider(
            [
                tool_response(
                    ToolCallBlock(id="cancel-approval", name="missing", arguments={})
                ),
                final_response("New turn."),
            ]
        )
        runtime = runtime_for_provider(
            provider,
            tool_policy=FixedPolicy(PolicyDecision.REQUIRE_APPROVAL),
        )
        first = runtime.create_thread(tmp_path)
        second = runtime.create_thread(tmp_path)
        active = asyncio.create_task(runtime.run_turn(first.thread_id, "Wait."))
        for _ in range(100):
            if runtime.get_snapshot(first.thread_id).status is ThreadStatus.WAITING_APPROVAL:
                break
            await asyncio.sleep(0.005)
        assert runtime.cancel_turn(first.thread_id)
        cancelled = await active
        next_turn = await runtime.run_turn(second.thread_id, "Lease was released.")
        return runtime, first.thread_id, cancelled, next_turn

    runtime, thread_id, cancelled, next_turn = asyncio.run(scenario())

    assert cancelled.status is TurnStatus.CANCELLED
    assert next_turn.status is TurnStatus.COMPLETED
    assert runtime.resolve_approval(
        thread_id,
        approval_id="stale",
        approved=True,
    ) is False


@pytest.mark.parametrize("approval_timeout_seconds", [0, True, 3601])
def test_approval_timeout_configuration_fails_closed(
    approval_timeout_seconds,
) -> None:
    with pytest.raises(ValueError, match="approval_timeout_seconds"):
        ThreadRuntime(
            provider_resolver=lambda _config_id, _model: ScriptedProvider([]),
            default_settings=ModelSettings(
                provider_config_id="test-provider", model="test-model"
            ),
            tool_registry_factory=empty_tools,
            approval_timeout_seconds=approval_timeout_seconds,
        )


def test_repeated_file_tool_edits_produce_one_original_to_final_diff(
    tmp_path,
) -> None:
    source = tmp_path / "app.py"
    source.write_text("value = 'old'\n", encoding="utf-8")
    provider = ScriptedProvider(
        [
            tool_response(
                ToolCallBlock(
                    id="write-middle",
                    name="write_file",
                    arguments={"path": "app.py", "content": "value = 'middle'\n"},
                )
            ),
            tool_response(
                ToolCallBlock(
                    id="edit-final",
                    name="edit_file",
                    arguments={
                        "path": "app.py",
                        "old_string": "'middle'",
                        "new_string": "'final'",
                    },
                )
            ),
            final_response("File updated."),
        ]
    )
    runtime = runtime_for_provider(
        provider,
        tool_registry_factory=create_test_tool_registry,
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Update app.py."))

    assert source.read_text(encoding="utf-8") == "value = 'final'\n"
    assert summary.modified_files == ["app.py"]
    assert summary.diff_complete is True
    assert summary.file_diffs == [
        {
            "path": "app.py",
            "change_type": "modified",
            "diff": (
                "--- a/app.py\n"
                "+++ b/app.py\n"
                    "@@ -1 +1 @@\n"
                    "-value = 'old'\n"
                    "+value = 'final'\n"
                ),
        }
    ]
    changed_events = [
        event
        for event in runtime.get_events(thread.thread_id).events
        if event.type == "file_changed"
    ]
    assert len(changed_events) == 2
    assert changed_events[-1].payload == summary.file_diffs[0]


def test_new_file_is_reported_against_dev_null(tmp_path) -> None:
    provider = ScriptedProvider(
        [
            tool_response(
                ToolCallBlock(
                    id="new-file",
                    name="write_file",
                    arguments={"path": "notes.txt", "content": "hello\n"},
                )
            ),
            final_response("Created."),
        ]
    )
    runtime = runtime_for_provider(
        provider,
        tool_registry_factory=create_test_tool_registry,
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Create notes."))

    assert summary.modified_files == ["notes.txt"]
    assert summary.file_diffs[0]["change_type"] == "added"
    assert summary.file_diffs[0]["diff"].startswith("--- /dev/null\n+++ b/notes.txt\n")
    assert "+hello" in summary.file_diffs[0]["diff"]


def test_external_change_returns_file_changed_until_model_rereads(tmp_path) -> None:
    source = tmp_path / "shared.txt"
    source.write_text("original\n", encoding="utf-8")

    class ConflictRecoveryProvider(LLMProvider):
        def __init__(self) -> None:
            self.requests: list[LLMRequest] = []

        async def chat(self, request: LLMRequest) -> LLMResponse:
            self.requests.append(request)
            call_number = len(self.requests)
            if call_number == 1:
                return tool_response(
                    ToolCallBlock(
                        id="initial-read",
                        name="read_file",
                        arguments={"path": "shared.txt"},
                    )
                )
            if call_number == 2:
                source.write_text("external\n", encoding="utf-8")
                return tool_response(
                    ToolCallBlock(
                        id="stale-write",
                        name="write_file",
                        arguments={"path": "shared.txt", "content": "stale\n"},
                    )
                )
            if call_number == 3:
                stale_result = request.messages[-1].content[0]
                assert isinstance(stale_result, ToolResultBlock)
                assert stale_result.error_code == "FILE_CHANGED"
                assert source.read_text(encoding="utf-8") == "external\n"
                return tool_response(
                    ToolCallBlock(
                        id="fresh-read",
                        name="read_file",
                        arguments={"path": "shared.txt"},
                    )
                )
            if call_number == 4:
                fresh_result = request.messages[-1].content[0]
                assert isinstance(fresh_result, ToolResultBlock)
                assert "external" in fresh_result.content
                return tool_response(
                    ToolCallBlock(
                        id="fresh-write",
                        name="write_file",
                        arguments={"path": "shared.txt", "content": "resolved\n"},
                    )
                )
            return final_response("Conflict resolved.")

    provider = ConflictRecoveryProvider()
    runtime = runtime_for_provider(
        provider,
        tool_registry_factory=create_test_tool_registry,
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Update shared.txt."))

    assert summary.status is TurnStatus.COMPLETED
    assert source.read_text(encoding="utf-8") == "resolved\n"
    assert summary.modified_files == ["shared.txt"]
    assert "-external" in summary.file_diffs[0]["diff"]
    assert "+resolved" in summary.file_diffs[0]["diff"]


def test_run_command_marks_diff_incomplete_without_guessing_changed_files(
    tmp_path,
) -> None:
    provider = ScriptedProvider(
        [
            tool_response(
                ToolCallBlock(
                    id="command-change",
                    name="run_command",
                    arguments={"command": "printf command > generated.txt"},
                )
            ),
            final_response("Command complete."),
        ]
    )
    runtime = runtime_for_provider(
        provider,
        tool_registry_factory=create_test_tool_registry,
        tool_policy=AllowAllPolicy(),
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Use a command."))

    assert (tmp_path / "generated.txt").read_text(encoding="utf-8") == "command"
    assert summary.modified_files == []
    assert summary.file_diffs == []
    assert summary.diff_complete is False


def test_policy_denied_command_does_not_make_diff_incomplete(tmp_path) -> None:
    provider = ScriptedProvider(
        [
            tool_response(
                ToolCallBlock(
                    id="denied-command",
                    name="run_command",
                    arguments={"command": "printf denied > denied.txt"},
                )
            ),
            final_response("Denied safely."),
        ]
    )
    runtime = runtime_for_provider(
        provider,
        tool_registry_factory=create_test_tool_registry,
        tool_policy=FixedPolicy(PolicyDecision.DENY),
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Do not execute."))

    assert not (tmp_path / "denied.txt").exists()
    assert summary.diff_complete is True


def test_unified_diff_preserves_a_removed_final_newline(tmp_path) -> None:
    source = tmp_path / "newline.txt"
    source.write_text("value\n", encoding="utf-8")
    provider = ScriptedProvider(
        [
            tool_response(
                ToolCallBlock(
                    id="remove-newline",
                    name="write_file",
                    arguments={"path": "newline.txt", "content": "value"},
                )
            ),
            final_response("Newline removed."),
        ]
    )
    runtime = runtime_for_provider(
        provider,
        tool_registry_factory=create_test_tool_registry,
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Remove newline."))

    assert summary.file_diffs[0]["diff"]
    assert "-value\n+value" in summary.file_diffs[0]["diff"]


def test_reverting_to_original_emits_a_final_reverted_event(tmp_path) -> None:
    source = tmp_path / "revert.txt"
    source.write_text("original\n", encoding="utf-8")
    provider = ScriptedProvider(
        [
            tool_response(
                ToolCallBlock(
                    id="change",
                    name="write_file",
                    arguments={"path": "revert.txt", "content": "changed\n"},
                )
            ),
            tool_response(
                ToolCallBlock(
                    id="revert",
                    name="write_file",
                    arguments={"path": "revert.txt", "content": "original\n"},
                )
            ),
            final_response("Reverted."),
        ]
    )
    runtime = runtime_for_provider(
        provider,
        tool_registry_factory=create_test_tool_registry,
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Change then revert."))

    assert summary.modified_files == []
    changed_events = [
        event.payload
        for event in runtime.get_events(thread.thread_id).events
        if event.type == "file_changed"
    ]
    assert changed_events[-1] == {
        "path": "revert.txt",
        "change_type": "reverted",
        "diff": "",
    }


def test_invalid_command_does_not_make_diff_incomplete(tmp_path) -> None:
    provider = ScriptedProvider(
        [
            tool_response(
                ToolCallBlock(
                    id="invalid-command",
                    name="run_command",
                    arguments={"command": ""},
                )
            ),
            final_response("Invalid command handled."),
        ]
    )
    runtime = runtime_for_provider(
        provider,
        tool_registry_factory=create_test_tool_registry,
        tool_policy=AllowAllPolicy(),
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Invalid command."))

    assert provider.requests[1].messages[-1].content[0].error_code == "INVALID_ARGUMENTS"
    assert summary.diff_complete is True


def test_read_fingerprint_matches_the_exact_content_returned_to_model(
    tmp_path,
) -> None:
    source = tmp_path / "raced.txt"
    source.write_text("model-saw-this\n", encoding="utf-8")

    def raced_read(arguments: dict[str, object]) -> ToolResult:
        source.write_text("external-version\n", encoding="utf-8")
        return ToolResult(
            content="1: model-saw-this",
            metadata={
                "path": "raced.txt",
                "content_fingerprint": content_fingerprint(b"model-saw-this\n"),
            },
        )

    def raced_write(arguments: dict[str, object]) -> ToolResult:
        source.write_text(str(arguments["content"]), encoding="utf-8")
        return ToolResult(
            content="wrote raced.txt",
            metadata={"path": "raced.txt"},
        )

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="read_file",
            description="Race-aware read fake.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
        raced_read,
    )
    registry.register(
        ToolDefinition(
            name="write_file",
            description="Write fake.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        ),
        raced_write,
    )
    provider = ScriptedProvider(
        [
            tool_response(
                ToolCallBlock(
                    id="raced-read",
                    name="read_file",
                    arguments={"path": "raced.txt"},
                )
            ),
            tool_response(
                ToolCallBlock(
                    id="raced-write",
                    name="write_file",
                    arguments={"path": "raced.txt", "content": "overwrite\n"},
                )
            ),
            final_response("Conflict observed."),
        ]
    )
    runtime = runtime_for_provider(
        provider,
        tool_registry_factory=lambda _: registry,
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Avoid overwrite."))

    assert summary.status is TurnStatus.COMPLETED
    assert source.read_text(encoding="utf-8") == "external-version\n"
    conflict = provider.requests[2].messages[-1].content[0]
    assert isinstance(conflict, ToolResultBlock)
    assert conflict.error_code == "FILE_CHANGED"


def test_workspace_root_symlink_is_canonicalized_before_tool_composition(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    runtime = runtime_for_provider(ScriptedProvider([]))

    thread = runtime.create_thread(linked)
    assert thread.workspace == str(real.resolve())


def test_workspace_symlink_parent_is_canonicalized_before_tool_composition(tmp_path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    workspace = real_parent / "workspace"
    workspace.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    runtime = runtime_for_provider(ScriptedProvider([]))

    thread = runtime.create_thread(linked_parent / "workspace")
    assert thread.workspace == str(workspace.resolve())


@pytest.mark.parametrize("link_kind", ["symbolic", "hard"])
def test_turn_allows_persistent_workspace_links(tmp_path, link_kind) -> None:
    target = tmp_path / "target.txt"
    target.write_text("data", encoding="utf-8")
    if link_kind == "symbolic":
        (tmp_path / "linked.txt").symlink_to(target)
    else:
        os.link(target, tmp_path / "linked.txt")
    runtime = runtime_for_provider(ScriptedProvider([final_response()]))
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Validate first."))

    assert summary.status is TurnStatus.COMPLETED
    assert runtime.get_snapshot(thread.thread_id).status is ThreadStatus.IDLE


def test_nested_mount_point_is_not_scanned_at_runtime_seam(tmp_path, monkeypatch) -> None:
    nested = tmp_path / "mounted"
    nested.mkdir()
    del nested
    monkeypatch.setattr(
        "agent.runtime.workspace_validator.mounted_paths",
        lambda: (_ for _ in ()).throw(AssertionError("mount table must not be scanned")),
    )
    runtime = runtime_for_provider(ScriptedProvider([final_response()]))
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Do not scan mounts."))
    assert summary.status is TurnStatus.COMPLETED


def test_workspace_entry_budget_does_not_trigger_recursive_preflight(tmp_path) -> None:
    (tmp_path / "one.txt").write_text("1", encoding="utf-8")
    (tmp_path / "two.txt").write_text("2", encoding="utf-8")
    runtime = runtime_for_provider(
        ScriptedProvider([final_response()]),
        workspace_validation_max_entries=1,
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Bound validation."))
    assert summary.status is TurnStatus.COMPLETED


def test_workspace_time_budget_does_not_trigger_recursive_preflight(tmp_path) -> None:
    (tmp_path / "one.txt").write_text("1", encoding="utf-8")
    clock_values = iter((0.0, 0.0, 2.0))
    runtime = runtime_for_provider(
        ScriptedProvider([final_response()]),
        workspace_validation_max_seconds=1,
        workspace_validation_clock=lambda: next(clock_values),
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Time-box validation."))
    assert summary.status is TurnStatus.COMPLETED


@pytest.mark.parametrize("link_kind", ["symbolic", "hard"])
def test_file_tools_authorize_links_created_at_access_time(
    tmp_path,
    link_kind,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("secret\n", encoding="utf-8")

    class LinkCreatingProvider(LLMProvider):
        def __init__(self) -> None:
            self.requests: list[LLMRequest] = []

        async def chat(self, request: LLMRequest) -> LLMResponse:
            self.requests.append(request)
            if len(self.requests) == 1:
                linked = tmp_path / "late-link.txt"
                if link_kind == "symbolic":
                    linked.symlink_to(source)
                else:
                    os.link(source, linked)
                return tool_response(
                    ToolCallBlock(
                        id="late-link",
                        name="read_file",
                        arguments={"path": "late-link.txt"},
                    )
                )
            return final_response("Link rejected.")

    provider = LinkCreatingProvider()
    runtime = runtime_for_provider(
        provider,
        tool_registry_factory=create_test_tool_registry,
    )
    thread = runtime.create_thread(tmp_path)

    summary = asyncio.run(runtime.run_turn(thread.thread_id, "Do not follow links."))

    assert summary.status is TurnStatus.COMPLETED
    result = provider.requests[1].messages[-1].content[0]
    assert isinstance(result, ToolResultBlock)
    assert result.error_code is None
    assert result.content == "1: secret"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("workspace_validation_max_entries", 0),
        ("workspace_validation_max_entries", True),
        ("workspace_validation_max_seconds", 0),
        ("workspace_validation_max_seconds", True),
    ],
)
def test_workspace_validation_configuration_fails_closed(name, value) -> None:
    with pytest.raises(ValueError, match=name):
        ThreadRuntime(
            provider_resolver=lambda _config_id, _model: ScriptedProvider([]),
            default_settings=ModelSettings(
                provider_config_id="test-provider", model="test-model"
            ),
            tool_registry_factory=empty_tools,
            **{name: value},
        )
