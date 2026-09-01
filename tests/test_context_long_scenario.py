"""One deterministic end-to-end Context V2 lifecycle scenario.

The provider below is intentionally local and scripted.  It exercises the
Runtime-owned seams without requiring a paid or network model: two Turns,
multiple tool rounds, a validation failure surrounded by large output, two
rolling compactions, and a SQLite restart.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent.core.messages import Message, TextBlock, ToolCallBlock, ToolResultBlock
from agent.model.provider import LLMProvider
from agent.model.types import LLMRequest, LLMResponse, ProviderCapabilities, Usage
from agent.runtime import AllowAllPolicy, ModelSettings, ThreadRuntime, TurnStatus
from agent.runtime.thread_store import LocalThreadStore
from agent.tools.registry import ToolRegistry
from agent.tools.types import ToolDefinition, ToolResult


def _text(message: Message) -> str:
    return "".join(
        block.text for block in message.content if isinstance(block, TextBlock)
    )


def _final(text: str) -> LLMResponse:
    return LLMResponse(
        message=Message(role="assistant", content=[TextBlock(text=text)]),
        finish_reason="stop",
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _LifecycleProvider(LLMProvider):
    capabilities = ProviderCapabilities(context_window_tokens=6_500)

    def __init__(self) -> None:
        self.model_requests: list[LLMRequest] = []
        self.compaction_requests: list[LLMRequest] = []
        self._turn_model_calls: dict[str, int] = {}

    async def chat(self, request: LLMRequest) -> LLMResponse:
        system = "\n".join(
            _text(message)
            for message in request.messages
            if message.role == "system"
        )
        if "semantic history compactor" in system:
            self.compaction_requests.append(request)
            source = "\n".join(_text(message) for message in request.messages)
            validation = "validation failure retained" if "VALIDATION_FAILED" in source else ""
            return _final(
                "handoff: preserve the original constraint; "
                f"{validation}; continue the scope safely"
            )

        self.model_requests.append(request)
        all_text = "\n".join(_text(message) for message in request.messages)
        turn = "scope changed" if "scope changed" in all_text else "initial"
        count = self._turn_model_calls.get(turn, 0) + 1
        self._turn_model_calls[turn] = count
        if turn == "initial" and count == 1:
            return LLMResponse(
                message=Message(
                    role="assistant",
                    content=[
                        ToolCallBlock(
                            id="large-head",
                            name="large_result",
                            arguments={"marker": "head"},
                        ),
                        ToolCallBlock(
                            id="validation-failure",
                            name="validate",
                            arguments={},
                        ),
                        ToolCallBlock(
                            id="initial-plan",
                            name="update_plan",
                            arguments={
                                "steps": [
                                    {
                                        "step": "retain original constraint",
                                        "status": "in_progress",
                                    },
                                    {
                                        "step": "repair validation failure",
                                        "status": "pending",
                                    },
                                ]
                            },
                        ),
                        ToolCallBlock(
                            id="tool-skill",
                            name="skill",
                            arguments={"name": "beta"},
                        ),
                    ],
                ),
                finish_reason="tool_calls",
                usage=Usage(),
            )
        if turn == "initial" and count == 2:
            return LLMResponse(
                message=Message(
                    role="assistant",
                    content=[
                        ToolCallBlock(
                            id="large-tail",
                            name="large_result",
                            arguments={"marker": "tail"},
                        ),
                        ToolCallBlock(
                            id="second-plan",
                            name="update_plan",
                            arguments={
                                "steps": [
                                    {
                                        "step": "retain original constraint",
                                        "status": "completed",
                                    },
                                    {
                                        "step": "repair validation failure",
                                        "status": "in_progress",
                                    },
                                ]
                            },
                        ),
                    ],
                ),
                finish_reason="tool_calls",
                usage=Usage(),
            )
        if turn == "scope changed" and count == 1:
            return LLMResponse(
                message=Message(
                    role="assistant",
                    content=[
                        ToolCallBlock(
                            id="scope-result",
                            name="large_result",
                            arguments={"marker": "scope"},
                        ),
                        ToolCallBlock(
                            id="scope-plan",
                            name="update_plan",
                            arguments={
                                "steps": [
                                    {
                                        "step": "re-evaluate changed scope",
                                        "status": "in_progress",
                                    }
                                ]
                            },
                        ),
                    ],
                ),
                finish_reason="tool_calls",
                usage=Usage(),
            )
        return _final("lifecycle complete")


def _tools(_: Path) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="large_result",
            description="Return bounded pressure-sized output.",
            parameters={
                "type": "object",
                "properties": {"marker": {"type": "string"}},
                "required": ["marker"],
                "additionalProperties": False,
            },
        ),
        lambda arguments: ToolResult(
            content=(
                f"{arguments['marker']} output begins\n"
                + "x" * 5_000
                + f"\n{arguments['marker']} output ends"
            ),
            metadata={"paths": [f"src/{index}.py" for index in range(100)]},
        ),
    )
    registry.register(
        ToolDefinition(
            name="validate",
            description="Run deterministic validation.",
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        lambda _: ToolResult(
            content=(
                "pytest output begins\n"
                + "noise " * 200
                + "\nAssertionError: expected repaired output, actual stale output\n"
                + "pytest output ends"
            ),
            metadata={
                "command": "pytest -q tests/validation.py",
                "status": "failed",
                "exit_code": 1,
                "paths": [f"tests/{index}.py" for index in range(100)],
            },
            error_code="VALIDATION_FAILED",
        ),
    )
    return registry


def test_long_context_lifecycle_survives_two_compactions_and_sqlite_reload(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name, description, body in (
        ("alpha", "Explicit workflow", "Alpha instructions"),
        ("beta", "Tool workflow", "Beta instructions"),
    ):
        skill = workspace / ".agents" / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n{body}",
            encoding="utf-8",
        )

    database = tmp_path / "state" / "threads.sqlite3"
    store = LocalThreadStore(database)
    provider = _LifecycleProvider()
    runtime = ThreadRuntime(
        tool_registry_factory=_tools,
        provider_resolver=lambda _provider, _model: provider,
        default_settings=ModelSettings(provider_config_id="provider", model="model"),
        tool_policy=AllowAllPolicy(),
        store=store,
    )
    thread = runtime.create_thread(workspace)

    first = asyncio.run(
        asyncio.wait_for(
            runtime.run_turn(thread.thread_id, "Keep the original constraint. Use $alpha."),
            timeout=10,
        )
    )
    assert first.status is TurnStatus.COMPLETED
    assert len(provider.compaction_requests) >= 1
    first_events = runtime.get_events(thread.thread_id).events
    assert any(
        event.type == "skill_loaded" and event.payload.get("name") == "alpha"
        for event in first_events
    )
    assert any(
        event.type == "skill_loaded" and event.payload.get("name") == "beta"
        for event in first_events
    )

    persisted = store.get_thread(thread.thread_id)
    assert persisted is not None and persisted.checkpoint is not None
    assert persisted.checkpoint.canonical_fingerprint
    assert any(
        isinstance(block, ToolResultBlock)
        and block.error_code == "VALIDATION_FAILED"
        and "salient_evidence" in block.metadata
        for message in persisted.messages
        for block in message.content
    )
    assert len(persisted.checkpoint.summary.text) < 2_000

    asyncio.run(runtime.aclose())
    store.close()

    restored_store = LocalThreadStore(database)
    restored = ThreadRuntime(
        tool_registry_factory=_tools,
        provider_resolver=lambda _provider, _model: provider,
        default_settings=ModelSettings(provider_config_id="provider", model="model"),
        tool_policy=AllowAllPolicy(),
        store=restored_store,
    )
    restored_thread = restored.open_thread(thread.thread_id)
    assert restored_thread.thread_id == thread.thread_id
    assert restored_thread.skills is not None
    assert all("body" not in skill for skill in restored_thread.skills["available"])
    assert restored.get_snapshot(thread.thread_id).context_diagnostics == {}

    second = asyncio.run(
        restored.run_turn(thread.thread_id, "The scope changed; re-evaluate only the new scope.")
    )
    assert second.status is TurnStatus.COMPLETED
    assert len(provider.compaction_requests) >= 2
    fresh_snapshot = restored.get_snapshot(thread.thread_id)
    assert fresh_snapshot.completed_turns == 2
    assert fresh_snapshot.skills is not None
    assert fresh_snapshot.skills["loaded"] == []
    diagnostics = fresh_snapshot.context_diagnostics
    assert diagnostics["checkpoint_validation"] in {"reused", "new", "invalidated"}
    assert diagnostics["final_request_fit"] == "fits"
    assert diagnostics["late_working_tail_sections"] == ["task_state"]

    final_requests = [
        request
        for request in provider.model_requests
        if any("scope changed" in _text(message) for message in request.messages)
    ]
    assert final_requests
    final_text = "\n".join(_text(message) for message in final_requests[-1].messages)
    assert "handoff" in final_text or "validation failure" in final_text
    skill_events = [
        event
        for event in restored.get_events(thread.thread_id).events
        if event.type == "skill_loaded"
    ]
    assert {event.payload.get("name") for event in skill_events} == {"alpha", "beta"}
    assert len({event.turn_id for event in skill_events}) == 1
    restored_store.close()
