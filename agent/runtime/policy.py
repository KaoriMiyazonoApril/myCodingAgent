"""Command authorization decisions independent of tools and the frontend.

The policy deliberately performs only a conservative classification of the
model-provided command string. The sandbox remains responsible for the actual
resource boundary and this module never executes a command.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
import shlex
from typing import Protocol

from agent.core.messages import ToolCallBlock

from .settings import ApprovalMode


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True, slots=True)
class PolicyResult:
    """Immutable result of one policy decision."""

    decision: PolicyDecision
    reason_code: str
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.decision, PolicyDecision):
            raise ValueError("decision must be a PolicyDecision")
        if not isinstance(self.reason_code, str) or not self.reason_code:
            raise ValueError("reason_code must be a non-empty string")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be a non-empty string")


@dataclass(frozen=True, slots=True)
class PolicyContext:
    """Per-Turn immutable policy context."""

    approval_mode: ApprovalMode = ApprovalMode.ON_REQUEST

    def __post_init__(self) -> None:
        if not isinstance(self.approval_mode, ApprovalMode):
            try:
                object.__setattr__(self, "approval_mode", ApprovalMode(self.approval_mode))
            except (TypeError, ValueError) as error:
                raise ValueError("approval_mode must be an ApprovalMode value") from error


class ExecClassification(str, Enum):
    """Conservative classes used by :class:`CommandAwarePolicy`."""

    SAFE_READ_ONLY = "safe_read_only"
    TEST_BUILD = "test_build"
    ORDINARY_SANDBOXED = "ordinary_sandboxed"
    DESTRUCTIVE = "destructive"
    NETWORK = "network"
    PACKAGE_INSTALL = "package_install"
    PRIVILEGED = "privileged"
    INTERACTIVE = "interactive"
    COMPLEX_SHELL = "complex_shell"
    UNKNOWN = "unknown"

    SAFE = SAFE_READ_ONLY
    ORDINARY = ORDINARY_SANDBOXED


_COMMAND_TOOLS = frozenset({"run_command", "exec_command"})
_SAFE_EXECUTABLES = frozenset(
    {
        "pwd", "ls", "find", "rg", "grep", "cat", "head", "tail",
        "stat", "file", "wc", "du", "git",
    }
)
_TEST_EXECUTABLES = frozenset(
    {
        "pytest", "py.test", "tox", "nox", "ruff", "flake8", "mypy",
        "pyright", "black", "isort", "eslint", "prettier", "tsc", "vitest",
        "jest", "cargo", "go", "mvn", "gradle", "make",
    }
)
_NETWORK_EXECUTABLES = frozenset(
    {"curl", "wget", "ssh", "scp", "sftp", "ftp", "nc", "netcat", "telnet"}
)
_PACKAGE_EXECUTABLES = frozenset(
    {"pip", "pip3", "pipx", "npm", "yarn", "pnpm", "apt", "apt-get", "brew"}
)
_PRIVILEGED_EXECUTABLES = frozenset(
    {"sudo", "su", "doas", "chmod", "chown", "chgrp", "mount", "umount"}
)
_INTERACTIVE_EXECUTABLES = frozenset(
    {
        "bash", "sh", "zsh", "fish", "ksh", "dash", "python", "python3",
        "ipython", "node", "irb", "ruby", "perl", "php", "lua", "top",
        "htop", "less", "more", "vim", "vi", "nano",
    }
)
_DESTRUCTIVE_EXECUTABLES = frozenset(
    {"rm", "rmdir", "unlink", "shred", "truncate", "mkfs", "fdisk", "dd"}
)
_SHELL_CONTROL_RE = re.compile(r"(?:\|\||&&|[|;]|\$\(|`)")
_SHELL_WRAPPER_RE = re.compile(r"^(?:ba)?sh\s+-c(?:\s|$)|^zsh\s+-c(?:\s|$)")


def classify_exec_command(command: str) -> ExecClassification:
    """Classify a submitted command without attempting to parse all of Bash."""

    if not isinstance(command, str) or not command.strip():
        return ExecClassification.UNKNOWN
    source = command.strip()
    if _SHELL_CONTROL_RE.search(source) or _SHELL_WRAPPER_RE.search(source):
        return ExecClassification.COMPLEX_SHELL
    try:
        tokens = shlex.split(source, posix=True)
    except ValueError:
        return ExecClassification.UNKNOWN
    if not tokens:
        return ExecClassification.UNKNOWN

    executable = tokens[0].rsplit("/", 1)[-1].lower()
    args = [token.lower() for token in tokens[1:]]
    if executable in _DESTRUCTIVE_EXECUTABLES:
        return ExecClassification.DESTRUCTIVE
    if executable == "git":
        if _git_is_destructive(args):
            return ExecClassification.DESTRUCTIVE
        if _git_is_networked(args):
            return ExecClassification.NETWORK
        if args and args[0] in {
            "status", "diff", "log", "show", "branch", "ls-files", "rev-parse",
        }:
            return ExecClassification.SAFE_READ_ONLY
        return ExecClassification.ORDINARY_SANDBOXED
    if executable in _PRIVILEGED_EXECUTABLES:
        return ExecClassification.PRIVILEGED
    if executable in _NETWORK_EXECUTABLES:
        return ExecClassification.NETWORK
    if executable in _PACKAGE_EXECUTABLES:
        if executable in {"npm", "yarn", "pnpm"} and any(
            arg in {"test", "run", "build", "lint", "check", "typecheck"}
            for arg in args
        ):
            return ExecClassification.TEST_BUILD
        if _is_package_install(executable, args):
            return ExecClassification.PACKAGE_INSTALL
        return ExecClassification.ORDINARY_SANDBOXED
    if executable in _INTERACTIVE_EXECUTABLES:
        if executable in {"bash", "sh", "zsh", "fish", "ksh", "dash"} and any(
            arg in {"-c", "--command"} for arg in args
        ):
            return ExecClassification.COMPLEX_SHELL
        if any(arg in {"-i", "--interactive"} for arg in args) or not args:
            return ExecClassification.INTERACTIVE
        return ExecClassification.ORDINARY_SANDBOXED
    if executable in _TEST_EXECUTABLES or _looks_like_test_build(executable, args):
        return ExecClassification.TEST_BUILD
    if executable in _SAFE_EXECUTABLES:
        return ExecClassification.SAFE_READ_ONLY
    if executable == "eval":
        return ExecClassification.COMPLEX_SHELL
    return ExecClassification.ORDINARY_SANDBOXED


classify_command = classify_exec_command


def _git_is_destructive(args: list[str]) -> bool:
    if not args:
        return False
    command = args[0]
    if command in {"reset", "clean", "restore"}:
        return True
    return command == "checkout" and "--" in args


def _git_is_networked(args: list[str]) -> bool:
    return bool(args) and args[0] in {"clone", "fetch", "pull", "push", "submodule"}


def _is_package_install(executable: str, args: list[str]) -> bool:
    if executable in {"pip", "pip3", "pipx"}:
        return any(arg in {"install", "uninstall", "inject", "runpip"} for arg in args)
    if executable in {"npm", "yarn", "pnpm"}:
        return any(arg in {"install", "i", "add", "remove", "uninstall", "update"} for arg in args)
    if executable in {"apt", "apt-get", "brew"}:
        return any(arg in {"install", "remove", "purge", "upgrade", "update"} for arg in args)
    return False


def _looks_like_test_build(executable: str, args: list[str]) -> bool:
    if executable in {"python", "python3", "uv", "poetry", "pipenv", "bun", "deno"}:
        joined = " ".join(args)
        return bool(
            re.search(
                r"(?:^|\s)(?:-m\s+)?(?:pytest|unittest|ruff|mypy|pyright|npm|yarn|pnpm|vitest|jest|tsc|eslint|build|test|lint)(?:\s|$)",
                joined,
            )
        )
    if executable in {"npm", "yarn", "pnpm"}:
        return any(arg in {"test", "run", "build", "lint", "check", "typecheck"} for arg in args)
    if executable in {"cargo", "go", "mvn", "gradle", "make"}:
        return any(arg in {"test", "check", "build", "lint", "verify", "compile"} for arg in args)
    return False


class ToolPolicy(Protocol):
    def decide(
        self,
        call: ToolCallBlock,
        *,
        approval_mode: ApprovalMode = ApprovalMode.ON_REQUEST,
    ) -> PolicyResult | PolicyDecision:
        """Return one explicit decision for a model-requested tool call."""


class AllowAllPolicy:
    """Compatibility policy: allow all valid calls with an explicit reason."""

    def decide(
        self,
        call: ToolCallBlock,
        *,
        approval_mode: ApprovalMode = ApprovalMode.ON_REQUEST,
    ) -> PolicyResult:
        del call, approval_mode
        return PolicyResult(PolicyDecision.ALLOW, "ALLOW_ALL", "tool allowed by policy")


class CommandAwarePolicy:
    """Apply the Phase 1 command matrix using immutable per-Turn settings."""

    _approval_required = frozenset(
        {
            ExecClassification.DESTRUCTIVE,
            ExecClassification.NETWORK,
            ExecClassification.PACKAGE_INSTALL,
            ExecClassification.PRIVILEGED,
            ExecClassification.INTERACTIVE,
            ExecClassification.COMPLEX_SHELL,
            ExecClassification.UNKNOWN,
        }
    )

    def decide(
        self,
        call: ToolCallBlock,
        *,
        approval_mode: ApprovalMode = ApprovalMode.ON_REQUEST,
    ) -> PolicyResult:
        if not isinstance(approval_mode, ApprovalMode):
            approval_mode = ApprovalMode(approval_mode)
        if call.name not in _COMMAND_TOOLS:
            return PolicyResult(
                PolicyDecision.ALLOW,
                "NON_COMMAND_TOOL",
                "non-command tool allowed",
            )
        if call.arguments_error is not None or call.arguments is None:
            # Let Registry produce its normal INVALID_ARGUMENTS result; there
            # is no trustworthy command intent to authorize in this case.
            return PolicyResult(
                PolicyDecision.ALLOW,
                "INVALID_ARGUMENTS",
                "invalid command arguments are handled by the tool registry",
            )
        else:
            command = call.arguments.get("command")
            if not isinstance(command, str) or not command.strip():
                return PolicyResult(
                    PolicyDecision.ALLOW,
                    "INVALID_ARGUMENTS",
                    "invalid command arguments are handled by the tool registry",
                )
            classification = (
                classify_exec_command(command)
            )
            if bool(call.arguments.get("tty", False)):
                classification = ExecClassification.INTERACTIVE

            # ``run_command`` is the pre-Phase-3 compatibility spelling. Its
            # shell wrapper was historically accepted, so preserve ordinary
            # ON_REQUEST behavior while keeping the formal ``exec_command``
            # surface strict. NEVER and UNTRUSTED remain conservative below.
            if (
                call.name == "run_command"
                and classification is ExecClassification.COMPLEX_SHELL
                and approval_mode is ApprovalMode.ON_REQUEST
            ):
                return PolicyResult(
                    PolicyDecision.ALLOW,
                    "LEGACY_SHELL_COMMAND",
                    "legacy command allowed inside the existing sandbox",
                )

        reason_code = _reason_code(classification)
        if classification is ExecClassification.SAFE_READ_ONLY:
            return PolicyResult(
                PolicyDecision.ALLOW,
                reason_code,
                "read-only command allowed",
            )
        if approval_mode is ApprovalMode.NEVER:
            return PolicyResult(
                PolicyDecision.DENY,
                f"{reason_code}_NEVER",
                "commands requiring approval are disabled by approval mode",
            )
        if classification in {
            ExecClassification.TEST_BUILD,
            ExecClassification.ORDINARY_SANDBOXED,
        }:
            if approval_mode is ApprovalMode.UNTRUSTED:
                return PolicyResult(
                    PolicyDecision.REQUIRE_APPROVAL,
                    reason_code,
                    "command requires approval in untrusted mode",
                )
            return PolicyResult(
                PolicyDecision.ALLOW,
                reason_code,
                "sandboxed command allowed",
            )
        if classification in self._approval_required:
            return PolicyResult(
                PolicyDecision.REQUIRE_APPROVAL,
                reason_code,
                "command requires approval",
            )
        return PolicyResult(
            PolicyDecision.REQUIRE_APPROVAL,
            "UNKNOWN_COMMAND_CLASSIFICATION",
            "command classification is not reliable",
        )


def _reason_code(classification: ExecClassification) -> str:
    return {
        ExecClassification.SAFE_READ_ONLY: "SAFE_READ_ONLY",
        ExecClassification.TEST_BUILD: "TEST_BUILD",
        ExecClassification.ORDINARY_SANDBOXED: "ORDINARY_SANDBOXED",
        ExecClassification.DESTRUCTIVE: "DESTRUCTIVE_COMMAND",
        ExecClassification.NETWORK: "NETWORK_COMMAND",
        ExecClassification.PACKAGE_INSTALL: "PACKAGE_INSTALL",
        ExecClassification.PRIVILEGED: "PRIVILEGED_COMMAND",
        ExecClassification.INTERACTIVE: "INTERACTIVE_COMMAND",
        ExecClassification.COMPLEX_SHELL: "COMPLEX_SHELL",
        ExecClassification.UNKNOWN: "UNKNOWN_COMMAND",
    }[classification]


# Names used by the public Phase 1 seam and by older design notes.
ExecPolicy = CommandAwarePolicy
CommandClassification = ExecClassification
StructuredPolicyResult = PolicyResult
