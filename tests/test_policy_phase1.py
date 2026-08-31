from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from agent.core.messages import Message, TextBlock, ToolCallBlock, ToolResultBlock
from agent.model.provider import LLMProvider
from agent.model.types import LLMRequest, LLMResponse, Usage
from agent.runtime import (
    ApprovalMode,
    CommandAwarePolicy,
    ExecClassification,
    ExecutionProfile,
    ModelSettings,
    PolicyDecision,
    PolicyResult,
    ThreadRuntime,
    TurnConfig,
    TurnSettingsOverride,
    classify_exec_command,
)
from agent.tools.registry import ToolRegistry
from agent.tools.types import ToolDefinition, ToolResult


def _call(command: str, *, tty: bool = False) -> ToolCallBlock:
    return ToolCallBlock(
        id="call-1",
        name="exec_command",
        arguments={"command": command, "tty": tty},
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("pwd", ExecClassification.SAFE_READ_ONLY),
        ("git diff --stat", ExecClassification.SAFE_READ_ONLY),
        ("pytest -q", ExecClassification.TEST_BUILD),
        ("npm run build", ExecClassification.TEST_BUILD),
        ("python -m my_tool", ExecClassification.ORDINARY_SANDBOXED),
        ("python -m pip install requests", ExecClassification.PACKAGE_INSTALL),
        ("python3 -m pip install requests", ExecClassification.PACKAGE_INSTALL),
        ("env pip install requests", ExecClassification.PACKAGE_INSTALL),
        ("/usr/bin/env pip install requests", ExecClassification.PACKAGE_INSTALL),
        ("env FOO=bar rm -rf build", ExecClassification.DESTRUCTIVE),
        ("python -c 'print(1)'", ExecClassification.DYNAMIC_INTERPRETER),
        ("python3 -c 'print(1)'", ExecClassification.DYNAMIC_INTERPRETER),
        ("python -", ExecClassification.DYNAMIC_INTERPRETER),
        ("python3 -", ExecClassification.DYNAMIC_INTERPRETER),
        ("xargs rm", ExecClassification.DESTRUCTIVE),
        ("xargs sh", ExecClassification.INTERACTIVE),
        ("xargs bash", ExecClassification.INTERACTIVE),
        ("xargs -n 1 rm", ExecClassification.DESTRUCTIVE),
        ("xargs", ExecClassification.UNKNOWN),
        ("rm -rf build", ExecClassification.DESTRUCTIVE),
        ("find . -delete", ExecClassification.DESTRUCTIVE),
        ("git branch -D old", ExecClassification.DESTRUCTIVE),
        ("git branch -f main old", ExecClassification.DESTRUCTIVE),
        ("git branch new", ExecClassification.ORDINARY_SANDBOXED),
        ("git branch -m old new", ExecClassification.ORDINARY_SANDBOXED),
        ("git branch --list", ExecClassification.SAFE_READ_ONLY),
        ("cat source.txt > generated.txt", ExecClassification.COMPLEX_SHELL),
        ("curl https://example.test", ExecClassification.NETWORK),
        ("npm install", ExecClassification.PACKAGE_INSTALL),
        ("sudo ls", ExecClassification.PRIVILEGED),
        ("python", ExecClassification.INTERACTIVE),
        ("bash -c 'echo hi'", ExecClassification.COMPLEX_SHELL),
        ("echo hi && echo bye", ExecClassification.COMPLEX_SHELL),
        ("", ExecClassification.UNKNOWN),
    ],
)
def test_exec_classifier_is_conservative(command, expected) -> None:
    assert classify_exec_command(command) is expected


def test_tty_and_interactive_shell_are_classified_as_interactive() -> None:
    assert CommandAwarePolicy().decide(_call("echo ready", tty=True)).decision is PolicyDecision.REQUIRE_APPROVAL
    assert classify_exec_command("bash") is ExecClassification.INTERACTIVE


@pytest.mark.parametrize("mode", list(ApprovalMode))
def test_safe_read_only_is_allowed_in_every_approval_mode(mode) -> None:
    result = CommandAwarePolicy().decide(_call("git status"), approval_mode=mode)
    assert result == PolicyResult(PolicyDecision.ALLOW, "SAFE_READ_ONLY", "read-only command allowed")


@pytest.mark.parametrize("mode", [ApprovalMode.UNTRUSTED, ApprovalMode.ON_REQUEST])
def test_dangerous_commands_require_approval_unless_never(mode) -> None:
    result = CommandAwarePolicy().decide(_call("rm -rf build"), approval_mode=mode)
    assert result.decision is PolicyDecision.REQUIRE_APPROVAL
    assert result.reason_code == "DESTRUCTIVE_COMMAND"


def test_never_denies_dangerous_commands_with_structured_reason() -> None:
    result = CommandAwarePolicy().decide(
        _call("rm -rf build"), approval_mode=ApprovalMode.NEVER
    )
    assert result == PolicyResult(
        PolicyDecision.DENY,
        "DESTRUCTIVE_COMMAND_NEVER",
        "commands requiring approval are disabled by approval mode",
    )


@pytest.mark.parametrize("command", ["pytest -q", "python -m my_tool"])
def test_never_still_allows_test_and_ordinary_sandboxed_commands(command) -> None:
    result = CommandAwarePolicy().decide(
        _call(command), approval_mode=ApprovalMode.NEVER
    )
    assert result.decision is PolicyDecision.ALLOW


def test_legacy_run_command_complex_shell_still_requires_approval() -> None:
    result = CommandAwarePolicy().decide(
        ToolCallBlock(
            id="legacy",
            name="run_command",
            arguments={"command": "echo one && echo two"},
        ),
        approval_mode=ApprovalMode.ON_REQUEST,
    )
    assert result.decision is PolicyDecision.REQUIRE_APPROVAL


def test_non_command_tools_are_allowed_without_risk_classification() -> None:
    result = CommandAwarePolicy().decide(
        ToolCallBlock(id="file", name="read_file", arguments={"path": "a.txt"}),
        approval_mode=ApprovalMode.NEVER,
    )
    assert result.decision is PolicyDecision.ALLOW
    assert result.reason_code == "NON_COMMAND_TOOL"


@pytest.mark.parametrize(
    ("command", "profile"),
    [
        ("git status", ExecutionProfile.READ_ONLY),
        ("pytest -q", ExecutionProfile.WORKSPACE_WRITE),
        ("curl https://example.test", ExecutionProfile.WORKSPACE_WRITE_NETWORK),
        ("npm install", ExecutionProfile.WORKSPACE_WRITE_NETWORK),
    ],
)
def test_policy_result_selects_minimum_execution_profile(command, profile) -> None:
    result = CommandAwarePolicy().decide(_call(command))
    assert result.execution_profile is profile


def test_privileged_command_is_denied_even_when_approval_is_available() -> None:
    result = CommandAwarePolicy().decide(_call("sudo ls"))
    assert result.decision is PolicyDecision.DENY
    assert result.reason_code == "PRIVILEGED_COMMAND_UNSUPPORTED"


@pytest.mark.parametrize("mode", [ApprovalMode.UNTRUSTED, ApprovalMode.ON_REQUEST])
def test_dynamic_interpreter_requires_approval(mode) -> None:
    result = CommandAwarePolicy().decide(
        _call("python3 -c 'print(1)'"), approval_mode=mode
    )
    assert result.decision is PolicyDecision.REQUIRE_APPROVAL
    assert result.reason_code == "DYNAMIC_INTERPRETER"


def test_dynamic_interpreter_is_denied_in_never_mode() -> None:
    result = CommandAwarePolicy().decide(
        _call("python -"), approval_mode=ApprovalMode.NEVER
    )
    assert result.decision is PolicyDecision.DENY
    assert result.reason_code == "DYNAMIC_INTERPRETER_NEVER"


def test_turn_config_freezes_approval_mode_and_override_does_not_mutate_defaults() -> None:
    defaults = ModelSettings(
        provider_config_id="p",
        model="m",
        approval_mode=ApprovalMode.UNTRUSTED,
    )
    config = TurnConfig.from_model_settings(
        defaults,
        settings_version=0,
        system_prompt="system",
        reasoning_visibility="hidden",
    )
    assert config.approval_mode is ApprovalMode.UNTRUSTED
    with pytest.raises(FrozenInstanceError):
        config.approval_mode = ApprovalMode.NEVER

    override = TurnSettingsOverride(approval_mode=ApprovalMode.NEVER)
    changed = override.apply(defaults)
    assert changed.approval_mode is ApprovalMode.NEVER
    assert defaults.approval_mode is ApprovalMode.UNTRUSTED


class _Provider(LLMProvider):
    async def chat(self, request: LLMRequest) -> LLMResponse:
        if any(message.role == "tool" for message in request.messages):
            return LLMResponse(
                message=Message(role="assistant", content=[TextBlock(text="done")]),
                finish_reason="stop",
                usage=Usage(),
            )
        return LLMResponse(
            message=Message(
                role="assistant",
                content=[
                    ToolCallBlock(
                        id="danger",
                        name="exec_command",
                        arguments={"command": "rm -rf build", "cwd": ".", "tty": False},
                    )
                ],
            ),
            finish_reason="tool_calls",
            usage=Usage(),
        )


def _registry(_: object) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="exec_command",
            description="run",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string", "default": "."},
                    "tty": {"type": "boolean", "default": False},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        ),
        lambda arguments: ToolResult(content="ran", metadata={}),
    )
    return registry


def test_runtime_denial_contains_policy_reason_metadata(tmp_path) -> None:
    runtime = ThreadRuntime(
        provider_resolver=lambda _provider, _model: _Provider(),
        default_settings=ModelSettings(
            provider_config_id="p", model="m", approval_mode=ApprovalMode.NEVER
        ),
        tool_registry_factory=_registry,
    )
    thread = runtime.create_thread(tmp_path)
    summary = asyncio.run(runtime.run_turn(thread.thread_id, "do it"))
    assert summary.final_text == "done"
    finished = next(
        event
        for event in runtime.get_events(thread.thread_id).events
        if event.type == "tool_finished"
    )
    result = finished.payload["result"]
    assert result["error_code"] == "POLICY_DENIED"
    assert result["metadata"]["reason_code"] == "DESTRUCTIVE_COMMAND_NEVER"


def test_policy_internal_type_error_is_not_retried_as_legacy_signature(tmp_path) -> None:
    class InternalTypeErrorPolicy:
        def __init__(self) -> None:
            self.calls = 0

        def decide(self, call, *, approval_mode=ApprovalMode.ON_REQUEST):
            del call, approval_mode
            self.calls += 1
            raise TypeError("policy implementation failure")

    policy = InternalTypeErrorPolicy()
    runtime = ThreadRuntime(
        provider_resolver=lambda _provider, _model: _Provider(),
        default_settings=ModelSettings(provider_config_id="p", model="m"),
        tool_registry_factory=_registry,
        tool_policy=policy,
    )
    thread = runtime.create_thread(tmp_path)
    summary = asyncio.run(runtime.run_turn(thread.thread_id, "fail"))

    assert summary.status.value == "failed"
    assert policy.calls == 1


def _messages(runtime: ThreadRuntime, thread_id: str) -> list[object]:
    # Runtime's public snapshot intentionally exposes the same safe metadata.
    return runtime.get_snapshot(thread_id).messages
