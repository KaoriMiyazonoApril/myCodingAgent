"""In-memory Thread runtime and the minimal readable ReAct loop."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from agent.core.messages import Message, TextBlock
from agent.model.provider import LLMProvider
from agent.tools.registry import ToolRegistry

from .errors import ThreadBusyError
from .loop import AgentLoop
from .types import ThreadSnapshot, ThreadStatus, TurnStatus, TurnSummary


ToolRegistryFactory = Callable[[Path], ToolRegistry]

DEFAULT_SYSTEM_PROMPT = (
    "You are a local coding agent. Inspect relevant files before modifying them, "
    "use the provided tools, validate completed work, and report only actions that "
    "actually succeeded."
)


@dataclass(slots=True)
class _ThreadRecord:
    thread_id: str
    workspace: Path
    tools: ToolRegistry
    messages: list[Message]
    status: ThreadStatus = ThreadStatus.IDLE
    active_turn_id: str | None = None
    completed_turns: int = 0


class ThreadRuntime:
    """Create in-memory Threads and run one ReAct Turn at a time."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        tool_registry_factory: ToolRegistryFactory,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ) -> None:
        self._tool_registry_factory = tool_registry_factory
        self._system_prompt = system_prompt
        self._loop = AgentLoop(provider)
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
            messages=[
                Message(
                    role="system",
                    content=[TextBlock(text=self._system_prompt)],
                )
            ],
        )
        self._threads[thread_id] = record
        return self._snapshot(record)

    async def run_turn(self, thread_id: str, user_text: str) -> TurnSummary:
        record = self._threads[thread_id]
        if record.status is not ThreadStatus.IDLE:
            raise ThreadBusyError(f"thread already has an active turn: {thread_id}")
        turn_id = str(uuid4())
        record.status = ThreadStatus.RUNNING
        record.active_turn_id = turn_id
        record.messages.append(
            Message(role="user", content=[TextBlock(text=user_text)])
        )
        try:
            outcome = await self._loop.run(record.messages, record.tools)
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

    @staticmethod
    def _snapshot(record: _ThreadRecord) -> ThreadSnapshot:
        return ThreadSnapshot(
            thread_id=record.thread_id,
            workspace=record.workspace.as_posix(),
            status=record.status,
            active_turn_id=record.active_turn_id,
            completed_turns=record.completed_turns,
        )
