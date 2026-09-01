from __future__ import annotations

import asyncio
from pathlib import Path

from agent.core.messages import Message, TextBlock, ToolCallBlock, ToolResultBlock
from agent.model.provider import LLMProvider
from agent.model.types import LLMRequest, LLMResponse, ProviderCapabilities, Usage
from agent.runtime import ModelSettings, ThreadRuntime
from agent.tools.registry import ToolRegistry


class _Provider(LLMProvider):
    capabilities = ProviderCapabilities(context_window_tokens=32_000)

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = iter(responses)
        self.requests: list[LLMRequest] = []

    async def chat(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return next(self.responses)


def _write_skill(workspace: Path) -> None:
    skill = workspace / ".agents" / "skills" / "alpha"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Alpha workflow\n---\n\nUse alpha workflow.",
        encoding="utf-8",
    )


def _runtime(provider: _Provider) -> ThreadRuntime:
    return ThreadRuntime(
        tool_registry_factory=lambda _: ToolRegistry(),
        provider_resolver=lambda _provider_id, _model: provider,
        default_settings=ModelSettings(provider_config_id="provider", model="model"),
    )


def _text(message: Message) -> str:
    return "".join(
        block.text for block in message.content if isinstance(block, TextBlock)
    )


def test_explicit_skill_loads_before_first_model_without_fake_tool_history(tmp_path: Path) -> None:
    _write_skill(tmp_path)
    provider = _Provider(
        [
            LLMResponse(
                message=Message(
                    role="assistant",
                    content=[TextBlock(text="used alpha")],
                ),
                finish_reason="stop",
                usage=Usage(),
            )
        ]
    )
    runtime = _runtime(provider)
    thread = runtime.create_thread(tmp_path)

    asyncio.run(runtime.run_turn(thread.thread_id, "Use $alpha now."))

    first_request = provider.requests[0]
    serialized = "\n".join(_text(message) for message in first_request.messages)
    assert "Use alpha workflow." in serialized
    assert "Alpha workflow" in serialized
    assert not any(
        isinstance(block, ToolCallBlock)
        for message in first_request.messages
        for block in message.content
    )
    assert not any(
        isinstance(block, ToolResultBlock)
        for message in first_request.messages
        for block in message.content
    )
    events = runtime.get_events(thread.thread_id)
    assert [event.type for event in events.events].count("skill_loaded") == 1
    assert "Use $alpha now." in [
        block["text"]
        for message in runtime.get_snapshot(thread.thread_id).to_dict()["messages"]
        for block in message["content"]
        if block.get("type") == "text"
    ]


def test_model_skill_tool_has_real_pair_without_duplicate_body_projection(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path)
    provider = _Provider(
        [
            LLMResponse(
                message=Message(
                    role="assistant",
                    content=[
                        ToolCallBlock(
                            id="skill-call",
                            name="skill",
                            arguments={"name": "alpha"},
                        )
                    ],
                ),
                finish_reason="tool_calls",
                usage=Usage(),
            ),
            LLMResponse(
                message=Message(
                    role="assistant",
                    content=[TextBlock(text="done")],
                ),
                finish_reason="stop",
                usage=Usage(),
            ),
        ]
    )
    runtime = _runtime(provider)
    thread = runtime.create_thread(tmp_path)

    asyncio.run(runtime.run_turn(thread.thread_id, "Load alpha."))

    assert len(provider.requests) == 2
    first_history = provider.requests[0].messages
    second_history = provider.requests[1].messages
    assert any(
        isinstance(block, ToolResultBlock)
        and "Use alpha workflow." in block.content
        for message in second_history
        for block in message.content
    )
    late_text = "\n".join(_text(message) for message in second_history[-1:])
    assert "loaded_skills:" not in late_text
    assert "Use alpha workflow." not in late_text
    assert sum(
        isinstance(block, ToolCallBlock)
        for message in second_history
        for block in message.content
    ) == 1
    events = runtime.get_events(thread.thread_id)
    assert [event.type for event in events.events].count("skill_loaded") == 1
    assert any(
        event.type == "skill_loaded" and event.payload.get("name") == "alpha"
        for event in events.events
    )
