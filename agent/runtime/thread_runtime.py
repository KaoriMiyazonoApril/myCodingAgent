"""In-memory Thread runtime and the minimal readable ReAct loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import threading
from uuid import uuid4

from agent.model.errors import LLMError
from agent.model.provider import LLMProvider
from agent.model.types import ProviderCapabilities, ThinkingParameterStyle
from agent.tools.registry import ToolRegistry
from agent.tools.types import ToolDefinition, ToolResult

from .conversation import Conversation
from .change_tracker import ChangeTracker
from .context_manager import (
    BaseSystemInstructions,
    ContextManager,
    ProjectInstructionsProvider,
    RootProjectInstructionsProvider,
    RuntimeContext,
    TaskPlan,
    TaskState,
)
from .context_budget import ContextBudget
from .history import (
    CompactionCheckpoint,
    CompactionError,
    LLMHistoryCompactor,
)
from .skills import SkillDiscovery, SkillCatalog, SkillTurnState, explicit_skill_names
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


logger = logging.getLogger(__name__)


ToolRegistryFactory = Callable[[Path], ToolRegistry]
ProviderResolver = Callable[[str, str], LLMProvider]


async def _validate_workspace_async(validator: WorkspaceValidator, workspace: Path) -> None:
    """Run the bounded root check without joining asyncio's default executor.

    The validator normally performs one cheap ``stat``.  Keeping this seam
    asynchronous is still important for embedders (and for cancellation
    tests that replace it with a deliberately blocking checker), while a
    daemon worker avoids the process-wide executor shutdown path used by
    ``asyncio.to_thread`` on affected Python builds.
    """

    error_holder: list[BaseException] = []
    settled = threading.Event()

    def validate() -> None:
        try:
            validator.validate(workspace)
        except BaseException as error:
            error_holder.append(error)
        finally:
            settled.set()

    threading.Thread(
        target=validate,
        name="my-coding-agent-workspace-validation",
        daemon=True,
    ).start()
    while not settled.is_set():
        try:
            await asyncio.sleep(0.005)
        except asyncio.CancelledError:
            # No workspace lease or provider has been acquired at this point.
            # The bounded checker can finish independently without keeping
            # the cancelled Turn or event-loop shutdown alive.
            raise
    if error_holder:
        raise error_holder[0]


@dataclass(slots=True)
class _ActiveTurn:
    turn_id: str
    controller: RunController
    tools: ToolCoordinator
    changes: ChangeTracker
    events: TurnEventEmitter
    skills: SkillTurnState
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
    preflight_turn_id: str | None = None
    closing: bool = False
    task_state: TaskState | None = None
    compaction_checkpoint: CompactionCheckpoint | None = None
    checkpoint_diagnostics: tuple[str, ...] = ()
    skill_catalog: SkillCatalog = field(default_factory=SkillCatalog)
    context_diagnostics: dict[str, object] = field(default_factory=dict)


class ThreadRuntime:
    """Create in-memory Threads and run one ReAct Turn at a time."""

    def __init__(
        self,
        *,
        tool_registry_factory: ToolRegistryFactory,
        provider_resolver: ProviderResolver,
        default_settings: ModelSettings,
        additional_system_instructions: str | None = None,
        project_instructions_provider: ProjectInstructionsProvider | None = None,
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
        if reasoning_visibility not in {"hidden", "visible", "debug"}:
            raise ValueError(
                "reasoning_visibility must be 'hidden', 'visible' or 'debug'"
            )
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
        self._base_system_instructions = BaseSystemInstructions(
            (prompt_builder or PromptBuilder()).build()
        )
        self._project_instructions_provider = project_instructions_provider or RootProjectInstructionsProvider(
            additional_instructions=additional_system_instructions,
        )
        # Conversation persists the stable base only. Project/runtime/task
        # sections are assembled into detached model requests by ContextManager.
        self._system_prompt = self._base_system_instructions.text
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
            skill_catalog=SkillDiscovery().discover(normalized_workspace),
        )
        self._register_update_plan_tool(record)
        self._register_skill_tool(record)
        set_context = getattr(record.tools, "set_session_context", None)
        if callable(set_context):
            set_context(thread_id=thread_id, turn_id=None)
        assert record.events is not None
        record.events.set_durable_sink(
            lambda event, current=record: self._on_durable_event(current, event)
        )
        record.events.set_sequence_checkpoint(
            lambda _watermark, current=record: self._persist_record(current)
        )
        self._threads[thread_id] = record
        self._persist_record(record)
        return self._snapshot(record)

    def list_threads(self) -> list[ThreadSnapshot]:
        """Enumerate all durable Threads in stable creation order."""

        return [self._snapshot(record) for record in self._threads.values()]

    @staticmethod
    def _register_update_plan_tool(record: _ThreadRecord) -> None:
        """Install the narrow model plan capability on one Thread registry."""

        if record.tools.lookup("update_plan") is not None:
            return

        definition = ToolDefinition(
            name="update_plan",
            description=(
                "Replace the optional current task plan with ordered steps. "
                "Use pending, in_progress, completed, or blocked statuses."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "minItems": 0,
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "properties": {
                                "step": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 2_000,
                                },
                                "status": {
                                    "type": "string",
                                    "enum": [
                                        "pending",
                                        "in_progress",
                                        "completed",
                                        "blocked",
                                    ],
                                },
                            },
                            "required": ["step", "status"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["steps"],
                "additionalProperties": False,
            },
        )

        def execute(arguments: dict[str, object]) -> ToolResult:
            state = record.task_state
            if state is None:
                return ToolResult(
                    content="update_plan is available only during an active Turn",
                    metadata={},
                    error_code="PLAN_UNAVAILABLE",
                )
            try:
                plan = TaskPlan.from_payload(arguments.get("steps"))
                state.replace_plan(plan)
            except (TypeError, ValueError) as error:
                return ToolResult(
                    content=str(error),
                    metadata={},
                    error_code="INVALID_ARGUMENTS",
                )
            return ToolResult(
                content="task plan updated",
                metadata={"plan_steps": len(plan.steps)},
            )

        async def execute_async(arguments: dict[str, object]) -> ToolResult:
            return execute(arguments)

        record.tools.register(definition, execute, async_executor=execute_async)

    @staticmethod
    def _register_skill_tool(record: _ThreadRecord) -> None:
        """Install the real local ``skill(name)`` capability once per Thread."""

        if record.tools.lookup("skill") is not None:
            return
        definition = ToolDefinition(
            name="skill",
            description="Load Skill instructions.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                    }
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        )

        def execute(arguments: dict[str, object]) -> ToolResult:
            active = record.active_turn
            if active is None:
                return ToolResult(
                    content="skill is available only during an active Turn",
                    metadata={"status": "unavailable"},
                    error_code="SKILL_UNAVAILABLE",
                )
            name = arguments.get("name")
            if not isinstance(name, str):
                return ToolResult(
                    content="skill name must be a string",
                    metadata={"status": "invalid"},
                    error_code="INVALID_ARGUMENTS",
                )
            return active.skills.load(name)

        async def execute_async(arguments: dict[str, object]) -> ToolResult:
            return execute(arguments)

        record.tools.register(definition, execute, async_executor=execute_async)

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
                compaction_checkpoint=deepcopy(state.checkpoint),
                checkpoint_diagnostics=state.checkpoint_diagnostics,
                skill_catalog=SkillDiscovery().discover(workspace),
            )
            self._register_update_plan_tool(record)
            self._register_skill_tool(record)
            set_context = getattr(record.tools, "set_session_context", None)
            if callable(set_context):
                set_context(thread_id=record.thread_id, turn_id=None)
            events.set_durable_sink(
                lambda event, current=record: self._on_durable_event(current, event)
            )
            events.set_sequence_checkpoint(
                lambda _watermark, current=record: self._persist_record(current)
            )
            self._threads[record.thread_id] = record
            if self._is_coherently_terminal(state):
                # A terminal summary/event may have committed just before an
                # older runtime cleared its active marker. Repair that stale
                # marker without creating a second terminal outcome.
                record.status = (
                    ThreadStatus.CLOSED
                    if state.status is ThreadStatus.CLOSED
                    else ThreadStatus.IDLE
                )
                record.active_turn = None
                self._persist_record(record)
            elif state.active_turn is not None or state.status in {
                ThreadStatus.RUNNING,
                ThreadStatus.WAITING_APPROVAL,
            }:
                self._recover_active_record(record, state.active_turn)

    @staticmethod
    def _is_coherently_terminal(state: ThreadState) -> bool:
        """Recognize a terminal transition whose active marker is merely stale."""

        active = state.active_turn
        summary = state.latest_turn
        if active is None or summary is None or summary.turn_id != active.turn_id:
            return False
        if summary.status in {TurnStatus.QUEUED, TurnStatus.RUNNING}:
            return False
        if state.completed_turns < 1 or not any(
            turn.turn_id == summary.turn_id and turn.status == summary.status
            for turn in state.turns
        ):
            return False
        terminal_event_types = {
            "turn_completed",
            "turn_failed",
            "turn_cancelled",
            "turn_limit_reached",
        }
        for event in state.events:
            if event.turn_id != summary.turn_id or event.type not in terminal_event_types:
                continue
            event_summary = event.payload.get("summary")
            if not isinstance(event_summary, dict):
                continue
            if (
                event_summary.get("turn_id") != summary.turn_id
                or event_summary.get("status") != summary.status.value
            ):
                continue
            if active.idempotency_key is None:
                return True
            submission = state.idempotency.get(active.idempotency_key)
            return bool(
                submission is not None
                and submission.summary is not None
                and submission.summary.turn_id == summary.turn_id
                and submission.summary.status == summary.status
            )
        return False

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
        emitter.emit(
            "turn_failed",
            {"summary": summary.to_dict()},
            checkpoint=False,
        )
        self._persist_record(record)

    def _on_durable_event(self, record: _ThreadRecord, event: AgentEvent) -> None:
        """Mirror one semantic event exactly once before committing state."""

        if event not in record.durable_events:
            record.durable_events.append(event)
        self._persist_record(record, new_events=(event,))

    def _persist_record(
        self,
        record: _ThreadRecord,
        *,
        new_events: tuple[AgentEvent, ...] = (),
    ) -> None:
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
            event_sequence=events.sequence_watermark,
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
            checkpoint=deepcopy(record.compaction_checkpoint),
            checkpoint_diagnostics=record.checkpoint_diagnostics,
        )
        transition = getattr(self._store, "save_thread_transition", None)
        if callable(transition):
            transition(state, new_events=new_events)
        else:
            self._store.save_thread(state)

    def _save_checkpoint(
        self,
        record: _ThreadRecord,
        checkpoint: CompactionCheckpoint,
    ) -> None:
        """Persist a rolling checkpoint without splitting memory and store.

        Compaction invokes this callback while a Turn is active.  The
        in-memory record must not advertise a new checkpoint when the durable
        transition fails; otherwise a later request could incorrectly reuse a
        prefix that was never committed to the ThreadStore.
        """

        previous = record.compaction_checkpoint
        try:
            record.compaction_checkpoint = deepcopy(checkpoint)
            self._persist_record(record)
        except BaseException:
            record.compaction_checkpoint = previous
            raise

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
        if record.status is not ThreadStatus.IDLE or record.preflight_turn_id is not None:
            if record.status is ThreadStatus.CLOSED or record.closing:
                raise ThreadClosedError(f"thread is closed: {thread_id}")
            raise ThreadBusyError(f"thread already has an active turn: {thread_id}")
        turn_id = str(uuid4())
        # Reserve the Turn slot before the first await.  Host submissions have
        # their own starting marker, but direct Runtime callers must also be
        # unable to race two preflight phases into one Thread.
        record.preflight_turn_id = turn_id
        assert record.events is not None
        events = TurnEventEmitter(
            thread_id=thread_id,
            turn_id=turn_id,
            buffer=record.events,
            reasoning_visibility=self._reasoning_visibility,
        )
        workspace_lease: WorkspaceLease | None = None
        provider: LLMProvider | None = None
        try:
            # A restored Thread remains readable when its root disappeared,
            # but a new Turn must fail before provider/tool execution and must
            # never recreate or substitute that workspace.
            # WorkspaceValidator deliberately performs one bounded root
            # ``stat``/directory check and never walks descendants. Keep the
            # public preflight awaitable for embedders and cancellation, but
            # use our daemon-worker seam instead of asyncio's process-global
            # default executor.
            await _validate_workspace_async(
                self._workspace_validator,
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
            # Acquire the workspace lease and complete the provider-free
            # context preflight before resolving a model client.  Rejected
            # Turns therefore do not allocate an HTTP transport.
            workspace_lease = self._workspace_leases.acquire(record.workspace)
            if record.status is ThreadStatus.CLOSED or record.closing:
                raise ThreadClosedError(f"thread is closed: {thread_id}")
            provider_capabilities = self._provider_capabilities_for(turn_config)
            if provider_capabilities is None:
                # Legacy/in-memory resolvers may expose capabilities only on
                # the provider object.  Keep that compatibility path while
                # production resolvers use ``capabilities_for`` above to stay
                # lazy about HTTP client construction.
                provider = self._provider_resolver(
                    turn_config.provider_config_id,
                    turn_config.model,
                )
                provider_capabilities = provider.capabilities
            # User text remains canonical Conversation intent.  TaskState is
            # deliberately limited to model-maintained plan plus objective
            # Harness evidence and never mirrors that goal.
            task_state = TaskState()
            pending_skill_events: list[tuple[str, dict[str, object]]] = []
            def skill_loaded(skill) -> None:
                activation_source = "tool"
                placement = "tool_history"
                activation = skill_state.activation_metadata(skill.name)
                if activation is not None:
                    activation_source = activation["activation_source"]
                    placement = activation["placement"]
                payload = {
                    "name": skill.name,
                    "source": skill.source,
                    "source_path": skill.source_path,
                    "description": skill.description,
                    "activation_source": activation_source,
                    "placement": placement,
                }
                # Explicit mentions are loaded during provider-free preflight,
                # so hold their event until after ``turn_started``.  A model
                # ``skill(name)`` call happens during an active Turn and must
                # be emitted immediately so Host/SSE consumers see both
                # activation paths as one live state transition.
                if record.active_turn is None:
                    pending_skill_events.append(("skill_loaded", payload))
                else:
                    events.emit("skill_loaded", payload)

            skill_state = SkillTurnState(
                record.skill_catalog,
                on_loaded=skill_loaded,
            )
            explicit_names = explicit_skill_names(user_text)
            explicit_loaded = skill_state.explicit_activation(explicit_names)
            for name in explicit_names:
                if name not in explicit_loaded:
                    pending_skill_events.append(
                        (
                            "skill_activation_failed",
                            {
                                "name": name,
                                "error_code": "SKILL_NOT_FOUND",
                            },
                        )
                    )
            context_manager = self._context_manager_for(
                record,
                turn_config,
                provider_capabilities,
                skill_catalog=record.skill_catalog,
                skill_state=skill_state,
            )
            # Preflight only the irreducible request prefix.  Reducible
            # durable history is handled after provider resolution by the
            # one-pass Context reduction pipeline; an oversized current user
            # input/system/tool schema still rejects without allocating a
            # transport.
            canonical = record.conversation.canonical_messages()
            context_manager.assemble(
                canonical[:1],
                current_input=user_text,
                runtime_context=self._runtime_context_for(
                    record, turn_config, turn_id=turn_id
                ),
                task_state=task_state,
                tools=record.tools.definitions(),
            )
            if provider is None:
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
        except BaseException as error:
            events.emit("turn_rejected", self._preflight_rejection(error))
            if workspace_lease is not None:
                self._workspace_leases.release(workspace_lease)
            await self._release_turn_provider(provider)
            raise
        finally:
            if record.preflight_turn_id == turn_id:
                record.preflight_turn_id = None
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
                turn_id=turn_id,
                registry=record.tools,
                conversation=record.conversation,
                events=events,
                controller=controller,
                policy=self._tool_policy,
                approval_timeout_seconds=self._approval_timeout_seconds,
                set_waiting=lambda waiting: self._set_waiting(record, waiting),
                change_tracker=changes,
                approval_mode=turn_config.approval_mode,
                task_state=task_state,
            )
            active_turn = _ActiveTurn(
                turn_id=turn_id,
                controller=controller,
                tools=tools,
                changes=changes,
                events=events,
                skills=skill_state,
                started_at=utc_now(),
                idempotency_key=idempotency_key,
            )
            record.active_turn = active_turn
            record.task_state = task_state
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
            # Explicit Skill activation happened during provider-free
            # preflight, but its durable UI events belong after the Turn
            # boundary.  This keeps the frontend's turn-local reset followed
            # by the actual loaded state, without fabricating a tool call.
            for event_type, payload in pending_skill_events:
                events.emit(event_type, payload)
            summary = await self._execute_active_turn(
                record,
                active_turn,
                model,
                provider,
                context_manager,
                task_state,
                user_text,
                turn_config,
            )
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
            try:
                record.status = (
                    ThreadStatus.CLOSED if record.closing else ThreadStatus.IDLE
                )
                record.active_turn = None
                record.task_state = None
                record.updated_at = utc_now()
                self._persist_record(record)
            finally:
                assert workspace_lease is not None
                self._workspace_leases.release(workspace_lease)
                try:
                    if record.closing:
                        await record.tools.aclose()
                finally:
                    await self._release_turn_provider(provider)

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

    def _context_manager_for(
        self,
        record: _ThreadRecord,
        turn_config: TurnConfig,
        provider_capabilities: ProviderCapabilities | None,
        *,
        skill_catalog: SkillCatalog | None = None,
        skill_state: SkillTurnState | None = None,
    ) -> ContextManager:
        """Create one request-context assembler for a frozen Turn config."""

        return ContextManager(
            base_system_instructions=self._base_system_instructions,
            project_instructions_provider=self._project_instructions_provider,
            working_tail_mode=(
                "structured_user_tail"
                if provider_capabilities is None
                else provider_capabilities.working_tail_mode
            ),
            budget=ContextBudget(
                context_window_tokens=(
                    (
                        provider_capabilities.context_window_tokens
                        if provider_capabilities is not None
                        else None
                    )
                    or self._default_context_window_tokens
                ),
                output_tokens=turn_config.max_tokens,
            ),
            skill_catalog=skill_catalog or record.skill_catalog,
            skill_state=skill_state,
        )

    def _provider_capabilities_for(
        self,
        turn_config: TurnConfig,
    ) -> ProviderCapabilities | None:
        """Read provider capabilities without constructing its HTTP client."""

        return self._provider_capabilities_for_values(
            turn_config.provider_config_id,
            turn_config.model,
        )

    def _provider_capabilities_for_values(
        self,
        provider_config_id: str,
        model: str,
    ) -> ProviderCapabilities | None:
        """Resolve candidate capabilities independently of Thread settings."""

        capabilities_for = getattr(self._provider_resolver, "capabilities_for", None)
        if not callable(capabilities_for):
            return None
        capabilities = capabilities_for(
            provider_config_id,
            model,
        )
        return capabilities if isinstance(capabilities, ProviderCapabilities) else None

    @staticmethod
    async def _release_turn_provider(provider: LLMProvider | None) -> None:
        if provider is None:
            return
        try:
            await provider.close()
        except Exception:
            # A transport pool remains the Host-level owner and will retry
            # cleanup during shutdown; one adapter release must not corrupt a
            # completed Turn transition.
            logger.exception("Failed to release Turn-scoped model provider")

    @staticmethod
    def _runtime_context_for(
        record: _ThreadRecord,
        turn_config: TurnConfig,
        *,
        turn_id: str | None = None,
    ) -> RuntimeContext:
        """Collect cheap environment facts without scanning the workspace."""

        return RuntimeContext(
            workspace=record.workspace,
            cwd=record.workspace,
            shell=os.environ.get("SHELL") or "/bin/sh",
            approval_mode=turn_config.approval_mode,
            capabilities=tuple(
                definition.name for definition in record.tools.definitions()
            ),
            turn_id=turn_id,
        )

    async def _execute_active_turn(
        self,
        record: _ThreadRecord,
        active_turn: _ActiveTurn,
        model: ModelInvoker,
        provider: LLMProvider,
        context_manager: ContextManager,
        task_state: TaskState,
        current_input: str,
        turn_config: TurnConfig,
    ) -> TurnSummary:
        controller = active_turn.controller
        history_compactor = LLMHistoryCompactor(
            provider,
            estimator=context_manager.budget.estimator,
            request_budget_tokens=context_manager.budget.usable_input_tokens,
        )

        def save_checkpoint(checkpoint: CompactionCheckpoint) -> None:
            self._save_checkpoint(record, checkpoint)

        def save_context_diagnostics(value: dict[str, object]) -> None:
            diagnostics = self._context_diagnostics(value)
            record.context_diagnostics = diagnostics

        try:
            outcome = await self._loop.run(
                record.conversation,
                active_turn.tools,
                model,
                active_turn.events,
                controller,
                context_manager=context_manager,
                runtime_context_factory=lambda: self._runtime_context_for(
                    record, turn_config, turn_id=active_turn.turn_id
                ),
                task_state=task_state,
                current_input=current_input,
                compaction_checkpoint=record.compaction_checkpoint,
                history_compactor=history_compactor,
                checkpoint_sink=save_checkpoint,
                context_diagnostics_sink=save_context_diagnostics,
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
            public_error = None
            if error.reason == "output_length":
                public_error = {
                    "code": "OUTPUT_TRUNCATED",
                    "message": (
                        "model output was truncated before the response completed"
                    ),
                }
            elif error.reason == "empty_response":
                public_error = {
                    "code": "EMPTY_RESPONSE",
                    "message": "model finished without any text or tool calls",
                }
            return self._finish_turn(
                record,
                active_turn,
                status=TurnStatus.LIMIT_REACHED,
                stop_reason=error.reason,
                final_text=controller.last_assistant_text,
                event_type="turn_limit_reached",
                error=public_error,
            )
        except asyncio.CancelledError:
            # Cancellation can arrive after a process-starting tool has returned,
            # while the next model invocation is active.  Reap every session
            # created by this Turn before sealing its event channel.
            active_turn.tools.cancel_owned_sessions()
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
        except CompactionError:
            return self._finish_turn(
                record,
                active_turn,
                status=TurnStatus.FAILED,
                stop_reason="context_compaction_failed",
                final_text=controller.last_assistant_text,
                event_type="turn_failed",
                error={
                    "code": "CONTEXT_COMPACTION_FAILED",
                    "message": "conversation history compaction failed",
                },
            )
        except Exception as error:
            logger.exception("Unexpected Turn failure")
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

    def get_context_diagnostics(self, thread_id: str) -> dict[str, object]:
        """Return the last bounded model-context decision for diagnostics."""

        return deepcopy(self._threads[thread_id].context_diagnostics)

    def capabilities_for(
        self,
        thread_id: str,
        *,
        provider_config_id: str | None = None,
        model: str | None = None,
    ) -> dict[str, object]:
        """Return a safe Host-facing projection for the Thread's model.

        Capability lookup deliberately uses the resolver's provider-independent
        hook.  If an embedder does not expose that hook, the conservative
        projection disables optional controls instead of guessing from names.
        """

        record = self._threads[thread_id]
        try:
            candidate_provider = (
                record.settings.provider_config_id
                if provider_config_id is None
                else provider_config_id
            )
            candidate_model = record.settings.model if model is None else model
            # Validate the candidate at this seam so a draft cannot silently
            # fall back to the current Thread's provider/model.
            candidate_settings = ModelSettings(
                provider_config_id=candidate_provider,
                model=candidate_model,
            )
            turn_config = TurnConfig.from_model_settings(
                candidate_settings,
                settings_version=record.settings.version,
                system_prompt=self._system_prompt,
                reasoning_visibility=self._reasoning_visibility,
            )
            capabilities = self._provider_capabilities_for(turn_config)
            if capabilities is None:
                # Lightweight test/embedder resolvers may not expose the
                # optional provider-independent hook.  Their provider object
                # is still the authoritative capability source; production
                # factories provide the hook above and therefore avoid
                # constructing a transport for this read-only endpoint.
                provider = self._provider_resolver(
                    candidate_provider,
                    candidate_model,
                )
                candidate = getattr(provider, "capabilities", None)
                capabilities = (
                    candidate if isinstance(candidate, ProviderCapabilities) else None
                )
        except Exception:
            capabilities = None
        thinking = None if capabilities is None else capabilities.thinking
        supported = bool(thinking is not None and thinking.supported)
        budget = bool(
            thinking is not None
            and thinking.supported
            and thinking.supports_budget_tokens
        )
        keep_values = () if thinking is None else thinking.supported_keep_values
        projection: dict[str, object] = {
            "thinking_supported": supported,
            "supports_thinking_budget": budget,
            "supported_keep_values": list(keep_values),
            "thinking": {
                "supported": supported,
                "supports_budget_tokens": budget,
                "supported_keep_values": list(keep_values),
            },
        }
        if thinking is not None and thinking.supported and (
            thinking.default_enabled
            or not bool(thinking.toggle_supported)
            or thinking.intensity_supported
            or capabilities.thinking_parameter_style
            is not ThinkingParameterStyle.GENERIC
        ):
            nested = projection["thinking"]
            assert isinstance(nested, dict)
            nested.update(
                {
                    "default_enabled": thinking.default_enabled,
                    "toggle_supported": bool(thinking.toggle_supported),
                    "intensity_supported": thinking.intensity_supported,
                    "intensity_options": list(thinking.intensity_options),
                    "default_intensity": thinking.default_intensity,
                }
            )
        if capabilities is not None and capabilities.context_window_tokens is not None:
            projection["context_window_tokens"] = capabilities.context_window_tokens
        return projection

    get_capabilities = capabilities_for

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
        # Expose the concrete reason (e.g. which workspace overlaps an active
        # Turn) without leaking internal exception types.
        detail = str(error) if str(error) else None
        return {
            "error": {
                "code": code,
                "message": message,
                "detail": detail,
            }
        }

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
        pending_approval = (
            None
            if record.active_turn is None
            else record.active_turn.tools.pending_approval()
        )
        loaded = () if record.active_turn is None else record.active_turn.skills.loaded
        loaded_names = {skill.name for skill in loaded}
        catalog = record.skill_catalog
        skills = {
            "schema_version": 1,
            # A Skill is shown in exactly one bucket while a Turn is active.
            # The full catalog remains owned by Runtime and is still used for
            # model context/tool resolution; this is only the Host/UI view.
            "available": [
                skill.metadata
                for skill in catalog.skills
                if skill.name not in loaded_names
            ],
            "loaded": (
                []
                if record.active_turn is None
                else record.active_turn.skills.loaded_metadata()
            ),
            "diagnostics": [diagnostic.to_dict() for diagnostic in catalog.diagnostics],
        }
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
            pending_approval=pending_approval,
            turns=deepcopy(record.turns),
            skills=skills,
            context_diagnostics=deepcopy(record.context_diagnostics),
        )

    @staticmethod
    def _context_diagnostics(value: dict[str, object]) -> dict[str, object]:
        """Keep a bounded, non-model-visible projection of ContextPlan facts."""

        keys = (
            "epoch_sections",
            "context_epoch_sections",
            "late_working_tail_sections",
            "working_tail_mode",
            "loaded_skill_names",
            "canonical_history_messages",
            "selected_history_messages",
            "compacted_history_messages",
            "history_estimate_tokens",
            "task_state_view_estimate_tokens",
            "tool_results_pruned",
            "checkpoint_validation",
            "checkpoint_reused",
            "checkpoint_covered_through",
            "compaction_performed",
            "final_request_estimate_tokens",
            "estimated_input_tokens",
            "final_request_fit",
            "final_fit",
            "pressure",
        )
        result: dict[str, object] = {}
        for key in keys:
            item = value.get(key)
            if isinstance(item, list):
                result[key] = [str(child)[:256] for child in item[:32]]
            elif isinstance(item, dict):
                result[key] = {
                    str(child_key)[:128]: str(child_value)[:512]
                    for child_key, child_value in list(item.items())[:32]
                }
            elif isinstance(item, (str, bool, int, float)) or item is None:
                result[key] = item
        return result

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
        if active_turn.idempotency_key is not None:
            submission = record.idempotent_submissions.get(active_turn.idempotency_key)
            if submission is not None:
                # Set the durable replay value before emitting the terminal
                # event.  The event sink then commits all terminal fields in a
                # single semantic ThreadStore transition.
                submission.summary = deepcopy(summary)
                submission.interrupted = False
        record.status = (
            ThreadStatus.CLOSED if record.closing else ThreadStatus.IDLE
        )
        record.active_turn = None
        active_turn.events.emit(
            event_type,
            {"summary": summary.to_dict()},
            checkpoint=False,
        )
        return summary
