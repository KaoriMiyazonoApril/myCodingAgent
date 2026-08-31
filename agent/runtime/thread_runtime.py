"""In-memory Thread runtime and the minimal readable ReAct loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from agent.model.errors import LLMError
from agent.model.provider import LLMProvider
from agent.tools.registry import ToolRegistry

from .conversation import Conversation
from .change_tracker import ChangeTracker
from .errors import (
    ApprovalTimeoutError,
    ContextLimitError,
    IdempotencyConflictError,
    IdempotencyInterruptedError,
    SettingsConflictError,
    ThreadBusyError,
    ThreadClosedError,
    TurnLimitReached,
)
from .events import (
    AgentEvent,
    EventBatch,
    EventBuffer,
    EventSubscription,
    TurnEventEmitter,
    utc_now,
)
from .loop import AgentLoop
from .model_invoker import ModelInvoker
from .prompt import PromptBuilder
from .policy import CommandAwarePolicy, ToolPolicy
from .run_controller import RunController
from .settings import (
    ModelSettings,
    ThreadSettings,
    TurnConfig,
    TurnSettingsOverride,
)
from .thread_store import (
    InMemoryThreadStore,
    StoredActiveTurn,
    StoredIdempotency,
    ThreadState,
    ThreadStore,
)
from .types import SCHEMA_VERSION, ThreadSnapshot, ThreadStatus, TurnStatus, TurnSummary
from .tool_coordinator import ToolCoordinator
from .workspace_lease import WorkspaceLease, WorkspaceLeaseManager
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
    idempotency_key: str | None = None


@dataclass(slots=True)
class _IdempotentSubmission:
    user_text: str
    settings_override: TurnSettingsOverride | None
    future: asyncio.Future[TurnSummary] | None
    summary: TurnSummary | None = None
    interrupted: bool = False


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
    durable_events: list[AgentEvent] = field(default_factory=list)
    turns: list[TurnSummary] = field(default_factory=list)
    idempotent_submissions: dict[str, _IdempotentSubmission] = field(
        default_factory=dict
    )
    closing: bool = False


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
        default_context_window_tokens: int = 32_000,
        store: ThreadStore | None = None,
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
        if (
            isinstance(default_context_window_tokens, bool)
            or not isinstance(default_context_window_tokens, int)
            or not 1 <= default_context_window_tokens <= 10_000_000
        ):
            raise ValueError(
                "default_context_window_tokens must be between 1 and 10000000"
            )
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
        self._tool_policy = tool_policy or CommandAwarePolicy()
        self._approval_timeout_seconds = approval_timeout_seconds
        self._default_context_window_tokens = default_context_window_tokens
        self._store = store if store is not None else InMemoryThreadStore()
        validator_options: dict[str, object] = {
            "max_entries": workspace_validation_max_entries,
            "max_seconds": workspace_validation_max_seconds,
        }
        if workspace_validation_clock is not None:
            validator_options["clock"] = workspace_validation_clock
        self._workspace_validator = WorkspaceValidator(**validator_options)
        self._restore_stored_threads()

    def create_thread(
        self,
        workspace: Path,
        *,
        settings: ModelSettings | None = None,
    ) -> ThreadSnapshot:
        normalized_workspace = self._workspace_validator.normalize_root(workspace)
        thread_id = str(uuid4())
        created_at = utc_now()
        record = _ThreadRecord(
            thread_id=thread_id,
            workspace=normalized_workspace,
            tools=self._tool_registry_factory(normalized_workspace),
            settings=ThreadSettings.from_model_settings(
                settings or self._default_settings,
                version=0,
            ),
            conversation=Conversation(self._system_prompt),
            created_at=created_at,
            updated_at=created_at,
            events=EventBuffer(self._event_buffer_capacity),
        )
        assert record.events is not None
        record.events.set_durable_sink(
            lambda event, current=record: self._on_durable_event(current, event)
        )
        self._threads[thread_id] = record
        self._persist_record(record)
        return self._snapshot(record)

    def list_threads(self) -> list[ThreadSnapshot]:
        """Enumerate all durable Threads in stable creation order."""

        return [self._snapshot(record) for record in self._threads.values()]

    def open_thread(self, thread_id: str) -> ThreadSnapshot:
        """Open a restored Thread through the same public Snapshot seam."""

        return self.get_snapshot(thread_id)

    def get_turns(self, thread_id: str) -> list[TurnSummary]:
        """Return detached terminal Turn summaries for one Thread."""

        return deepcopy(self._threads[thread_id].turns)

    def _restore_stored_threads(self) -> None:
        """Hydrate canonical state and recover work that died with the process."""

        for state in self._store.list_threads():
            workspace = Path(state.workspace)
            tools = self._tools_for_restored_workspace(workspace)
            durable_events = list(state.events)
            events = EventBuffer(
                self._event_buffer_capacity,
                events=durable_events[-self._event_buffer_capacity :],
                initial_sequence=state.event_sequence,
            )
            record = _ThreadRecord(
                thread_id=state.thread_id,
                workspace=workspace,
                tools=tools,
                conversation=Conversation.from_messages(state.messages),
                settings=state.settings,
                status=state.status,
                completed_turns=state.completed_turns,
                created_at=state.created_at,
                updated_at=state.updated_at,
                latest_turn=deepcopy(state.latest_turn),
                events=events,
                durable_events=durable_events,
                turns=deepcopy(state.turns),
                idempotent_submissions={
                    key: _IdempotentSubmission(
                        user_text=value.user_text,
                        settings_override=deepcopy(value.settings_override),
                        future=None,
                        summary=deepcopy(value.summary),
                        interrupted=value.interrupted,
                    )
                    for key, value in state.idempotency.items()
                },
            )
            events.set_durable_sink(
                lambda event, current=record: self._on_durable_event(current, event)
            )
            self._threads[record.thread_id] = record
            if state.active_turn is not None or state.status in {
                ThreadStatus.RUNNING,
                ThreadStatus.WAITING_APPROVAL,
            }:
                self._recover_active_record(record, state.active_turn)

    def _tools_for_restored_workspace(self, workspace: Path) -> ToolRegistry:
        """Rebuild ephemeral tools when possible; history survives a missing root."""

        try:
            if not workspace.exists() or not workspace.is_dir():
                return ToolRegistry()
        except OSError:
            return ToolRegistry()
        return self._tool_registry_factory(workspace)

    def _recover_active_record(
        self,
        record: _ThreadRecord,
        active: StoredActiveTurn | None,
    ) -> None:
        """Turn interrupted by process restart becomes one terminal failure."""

        turn_id = active.turn_id if active is not None else str(uuid4())
        ended_at = utc_now()
        summary = TurnSummary(
            schema_version=SCHEMA_VERSION,
            turn_id=turn_id,
            thread_id=record.thread_id,
            status=TurnStatus.FAILED,
            stop_reason="runtime_restarted",
            final_text="" if active is None else active.last_assistant_text,
            iterations=0 if active is None else active.iterations,
            tool_calls=0 if active is None else active.tool_calls,
            usage={} if active is None else deepcopy(active.usage),
            modified_files=[],
            file_diffs=[],
            diff_complete=False,
            started_at="" if active is None else active.started_at,
            ended_at=ended_at,
            error={
                "code": "RUNTIME_RESTARTED",
                "message": "Turn was interrupted when the Runtime restarted",
            },
        )
        record.status = ThreadStatus.IDLE
        record.active_turn = None
        record.conversation.append_interrupted_tool_results()
        record.completed_turns += 1
        record.latest_turn = deepcopy(summary)
        record.turns.append(deepcopy(summary))
        record.updated_at = ended_at
        if active is not None and active.idempotency_key is not None:
            submission = record.idempotent_submissions.get(active.idempotency_key)
            if submission is not None:
                submission.summary = deepcopy(summary)
                submission.interrupted = True
                submission.future = None
        assert record.events is not None
        emitter = TurnEventEmitter(
            thread_id=record.thread_id,
            turn_id=turn_id,
            buffer=record.events,
            reasoning_visibility=self._reasoning_visibility,
        )
        emitter.emit("turn_failed", {"summary": summary.to_dict()})
        self._persist_record(record)

    def _on_durable_event(self, record: _ThreadRecord, event: AgentEvent) -> None:
        """Mirror one semantic event exactly once before committing state."""

        if event not in record.durable_events:
            record.durable_events.append(event)
        self._persist_record(record)

    def _persist_record(self, record: _ThreadRecord) -> None:
        """Write one detached semantic state transition through ThreadStore."""

        events = record.events
        assert events is not None
        active = record.active_turn
        active_state = None
        if active is not None:
            active_state = StoredActiveTurn(
                turn_id=active.turn_id,
                started_at=active.started_at,
                idempotency_key=active.idempotency_key,
                iterations=active.controller.iterations,
                tool_calls=active.controller.tool_calls,
                usage=deepcopy(active.controller.usage),
                last_assistant_text=active.controller.last_assistant_text,
            )
        state = ThreadState(
            thread_id=record.thread_id,
            workspace=record.workspace.as_posix(),
            status=record.status,
            settings=record.settings,
            messages=record.conversation.canonical_messages(),
            completed_turns=record.completed_turns,
            created_at=record.created_at,
            updated_at=record.updated_at,
            latest_turn=deepcopy(record.latest_turn),
            turns=deepcopy(record.turns),
            events=deepcopy(record.durable_events),
            event_sequence=events.thread_sequence,
            active_turn=active_state,
            idempotency={
                key: StoredIdempotency(
                    user_text=value.user_text,
                    settings_override=deepcopy(value.settings_override),
                    summary=deepcopy(value.summary),
                    interrupted=value.interrupted,
                )
                for key, value in record.idempotent_submissions.items()
            },
        )
        self._store.save_thread(state)

    async def run_turn(
        self,
        thread_id: str,
        user_text: str,
        *,
        settings_override: TurnSettingsOverride | None = None,
        idempotency_key: str | None = None,
    ) -> TurnSummary:
        if not isinstance(user_text, str):
            raise ValueError("user_text must be a string")
        self._validate_idempotency_key(idempotency_key)
        record = self._threads[thread_id]
        if idempotency_key is not None:
            existing = record.idempotent_submissions.get(idempotency_key)
            if existing is not None:
                if (
                    existing.user_text != user_text
                    or existing.settings_override != settings_override
                ):
                    raise IdempotencyConflictError(
                        "idempotency key was already used for another Turn"
                    )
                if existing.interrupted:
                    raise IdempotencyInterruptedError(
                        "idempotent Turn was interrupted by a Runtime restart"
                    )
                if existing.summary is not None:
                    return deepcopy(existing.summary)
                assert existing.future is not None
                return deepcopy(await asyncio.shield(existing.future))
        if record.status is not ThreadStatus.IDLE:
            if record.status is ThreadStatus.CLOSED or record.closing:
                raise ThreadClosedError(f"thread is closed: {thread_id}")
            raise ThreadBusyError(f"thread already has an active turn: {thread_id}")
        turn_id = str(uuid4())
        assert record.events is not None
        events = TurnEventEmitter(
            thread_id=thread_id,
            turn_id=turn_id,
            buffer=record.events,
            reasoning_visibility=self._reasoning_visibility,
        )
        workspace_lease: WorkspaceLease | None = None
        try:
            # A restored Thread remains readable when its root disappeared,
            # but a new Turn must fail before provider/tool execution and must
            # never recreate or substitute that workspace.
            await asyncio.to_thread(
                self._workspace_validator.validate,
                record.workspace,
            )
            turn_config = (
                TurnConfig.from_thread_settings(
                    record.settings,
                    system_prompt=self._system_prompt,
                    reasoning_visibility=self._reasoning_visibility,
                )
                if settings_override is None
                else TurnConfig.from_model_settings(
                    settings_override.apply(record.settings),
                    settings_version=record.settings.version,
                    system_prompt=self._system_prompt,
                    reasoning_visibility=self._reasoning_visibility,
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
                default_context_window_tokens=self._default_context_window_tokens,
            )
            model.ensure_context(
                record.conversation.prospective_request_messages(user_text),
                record.tools.definitions(),
            )
            workspace_lease = self._workspace_leases.acquire(record.workspace)
            if record.status is ThreadStatus.CLOSED or record.closing:
                raise ThreadClosedError(f"thread is closed: {thread_id}")
        except BaseException as error:
            events.emit("turn_rejected", self._preflight_rejection(error))
            if workspace_lease is not None:
                self._workspace_leases.release(workspace_lease)
            raise
        record.status = ThreadStatus.RUNNING
        submission: _IdempotentSubmission | None = None
        try:
            record.conversation.append_user(user_text)
            record.updated_at = utc_now()
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
                approval_mode=turn_config.approval_mode,
            )
            active_turn = _ActiveTurn(
                turn_id=turn_id,
                controller=controller,
                tools=tools,
                changes=changes,
                events=events,
                started_at=utc_now(),
                idempotency_key=idempotency_key,
            )
            record.active_turn = active_turn
            if idempotency_key is not None:
                submission = _IdempotentSubmission(
                    user_text=user_text,
                    settings_override=settings_override,
                    future=asyncio.get_running_loop().create_future(),
                )
                record.idempotent_submissions[idempotency_key] = submission
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
            summary = await self._execute_active_turn(record, active_turn, model)
            if submission is not None:
                submission.summary = deepcopy(summary)
                assert submission.future is not None
                submission.future.set_result(deepcopy(summary))
                submission.future = None
            self._persist_record(record)
            return summary
        except BaseException:
            if idempotency_key is not None and submission is not None:
                record.idempotent_submissions.pop(idempotency_key, None)
                if submission.future is not None and not submission.future.done():
                    submission.future.cancel()
            raise
        finally:
            record.status = (
                ThreadStatus.CLOSED if record.closing else ThreadStatus.IDLE
            )
            record.active_turn = None
            record.updated_at = utc_now()
            self._persist_record(record)
            assert workspace_lease is not None
            self._workspace_leases.release(workspace_lease)
            if record.closing:
                await record.tools.aclose()

    async def aclose(self) -> None:
        """Await cleanup for every Thread-owned stateful tool capability."""

        records = tuple(self._threads.values())
        for record in records:
            if record.active_turn is not None:
                record.active_turn.controller.cancel()
            elif record.status is not ThreadStatus.CLOSED:
                # Host shutdown is process lifecycle, not an explicit user
                # close. Preserve the Thread as resumable durable history.
                record.status = ThreadStatus.IDLE
                record.updated_at = utc_now()
                self._persist_record(record)
        await asyncio.gather(
            *(record.tools.aclose() for record in records),
            return_exceptions=True,
        )

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
        except ContextLimitError:
            return self._finish_turn(
                record,
                active_turn,
                status=TurnStatus.FAILED,
                stop_reason="context_limit",
                final_text=controller.last_assistant_text,
                event_type="turn_failed",
                error={
                    "code": "CONTEXT_LIMIT",
                    "message": "conversation exceeds the model context budget",
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

    def subscribe_events(
        self,
        thread_id: str,
        *,
        after_event_id: str | None = None,
    ) -> EventSubscription:
        """Subscribe to wake-only real-time events for one Thread."""

        events = self._threads[thread_id].events
        assert events is not None
        return events.subscribe(after_event_id)

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
        if record.status is ThreadStatus.CLOSED or record.closing:
            raise ThreadClosedError(f"thread is closed: {thread_id}")
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
        assert record.events is not None
        record.events.emit_thread(
            thread_id=thread_id,
            event_type="settings_updated",
            payload={
                "settings_version": updated.version,
                "provider_config_id": updated.provider_config_id,
                "model": updated.model,
            },
        )
        return updated

    def close_thread(self, thread_id: str) -> bool:
        """Close an idle Thread or cancel its active Turn before closing it."""

        record = self._threads[thread_id]
        if record.status is ThreadStatus.CLOSED or record.closing:
            return False
        record.closing = True
        record.updated_at = utc_now()
        active_turn = record.active_turn
        if active_turn is None:
            record.status = ThreadStatus.CLOSED
            record.tools.close()
            self._persist_record(record)
            return True
        active_turn.events.emit("thread_close_requested", {})
        active_turn.controller.cancel()
        return True

    @staticmethod
    def _validate_idempotency_key(idempotency_key: str | None) -> None:
        if idempotency_key is None:
            return
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key) > 200
        ):
            raise ValueError(
                "idempotency_key must be a non-empty string of at most 200 characters"
            )

    @staticmethod
    def _preflight_rejection(error: BaseException) -> dict[str, object]:
        if isinstance(error, asyncio.CancelledError):
            code = "TURN_CANCELLED_BEFORE_START"
            message = "Turn was cancelled before it started"
        else:
            public_code = getattr(error, "code", None)
            code = public_code if isinstance(public_code, str) else "PREFLIGHT_FAILED"
            message = "Turn could not start"
        return {"error": {"code": code, "message": message}}

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
            turns=deepcopy(record.turns),
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
        record.turns.append(deepcopy(summary))
        record.updated_at = summary.ended_at
        active_turn.events.emit(event_type, {"summary": summary.to_dict()})
        return summary
