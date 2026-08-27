"""In-memory Thread runtime and the minimal readable ReAct loop."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from agent.model.provider import LLMProvider
from agent.tools.registry import ToolRegistry

from .conversation import Conversation
from .errors import SettingsConflictError, ThreadBusyError
from .loop import AgentLoop
from .model_invoker import ModelInvoker
from .prompt import PromptBuilder
from .settings import (
    ModelSettings,
    ThreadSettings,
    TurnConfig,
    TurnSettingsOverride,
)
from .types import ThreadSnapshot, ThreadStatus, TurnStatus, TurnSummary


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
    ) -> None:
        self._tool_registry_factory = tool_registry_factory
        self._provider_resolver = provider_resolver
        self._default_settings = default_settings
        self._system_prompt = (prompt_builder or PromptBuilder()).build(
            additional_system_instructions
        )
        self._loop = AgentLoop()
        self._threads: dict[str, _ThreadRecord] = {}

    def create_thread(self, workspace: Path) -> ThreadSnapshot:
        normalized_workspace = workspace.resolve()
        if not normalized_workspace.is_dir():
            raise ValueError("workspace must be an existing directory")
        thread_id = str(uuid4())
        record = _ThreadRecord(
            thread_id=thread_id,
            workspace=normalized_workspace,
            tools=self._tool_registry_factory(normalized_workspace),
            settings=ThreadSettings.from_model_settings(
                self._default_settings,
                version=0,
            ),
            conversation=Conversation(self._system_prompt),
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
        try:
            outcome = await self._loop.run(record.conversation, record.tools, model)
            record.completed_turns += 1
            return TurnSummary(
                turn_id=turn_id,
                thread_id=thread_id,
                status=TurnStatus.COMPLETED,
                final_text=outcome.final_text,
                iterations=outcome.iterations,
                tool_calls=outcome.tool_calls,
            )
        finally:
            record.status = ThreadStatus.IDLE
            record.active_turn_id = None

    def get_snapshot(self, thread_id: str) -> ThreadSnapshot:
        return self._snapshot(self._threads[thread_id])

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
        return updated

    @staticmethod
    def _snapshot(record: _ThreadRecord) -> ThreadSnapshot:
        return ThreadSnapshot(
            thread_id=record.thread_id,
            workspace=record.workspace.as_posix(),
            status=record.status,
            active_turn_id=record.active_turn_id,
            completed_turns=record.completed_turns,
            settings=record.settings,
        )
