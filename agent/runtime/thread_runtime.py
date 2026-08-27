"""In-memory Thread runtime and the minimal readable ReAct loop."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from agent.model.provider import LLMProvider
from agent.tools.registry import ToolRegistry

from .conversation import Conversation
from .errors import SettingsConflictError, ThreadBusyError
from .events import EventBatch, EventBuffer, TurnEventEmitter, utc_now
from .loop import AgentLoop
from .model_invoker import ModelInvoker
from .prompt import PromptBuilder
from .settings import (
    ModelSettings,
    ThreadSettings,
    TurnConfig,
    TurnSettingsOverride,
)
from .types import SCHEMA_VERSION, ThreadSnapshot, ThreadStatus, TurnStatus, TurnSummary


ToolRegistryFactory = Callable[[Path], ToolRegistry]
ProviderResolver = Callable[[str, str], LLMProvider]


@dataclass(slots=True)
class _ThreadRecord:
    thread_id: str
    workspace: Path
    tools: ToolRegistry
    conversation: Conversation
    settings: ThreadSettings
    status: ThreadStatus = ThreadStatus.IDLE
    active_turn_id: str | None = None
    completed_turns: int = 0
    created_at: str = ""
    updated_at: str = ""
    latest_turn: TurnSummary | None = None
    events: EventBuffer | None = None


class ThreadRuntime:
    """Create in-memory Threads and run one ReAct Turn at a time."""

    def __init__(
        self,
        *,
        tool_registry_factory: ToolRegistryFactory,
        provider_resolver: ProviderResolver,
        default_settings: ModelSettings,
        additional_system_instructions: str | None = None,
        prompt_builder: PromptBuilder | None = None,
        event_buffer_capacity: int = 512,
        reasoning_visibility: str = "hidden",
    ) -> None:
        if reasoning_visibility not in {"hidden", "debug"}:
            raise ValueError("reasoning_visibility must be 'hidden' or 'debug'")
        if (
            isinstance(event_buffer_capacity, bool)
            or not isinstance(event_buffer_capacity, int)
            or event_buffer_capacity < 1
        ):
            raise ValueError("event_buffer_capacity must be a positive integer")
        self._tool_registry_factory = tool_registry_factory
        self._provider_resolver = provider_resolver
        self._default_settings = default_settings
        self._system_prompt = (prompt_builder or PromptBuilder()).build(
            additional_system_instructions
        )
        self._loop = AgentLoop()
        self._threads: dict[str, _ThreadRecord] = {}
        self._event_buffer_capacity = event_buffer_capacity
        self._reasoning_visibility = reasoning_visibility

    def create_thread(self, workspace: Path) -> ThreadSnapshot:
        normalized_workspace = workspace.resolve()
        if not normalized_workspace.is_dir():
            raise ValueError("workspace must be an existing directory")
        thread_id = str(uuid4())
        created_at = utc_now()
        record = _ThreadRecord(
            thread_id=thread_id,
            workspace=normalized_workspace,
            tools=self._tool_registry_factory(normalized_workspace),
            settings=ThreadSettings.from_model_settings(
                self._default_settings,
                version=0,
            ),
            conversation=Conversation(self._system_prompt),
            created_at=created_at,
            updated_at=created_at,
            events=EventBuffer(self._event_buffer_capacity),
        )
        self._threads[thread_id] = record
        return self._snapshot(record)

    async def run_turn(
        self,
        thread_id: str,
        user_text: str,
        *,
        settings_override: TurnSettingsOverride | None = None,
    ) -> TurnSummary:
        record = self._threads[thread_id]
        if record.status is not ThreadStatus.IDLE:
            raise ThreadBusyError(f"thread already has an active turn: {thread_id}")
        turn_id = str(uuid4())
        turn_config = (
            TurnConfig.from_thread_settings(
                record.settings,
                system_prompt=self._system_prompt,
            )
            if settings_override is None
            else TurnConfig.from_model_settings(
                settings_override.apply(record.settings),
                settings_version=record.settings.version,
                system_prompt=self._system_prompt,
            )
        )
        provider = self._provider_resolver(
            turn_config.provider_config_id,
            turn_config.model,
        )
        model = ModelInvoker(provider, turn_config)
        record.status = ThreadStatus.RUNNING
        record.active_turn_id = turn_id
        record.conversation.append_user(user_text)
        record.updated_at = utc_now()
        assert record.events is not None
        events = TurnEventEmitter(
            thread_id=thread_id,
            turn_id=turn_id,
            buffer=record.events,
            reasoning_visibility=self._reasoning_visibility,
        )
        started_at = utc_now()
        events.emit(
            "turn_started",
            {
                "user_message": user_text,
                "settings_version": turn_config.settings_version,
                "provider_config_id": turn_config.provider_config_id,
                "model": turn_config.model,
            },
        )
        try:
            outcome = await self._loop.run(
                record.conversation,
                record.tools,
                model,
                events,
            )
            record.completed_turns += 1
            summary = TurnSummary(
                schema_version=SCHEMA_VERSION,
                turn_id=turn_id,
                thread_id=thread_id,
                status=TurnStatus.COMPLETED,
                stop_reason="completed",
                final_text=outcome.final_text,
                iterations=outcome.iterations,
                tool_calls=outcome.tool_calls,
                usage=outcome.usage,
                started_at=started_at,
                ended_at=utc_now(),
            )
            record.latest_turn = deepcopy(summary)
            record.updated_at = summary.ended_at
            events.emit("turn_completed", {"summary": summary.to_dict()})
            return summary
        except Exception:
            record.updated_at = utc_now()
            events.emit(
                "turn_failed",
                {"error": {"code": "RUNTIME_ERROR", "message": "turn failed"}},
            )
            raise
        finally:
            record.status = ThreadStatus.IDLE
            record.active_turn_id = None
            record.updated_at = utc_now()

    def get_snapshot(self, thread_id: str) -> ThreadSnapshot:
        return self._snapshot(self._threads[thread_id])

    def get_events(
        self,
        thread_id: str,
        *,
        after_event_id: str | None = None,
    ) -> EventBatch:
        """Read retained events immediately; an expired cursor requires Snapshot recovery."""

        events = self._threads[thread_id].events
        assert events is not None
        return events.read(after_event_id)

    def update_settings(
        self,
        thread_id: str,
        *,
        expected_version: int,
        settings: ModelSettings,
    ) -> ThreadSettings:
        record = self._threads[thread_id]
        if record.settings.version != expected_version:
            raise SettingsConflictError(
                f"settings version {expected_version} is stale; "
                f"current version is {record.settings.version}"
            )
        updated = ThreadSettings.from_model_settings(
            settings,
            version=record.settings.version + 1,
        )
        record.settings = updated
        record.updated_at = utc_now()
        return updated

    @staticmethod
    def _snapshot(record: _ThreadRecord) -> ThreadSnapshot:
        return ThreadSnapshot(
            schema_version=SCHEMA_VERSION,
            thread_id=record.thread_id,
            workspace=record.workspace.as_posix(),
            status=record.status,
            active_turn_id=record.active_turn_id,
            completed_turns=record.completed_turns,
            settings=record.settings,
            messages=record.conversation.public_messages(),
            created_at=record.created_at,
            updated_at=record.updated_at,
            latest_turn=deepcopy(record.latest_turn),
        )
