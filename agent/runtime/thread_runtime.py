"""In-memory Thread runtime and the minimal readable ReAct loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from agent.model.errors import LLMError
from agent.model.provider import LLMProvider
from agent.tools.registry import ToolRegistry

from .conversation import Conversation
from .change_tracker import ChangeTracker
from .errors import (
    ApprovalTimeoutError,
    SettingsConflictError,
    ThreadBusyError,
    TurnLimitReached,
)
from .events import EventBatch, EventBuffer, TurnEventEmitter, utc_now
from .loop import AgentLoop
from .model_invoker import ModelInvoker
from .prompt import PromptBuilder
from .policy import AllowAllPolicy, ToolPolicy
from .run_controller import RunController
from .settings import (
    ModelSettings,
    ThreadSettings,
    TurnConfig,
    TurnSettingsOverride,
)
from .types import SCHEMA_VERSION, ThreadSnapshot, ThreadStatus, TurnStatus, TurnSummary
from .tool_coordinator import ToolCoordinator
from .workspace_lease import WorkspaceLeaseManager
from .workspace_validator import WorkspaceValidator


ToolRegistryFactory = Callable[[Path], ToolRegistry]
ProviderResolver = Callable[[str, str], LLMProvider]


@dataclass(slots=True)
class _ActiveTurn:
    turn_id: str
    controller: RunController
    tools: ToolCoordinator
    changes: ChangeTracker
    events: TurnEventEmitter
    started_at: str


@dataclass(slots=True)
class _ThreadRecord:
    thread_id: str
    workspace: Path
    tools: ToolRegistry
    conversation: Conversation
    settings: ThreadSettings
    status: ThreadStatus = ThreadStatus.IDLE
    active_turn: _ActiveTurn | None = None
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
        model_retry_delays: tuple[float, ...] = (0.1, 0.2),
        max_active_turns: int = 4,
        tool_policy: ToolPolicy | None = None,
        approval_timeout_seconds: float = 5 * 60,
        workspace_validation_max_entries: int = 100_000,
        workspace_validation_max_seconds: float = 10,
        workspace_validation_clock: Callable[[], float] | None = None,
    ) -> None:
        if reasoning_visibility not in {"hidden", "debug"}:
            raise ValueError("reasoning_visibility must be 'hidden' or 'debug'")
        if (
            isinstance(event_buffer_capacity, bool)
            or not isinstance(event_buffer_capacity, int)
            or event_buffer_capacity < 1
        ):
            raise ValueError("event_buffer_capacity must be a positive integer")
        if not isinstance(model_retry_delays, tuple) or any(
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or delay < 0
            for delay in model_retry_delays
        ):
            raise ValueError("model_retry_delays must be non-negative numbers")
        if (
            isinstance(approval_timeout_seconds, bool)
            or not isinstance(approval_timeout_seconds, (int, float))
            or not 0 < approval_timeout_seconds <= 60 * 60
        ):
            raise ValueError(
                "approval_timeout_seconds must be greater than 0 and at most 3600"
            )
        if tool_policy is not None and not callable(
            getattr(tool_policy, "decide", None)
        ):
            raise ValueError("tool_policy must provide decide(call)")
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
        self._model_retry_delays = model_retry_delays
        self._workspace_leases = WorkspaceLeaseManager(max_active_turns)
        self._tool_policy = tool_policy or AllowAllPolicy()
        self._approval_timeout_seconds = approval_timeout_seconds
        validator_options: dict[str, object] = {
            "max_entries": workspace_validation_max_entries,
            "max_seconds": workspace_validation_max_seconds,
        }
        if workspace_validation_clock is not None:
            validator_options["clock"] = workspace_validation_clock
        self._workspace_validator = WorkspaceValidator(**validator_options)

    def create_thread(self, workspace: Path) -> ThreadSnapshot:
        normalized_workspace = self._workspace_validator.normalize_root(workspace)
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
        model = ModelInvoker(
            provider,
            turn_config,
            retry_delays=self._model_retry_delays,
        )
        workspace_lease = self._workspace_leases.acquire(record.workspace)
        try:
            self._workspace_validator.validate(record.workspace)
        except Exception:
            self._workspace_leases.release(workspace_lease)
            raise
        record.status = ThreadStatus.RUNNING
        try:
            record.conversation.append_user(user_text)
            record.updated_at = utc_now()
            assert record.events is not None
            events = TurnEventEmitter(
                thread_id=thread_id,
                turn_id=turn_id,
                buffer=record.events,
                reasoning_visibility=self._reasoning_visibility,
            )
            controller = RunController(turn_config.limits)
            current_task = asyncio.current_task()
            assert current_task is not None
            controller.bind(current_task)
            changes = ChangeTracker(record.workspace, events)
            tools = ToolCoordinator(
                registry=record.tools,
                conversation=record.conversation,
                events=events,
                controller=controller,
                policy=self._tool_policy,
                approval_timeout_seconds=self._approval_timeout_seconds,
                set_waiting=lambda waiting: self._set_waiting(record, waiting),
                change_tracker=changes,
            )
            active_turn = _ActiveTurn(
                turn_id=turn_id,
                controller=controller,
                tools=tools,
                changes=changes,
                events=events,
                started_at=utc_now(),
            )
            record.active_turn = active_turn
            events.emit(
                "turn_started",
                {
                    "user_message": user_text,
                    "settings_version": turn_config.settings_version,
                    "provider_config_id": turn_config.provider_config_id,
                    "model": turn_config.model,
                    "limits": {
                        "max_iterations": turn_config.limits.max_iterations,
                        "max_tool_calls": turn_config.limits.max_tool_calls,
                        "max_execution_seconds": (
                            turn_config.limits.max_execution_seconds
                        ),
                    },
                },
            )
            return await self._execute_active_turn(record, active_turn, model)
        finally:
            record.status = ThreadStatus.IDLE
            record.active_turn = None
            record.updated_at = utc_now()
            self._workspace_leases.release(workspace_lease)

    async def _execute_active_turn(
        self,
        record: _ThreadRecord,
        active_turn: _ActiveTurn,
        model: ModelInvoker,
    ) -> TurnSummary:
        controller = active_turn.controller
        try:
            outcome = await self._loop.run(
                record.conversation,
                active_turn.tools,
                model,
                active_turn.events,
                controller,
            )
            return self._finish_turn(
                record,
                active_turn,
                status=TurnStatus.COMPLETED,
                stop_reason="completed",
                final_text=outcome.final_text,
                event_type="turn_completed",
            )
        except TurnLimitReached as error:
            return self._finish_turn(
                record,
                active_turn,
                status=TurnStatus.LIMIT_REACHED,
                stop_reason=error.reason,
                final_text=controller.last_assistant_text,
                event_type="turn_limit_reached",
            )
        except asyncio.CancelledError:
            return self._finish_turn(
                record,
                active_turn,
                status=TurnStatus.CANCELLED,
                stop_reason="cancelled",
                final_text=controller.last_assistant_text,
                event_type="turn_cancelled",
            )
        except ApprovalTimeoutError:
            return self._finish_turn(
                record,
                active_turn,
                status=TurnStatus.FAILED,
                stop_reason="approval_timeout",
                final_text=controller.last_assistant_text,
                event_type="turn_failed",
                error={
                    "code": "APPROVAL_TIMEOUT",
                    "message": "tool approval timed out",
                },
            )
        except Exception as error:
            is_model_error = isinstance(error, LLMError)
            public_error = {
                "code": "LLM_ERROR" if is_model_error else "RUNTIME_ERROR",
                "message": (
                    "model request failed" if is_model_error else "turn failed"
                ),
            }
            return self._finish_turn(
                record,
                active_turn,
                status=TurnStatus.FAILED,
                stop_reason="model_error" if is_model_error else "runtime_error",
                final_text=controller.last_assistant_text,
                event_type="turn_failed",
                error=public_error,
            )

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

    def cancel_turn(self, thread_id: str) -> bool:
        """Request cancellation of the Thread's active model/tool operation."""

        record = self._threads[thread_id]
        active_turn = record.active_turn
        if active_turn is None:
            return False
        cancelled = active_turn.controller.cancel()
        if cancelled:
            active_turn.events.emit("turn_cancel_requested", {})
        return cancelled

    def resolve_approval(
        self,
        thread_id: str,
        *,
        approval_id: str,
        approved: bool,
    ) -> bool:
        """Resolve the active approval when both Thread and request ID match."""

        if not isinstance(approved, bool):
            raise ValueError("approved must be a boolean")
        active_turn = self._threads[thread_id].active_turn
        if active_turn is None:
            return False
        return active_turn.tools.resolve_approval(approval_id, approved)

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
    def _set_waiting(record: _ThreadRecord, waiting: bool) -> None:
        record.status = (
            ThreadStatus.WAITING_APPROVAL if waiting else ThreadStatus.RUNNING
        )
        record.updated_at = utc_now()

    @staticmethod
    def _snapshot(record: _ThreadRecord) -> ThreadSnapshot:
        active_turn_id = (
            None if record.active_turn is None else record.active_turn.turn_id
        )
        return ThreadSnapshot(
            schema_version=SCHEMA_VERSION,
            thread_id=record.thread_id,
            workspace=record.workspace.as_posix(),
            status=record.status,
            active_turn_id=active_turn_id,
            completed_turns=record.completed_turns,
            settings=record.settings,
            messages=record.conversation.public_messages(),
            created_at=record.created_at,
            updated_at=record.updated_at,
            latest_turn=deepcopy(record.latest_turn),
        )

    @staticmethod
    def _finish_turn(
        record: _ThreadRecord,
        active_turn: _ActiveTurn,
        *,
        status: TurnStatus,
        stop_reason: str,
        final_text: str,
        event_type: str,
        error: dict[str, object] | None = None,
    ) -> TurnSummary:
        file_diffs = active_turn.changes.changes()
        summary = TurnSummary(
            schema_version=SCHEMA_VERSION,
            turn_id=active_turn.turn_id,
            thread_id=record.thread_id,
            status=status,
            stop_reason=stop_reason,
            final_text=final_text,
            iterations=active_turn.controller.iterations,
            tool_calls=active_turn.controller.tool_calls,
            usage=deepcopy(active_turn.controller.usage),
            modified_files=[change["path"] for change in file_diffs],
            file_diffs=file_diffs,
            diff_complete=active_turn.changes.diff_complete,
            started_at=active_turn.started_at,
            ended_at=utc_now(),
            error=error,
        )
        record.completed_turns += 1
        record.latest_turn = deepcopy(summary)
        record.updated_at = summary.ended_at
        active_turn.events.emit(event_type, {"summary": summary.to_dict()})
        return summary
