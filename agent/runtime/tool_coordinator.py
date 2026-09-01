"""Sequential tool execution, Policy decisions and external approval."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
import inspect
from uuid import uuid4

from agent.core.messages import ToolCallBlock
from agent.tools.registry import ToolRegistry
from agent.tools.types import ToolDefinition, ToolResult

from .conversation import Conversation
from .change_tracker import ChangeTracker
from .task_state import TaskState
from .evidence import evidence_from_tool_execution
from .errors import ApprovalTimeoutError, TurnLimitReached
from .events import TurnEventEmitter, public_tool_call, utc_now
from .policy import PolicyDecision, PolicyResult, ToolPolicy
from .run_controller import RunController
from .settings import ApprovalMode
from agent.tools.result_bounds import reduce_tool_result


@dataclass(frozen=True, slots=True)
class _PendingApproval:
    approval_id: str
    future: asyncio.Future[bool]
    tool_call: dict[str, object]
    timeout_seconds: float
    decision: str
    reason_code: str
    message: str
    execution_profile: str


class ToolCoordinator:
    """Keep tool lifecycle and approval state out of the Agent Loop."""

    def __init__(
        self,
        *,
        turn_id: str,
        registry: ToolRegistry,
        conversation: Conversation,
        events: TurnEventEmitter,
        controller: RunController,
        policy: ToolPolicy,
        approval_timeout_seconds: float,
        set_waiting: Callable[[bool], None],
        change_tracker: ChangeTracker,
        approval_mode: ApprovalMode = ApprovalMode.ON_REQUEST,
        task_state: TaskState | None = None,
    ) -> None:
        self._turn_id = turn_id
        self._registry = registry
        self._conversation = conversation
        self._events = events
        self._controller = controller
        self._policy = policy
        self._approval_timeout_seconds = approval_timeout_seconds
        self._set_waiting = set_waiting
        self._change_tracker = change_tracker
        self._approval_mode = approval_mode
        self._task_state = task_state
        self._pending_approval: _PendingApproval | None = None

    def definitions(self) -> list[ToolDefinition]:
        return self._registry.definitions()

    def pending_approval(self) -> dict[str, object] | None:
        """Return the detached actionable approval state for Thread snapshots."""

        pending = self._pending_approval
        if pending is None or pending.future.done():
            return None
        return deepcopy(
            {
                "approval_id": pending.approval_id,
                "tool_call": pending.tool_call,
                "timeout_seconds": pending.timeout_seconds,
                "decision": pending.decision,
                "reason_code": pending.reason_code,
                "message": pending.message,
                "execution_profile": pending.execution_profile,
            }
        )

    def cancel_owned_sessions(self) -> None:
        """Terminate persistent process sessions created by this Turn."""

        self._registry.cancel_active_sessions(self._turn_id)

    async def execute(self, calls: list[ToolCallBlock]) -> None:
        for index, call in enumerate(calls):
            self._events.tool_requested(call)
            try:
                self._controller.begin_tool()
            except TurnLimitReached:
                self._append_skipped(
                    calls[index:],
                    reason="tool call budget reached",
                    first_request_emitted=True,
                )
                raise

            try:
                result = await self._execute_one(call)
            except ApprovalTimeoutError:
                self._abort_batch(
                    call,
                    ToolResult(
                        content="tool approval timed out",
                        metadata={"executed": False},
                        error_code="APPROVAL_TIMEOUT",
                    ),
                    calls[index + 1 :],
                    reason="tool approval timed out",
                    error_code="APPROVAL_TIMEOUT",
                )
                raise
            except TurnLimitReached:
                self._abort_batch(
                    call,
                    ToolResult(
                        content=(
                            "tool cancelled because the Turn execution deadline "
                            "was reached"
                        ),
                        metadata={},
                        error_code="LIMIT_REACHED",
                    ),
                    calls[index + 1 :],
                    reason="Turn execution deadline reached",
                )
                raise
            except asyncio.CancelledError:
                self._abort_batch(
                    call,
                    ToolResult(
                        content="tool cancelled with its active Turn",
                        metadata={},
                        error_code="CANCELLED",
                    ),
                    calls[index + 1 :],
                    reason="Turn cancelled",
                    error_code="CANCELLED",
                )
                raise
            except Exception:
                self._abort_batch(
                    call,
                    ToolResult(
                        content="tool coordination failed",
                        metadata={"executed": False},
                        error_code="INTERNAL_ERROR",
                    ),
                    calls[index + 1 :],
                    reason="tool coordination failed",
                    error_code="INTERNAL_ERROR",
                )
                raise

            self._record(call, result)
            try:
                self._controller.record_tool_result(call, result)
                self._controller.checkpoint()
            except TurnLimitReached as error:
                self._append_skipped(
                    calls[index + 1 :],
                    reason=(
                        "repeated tool failure limit reached"
                        if error.reason == "repeated_tool_failure"
                        else "Turn execution deadline reached"
                    ),
                )
                raise
            except asyncio.CancelledError:
                self._append_skipped(
                    calls[index + 1 :],
                    reason="Turn cancelled",
                    error_code="CANCELLED",
                )
                raise

    def resolve_approval(self, approval_id: str, approved: bool) -> bool:
        pending = self._pending_approval
        if (
            pending is None
            or pending.future.done()
            or approval_id != pending.approval_id
        ):
            return False
        pending.future.set_result(approved)
        return True

    async def _execute_one(self, call: ToolCallBlock) -> ToolResult:
        policy_result = self._decide(call)
        if policy_result.decision is PolicyDecision.DENY:
            return self._denied_result(policy_result)
        if policy_result.decision is PolicyDecision.REQUIRE_APPROVAL:
            if not await self._request_approval(call, policy_result):
                return self._denied_result(policy_result, approval_denied=True)
        set_context = getattr(self._registry, "set_session_context", None)
        if callable(set_context):
            set_context(turn_id=self._turn_id)
        self._registry.set_event_sink(self._events.emit)
        execution_profile = policy_result.execution_profile
        self._events.tool_started(call)
        conflict = self._change_tracker.before_execution(call)
        if conflict is not None:
            return conflict
        changes_before = len(self._change_tracker.changes())
        try:
            execute_async = self._registry.execute_async
            try:
                parameters = inspect.signature(execute_async).parameters.values()
            except (TypeError, ValueError):
                parameters = ()
            accepts_profile = any(
                parameter.name == "execution_profile"
                or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
            execution = (
                execute_async(call, execution_profile=execution_profile)
                if accepts_profile
                else execute_async(call)
            )
            result = await self._controller.wait(execution)
        except (TurnLimitReached, asyncio.CancelledError):
            self.cancel_owned_sessions()
            self._change_tracker.execution_interrupted(call)
            raise
        self._change_tracker.after_execution(call, result)
        changes_after = self._change_tracker.changes()
        new_changes = changes_after[changes_before:]
        if new_changes:
            # Keep only bounded path/type facts in the ToolResult metadata;
            # full diffs remain in ChangeTracker and TurnSummary.
            result.metadata = {
                **result.metadata,
                "changed_paths": [
                    change["path"]
                    for change in new_changes
                    if isinstance(change.get("path"), str)
                ],
                "change_types": [
                    change["change_type"]
                    for change in new_changes
                    if isinstance(change.get("change_type"), str)
                ],
            }
        return result

    async def _request_approval(
        self,
        call: ToolCallBlock,
        policy_result: PolicyResult,
    ) -> bool:
        loop = asyncio.get_running_loop()
        approval_id = str(uuid4())
        approval = loop.create_future()
        self._pending_approval = _PendingApproval(
            approval_id=approval_id,
            future=approval,
            tool_call=public_tool_call(call),
            timeout_seconds=self._approval_timeout_seconds,
            decision=policy_result.decision.value,
            reason_code=policy_result.reason_code,
            message=policy_result.message,
            execution_profile=policy_result.profile.value,
        )
        self._controller.pause_deadline()
        self._set_waiting(True)
        self._events.emit(
            "approval_requested",
            {
                "approval_id": approval_id,
                "tool_call": public_tool_call(call),
                "timeout_seconds": self._approval_timeout_seconds,
                "decision": policy_result.decision.value,
                "reason_code": policy_result.reason_code,
                "message": policy_result.message,
                "execution_profile": policy_result.profile.value,
            },
        )
        resolution = "cancelled"
        try:
            approved = await asyncio.wait_for(
                approval,
                timeout=self._approval_timeout_seconds,
            )
            resolution = "approved" if approved else "denied"
            return approved
        except TimeoutError as error:
            resolution = "timeout"
            raise ApprovalTimeoutError from error
        finally:
            self._pending_approval = None
            self._set_waiting(False)
            self._controller.resume_deadline()
            self._events.emit(
                "approval_resolved",
                {
                    "approval_id": approval_id,
                    "resolution": resolution,
                    "reason_code": policy_result.reason_code,
                },
            )

    def _decide(self, call: ToolCallBlock) -> PolicyResult:
        decide = self._policy.decide
        try:
            parameters = inspect.signature(decide).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        accepts_approval_mode = any(
            parameter.name == "approval_mode"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if accepts_approval_mode:
            decision = decide(call, approval_mode=self._approval_mode)
        else:
            decision = decide(call)
        if isinstance(decision, PolicyResult):
            return decision
        if isinstance(decision, PolicyDecision):
            return PolicyResult(
                decision,
                "POLICY_DECISION",
                "tool decision returned by policy",
            )
        raise ValueError("tool policy must return a PolicyResult or PolicyDecision")

    @staticmethod
    def _denied_result(
        policy_result: PolicyResult,
        *,
        approval_denied: bool = False,
    ) -> ToolResult:
        reason_code = "APPROVAL_DENIED" if approval_denied else policy_result.reason_code
        return ToolResult(
            content="tool call denied by Policy",
            metadata={
                "executed": False,
                "reason_code": reason_code,
                "policy_reason_code": policy_result.reason_code,
                "policy_message": policy_result.message,
            },
            error_code="POLICY_DENIED",
        )

    def _append_skipped(
        self,
        calls: list[ToolCallBlock],
        *,
        reason: str,
        error_code: str = "LIMIT_REACHED",
        first_request_emitted: bool = False,
    ) -> None:
        for index, call in enumerate(calls):
            if index > 0 or not first_request_emitted:
                self._events.tool_requested(call)
            self._record(
                call,
                ToolResult(
                    content=reason,
                    metadata={"executed": False},
                    error_code=error_code,
                ),
            )

    def _abort_batch(
        self,
        call: ToolCallBlock,
        result: ToolResult,
        remaining_calls: list[ToolCallBlock],
        *,
        reason: str,
        error_code: str = "LIMIT_REACHED",
    ) -> None:
        self._record(call, result)
        self._append_skipped(
            remaining_calls,
            reason=reason,
            error_code=error_code,
        )

    def _record(self, call: ToolCallBlock, result: ToolResult) -> None:
        # Extract semantic facts from the complete in-memory result before the
        # shared Layer-1 reducer can head/tail bound it.  The extractor keeps
        # only bounded summaries; neither raw stdout nor stderr is retained in
        # TaskState.
        evidence_values = []
        if self._task_state is not None:
            try:
                evidence_values = evidence_from_tool_execution(
                    call,
                    result,
                    timestamp=utc_now(),
                )
            except (TypeError, ValueError):
                evidence_values = []
        bounded = reduce_tool_result(result)
        # Preserve internal cancellation bookkeeping while replacing the
        # model-visible result with its bounded detached value.
        bounded._settled_after_cancellation = result._settled_after_cancellation
        self._conversation.append_tool_result(bounded.to_message_block(call.id))
        self._events.tool_finished(call, bounded)
        if self._task_state is not None:
            for evidence in evidence_values:
                self._task_state.record_evidence(evidence)
                self._events.emit(
                    "task_evidence_recorded",
                    {"evidence": evidence.to_dict()},
                )
