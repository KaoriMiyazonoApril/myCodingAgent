"""Sequential tool execution, Policy decisions and external approval."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from agent.core.messages import ToolCallBlock
from agent.tools.registry import ToolRegistry
from agent.tools.types import ToolDefinition, ToolResult

from .conversation import Conversation
from .change_tracker import ChangeTracker
from .errors import ApprovalTimeoutError, TurnLimitReached
from .events import TurnEventEmitter, public_tool_call
from .policy import PolicyDecision, PolicyResult, ToolPolicy
from .run_controller import RunController
from .settings import ApprovalMode


@dataclass(frozen=True, slots=True)
class _PendingApproval:
    approval_id: str
    future: asyncio.Future[bool]


class ToolCoordinator:
    """Keep tool lifecycle and approval state out of the Agent Loop."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        conversation: Conversation,
        events: TurnEventEmitter,
        controller: RunController,
        policy: ToolPolicy,
        approval_timeout_seconds: float,
        set_waiting: Callable[[bool], None],
        change_tracker: ChangeTracker,
        approval_mode: ApprovalMode = ApprovalMode.ON_REQUEST,
    ) -> None:
        self._registry = registry
        self._conversation = conversation
        self._events = events
        self._controller = controller
        self._policy = policy
        self._approval_timeout_seconds = approval_timeout_seconds
        self._set_waiting = set_waiting
        self._change_tracker = change_tracker
        self._approval_mode = approval_mode
        self._pending_approval: _PendingApproval | None = None

    def definitions(self) -> list[ToolDefinition]:
        return self._registry.definitions()

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
        self._events.tool_started(call)
        conflict = self._change_tracker.before_execution(call)
        if conflict is not None:
            return conflict
        try:
            result = await self._controller.wait(self._registry.execute_async(call))
        except (TurnLimitReached, asyncio.CancelledError):
            self._change_tracker.execution_interrupted(call)
            raise
        self._change_tracker.after_execution(call, result)
        return result

    async def _request_approval(
        self,
        call: ToolCallBlock,
        policy_result: PolicyResult,
    ) -> bool:
        loop = asyncio.get_running_loop()
        approval_id = str(uuid4())
        approval = loop.create_future()
        self._pending_approval = _PendingApproval(approval_id, approval)
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
            decision = decide(call, approval_mode=self._approval_mode)
        except TypeError as error:
            # Existing custom policies predate command-aware context. Keep the
            # public seam source-compatible without hiding other exceptions.
            try:
                decision = decide(call)
            except TypeError:
                raise error
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
        self._conversation.append_tool_result(result.to_message_block(call.id))
        self._events.tool_finished(call, result)
