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


class ExecutionProfile(str, Enum):
    """Minimum sandbox capabilities granted to an approved command."""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    WORKSPACE_WRITE_NETWORK = "workspace_write_network"


@dataclass(frozen=True, slots=True)
class PolicyResult:
    """Immutable result of one policy decision."""

    decision: PolicyDecision
    reason_code: str
    message: str
    execution_profile: ExecutionProfile | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, PolicyDecision):
            raise ValueError("decision must be a PolicyDecision")
        if not isinstance(self.reason_code, str) or not self.reason_code:
            raise ValueError("reason_code must be a non-empty string")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be a non-empty string")
        profile = self.execution_profile
        if profile is None:
            profile = _profile_for_reason(self.reason_code)
            object.__setattr__(self, "execution_profile", profile)
        elif not isinstance(profile, ExecutionProfile):
            try:
                object.__setattr__(self, "execution_profile", ExecutionProfile(profile))
            except (TypeError, ValueError) as error:
                raise ValueError("execution_profile must be an ExecutionProfile") from error

    @property
    def profile(self) -> ExecutionProfile:
        """Short alias for integrations that call the field ``profile``."""

        assert self.execution_profile is not None
        return self.execution_profile


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
    DYNAMIC_INTERPRETER = "dynamic_interpreter"
    UNKNOWN = "unknown"

    # Wrapper analysis deliberately has no separate ``allow`` path for an
    # ambiguous wrapper.  Keeping this alias makes the conservative outcome
    # explicit to callers while preserving the existing UNKNOWN contract.
    AMBIGUOUS_WRAPPER = UNKNOWN

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
_SHELL_WRAPPER_RE = re.compile(r"^(?:ba)?sh\s+-c(?:\s|$)|^zsh\s+-c(?:\s|$)")


def classify_exec_command(command: str) -> ExecClassification:
    """Classify a submitted command without attempting to parse all of Bash."""

    if not isinstance(command, str) or not command.strip():
        return ExecClassification.UNKNOWN
    source = command.strip()
    if _contains_shell_control(source) or _SHELL_WRAPPER_RE.search(source):
        return ExecClassification.COMPLEX_SHELL
    try:
        tokens = shlex.split(source, posix=True)
    except ValueError:
        return ExecClassification.UNKNOWN
    if not tokens:
        return ExecClassification.UNKNOWN

    return _classify_tokens(tokens)


def _classify_tokens(tokens: list[str], *, depth: int = 0) -> ExecClassification:
    """Classify one tokenized command, recursively unwrapping safe wrappers.

    This is intentionally a bounded token analysis rather than a shell
    parser.  A wrapper is only removed when its target can be identified
    unambiguously; otherwise UNKNOWN reaches the normal approval matrix.
    """

    if not tokens or depth > 4:
        return ExecClassification.UNKNOWN
    executable = tokens[0].rsplit("/", 1)[-1].lower()
    if executable == "env":
        target = _env_target(tokens[1:])
        return (
            ExecClassification.UNKNOWN
            if target is None
            else _classify_tokens(target, depth=depth + 1)
        )
    if executable in {"python", "python3"}:
        return _classify_python(tokens, depth=depth)
    if executable == "xargs":
        target = _xargs_target(tokens[1:])
        return (
            ExecClassification.UNKNOWN
            if target is None
            else _classify_tokens(target, depth=depth + 1)
        )

    return _classify_direct(tokens)


def _classify_python(tokens: list[str], *, depth: int) -> ExecClassification:
    """Recognize Python's dynamic modes and classify ``-m`` targets."""

    args = [token.lower() for token in tokens[1:]]
    if any(argument == "-c" for argument in args):
        return ExecClassification.DYNAMIC_INTERPRETER
    # ``python -`` reads source from stdin.  It is dynamic even when options
    # precede the stdin marker, so do not reduce it to ordinary execution.
    if any(argument == "-" for argument in args):
        return ExecClassification.DYNAMIC_INTERPRETER
    for index, argument in enumerate(args):
        if argument == "-m":
            if index + 1 >= len(args):
                return ExecClassification.UNKNOWN
            module = args[index + 1]
            module_args = tokens[index + 3 :]
            if module in {"pip", "pip3", "pipx", "npm", "yarn", "pnpm"}:
                return _classify_tokens(
                    [module, *module_args],
                    depth=depth + 1,
                )
            if module in {
                "pytest", "py.test", "tox", "nox", "ruff", "flake8",
                "mypy", "pyright", "unittest",
            }:
                return ExecClassification.TEST_BUILD
            # An explicit module is ordinary interpreter execution unless it
            # contains a shell-control token (which the outer source scan
            # already catches for unquoted operators).
            return ExecClassification.ORDINARY_SANDBOXED
    return _classify_direct(tokens)


def _classify_direct(tokens: list[str]) -> ExecClassification:
    """Classify an already unwrapped executable using the existing matrix."""

    executable = tokens[0].rsplit("/", 1)[-1].lower()
    args = [token.lower() for token in tokens[1:]]
    if executable in _DESTRUCTIVE_EXECUTABLES:
        return ExecClassification.DESTRUCTIVE
    if executable == "find" and any(
        argument in {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
        for argument in args
    ):
        return ExecClassification.DESTRUCTIVE
    if executable == "git":
        if _git_is_destructive(args):
            return ExecClassification.DESTRUCTIVE
        if _git_is_networked(args):
            return ExecClassification.NETWORK
        if args and args[0] in {
            "status", "diff", "log", "show", "ls-files", "rev-parse",
        }:
            return ExecClassification.SAFE_READ_ONLY
        if args and args[0] == "branch" and _git_branch_is_read_only(args[1:]):
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


_ENV_FLAG_OPTIONS = frozenset({
    "-i",
    "--ignore-environment",
    "-0",
})
_ENV_VALUE_OPTIONS = frozenset({
    "-u",
    "--unset",
})


def _env_target(tokens: list[str]) -> list[str] | None:
    """Return the command after a reliably parsed ``env`` prefix."""

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if _is_env_assignment(token):
            index += 1
            continue
        if token in _ENV_FLAG_OPTIONS:
            index += 1
            continue
        if token in _ENV_VALUE_OPTIONS:
            if index + 1 >= len(tokens):
                return None
            index += 2
            continue
        if token.startswith("--unset="):
            if not token.removeprefix("--unset="):
                return None
            index += 1
            continue
        if token.startswith("-"):
            # Unknown options may alter how following tokens are interpreted.
            return None
        break
    return tokens[index:] or None


def _is_env_assignment(token: str) -> bool:
    name, separator, _ = token.partition("=")
    return bool(
        separator
        and name
        and (name[0].isalpha() or name[0] == "_")
        and all(character.isalnum() or character == "_" for character in name)
    )


_XARGS_FLAG_OPTIONS = frozenset({
    "-0", "--null", "-r", "--no-run-if-empty", "-t", "--verbose",
    "-p", "--interactive", "-o", "--open-tty", "--show-limits",
})
_XARGS_VALUE_OPTIONS = frozenset({
    "-a", "--arg-file", "-d", "--delimiter", "-e", "-E", "--eof",
    "-I", "--replace", "-L", "--max-lines", "-n", "--max-args",
    "-P", "--max-procs", "-s", "--max-chars", "--process-slot-var",
})


def _xargs_target(tokens: list[str]) -> list[str] | None:
    """Return a known xargs target, or None for an ambiguous invocation."""

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if not token.startswith("-") or token == "-":
            break
        if token in _XARGS_FLAG_OPTIONS:
            index += 1
            continue
        if token in _XARGS_VALUE_OPTIONS:
            if index + 1 >= len(tokens):
                return None
            index += 2
            continue
        if token.startswith("--"):
            option, separator, value = token.partition("=")
            if option not in _XARGS_VALUE_OPTIONS or not separator or not value:
                return None
            index += 1
            continue
        # Common short options may carry their value in the same token (for
        # example -n1 and -I{}).  Unknown clusters stay conservative.
        if len(token) >= 3 and token[:2] in _XARGS_VALUE_OPTIONS:
            index += 1
            continue
        return None
    return tokens[index:] or None


classify_command = classify_exec_command


def _contains_shell_control(source: str) -> bool:
    """Recognize obvious shell operators while respecting quoted arguments."""

    single = False
    double = False
    escaped = False
    index = 0
    while index < len(source):
        character = source[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if character == "\\" and not single:
            escaped = True
            index += 1
            continue
        if character == "'" and not double:
            single = not single
        elif character == '"' and not single:
            double = not double
        elif not single and character == "`":
            return True
        elif not single and character == "$" and source[index : index + 2] == "$(":
            return True
        elif not single and not double:
            if character in ";|<>" or source[index : index + 2] in {"&&", "||"}:
                return True
        index += 1
    return False


def _git_is_destructive(args: list[str]) -> bool:
    if not args:
        return False
    command = args[0]
    if command in {"reset", "clean", "restore"}:
        return True
    if command == "checkout" and "--" in args:
        return True
    if command in {"branch", "tag"} and any(
        argument in {"-d", "-D", "-f", "--delete", "--force"} for argument in args
    ):
        return True
    return command in {"stash", "worktree"} and any(
        argument in {"drop", "clear", "remove"} for argument in args
    )


def _git_is_networked(args: list[str]) -> bool:
    return bool(args) and args[0] in {"clone", "fetch", "pull", "push", "submodule"}


def _git_branch_is_read_only(args: list[str]) -> bool:
    """Allow only branch listing/inspection forms through the read-only class."""

    if not args:
        return True
    read_only_flags = {
        "-a",
        "--all",
        "-l",
        "--list",
        "-r",
        "--remotes",
        "-v",
        "-vv",
        "--verbose",
        "--contains",
        "--merged",
        "--no-merged",
        "--points-at",
    }
    return all(argument in read_only_flags for argument in args)


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
            ExecClassification.DYNAMIC_INTERPRETER,
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

        reason_code = _reason_code(classification)
        if classification is ExecClassification.SAFE_READ_ONLY:
            return PolicyResult(
                PolicyDecision.ALLOW,
                reason_code,
                "read-only command allowed",
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
            if classification is ExecClassification.PRIVILEGED:
                return PolicyResult(
                    PolicyDecision.DENY,
                    "PRIVILEGED_COMMAND_UNSUPPORTED",
                    "privileged capabilities are unavailable in the sandbox",
                )
            if approval_mode is ApprovalMode.NEVER:
                return PolicyResult(
                    PolicyDecision.DENY,
                    f"{reason_code}_NEVER",
                    "commands requiring approval are disabled by approval mode",
                )
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
        ExecClassification.DYNAMIC_INTERPRETER: "DYNAMIC_INTERPRETER",
        ExecClassification.UNKNOWN: "UNKNOWN_COMMAND",
    }[classification]


def _profile_for_reason(reason_code: str) -> ExecutionProfile:
    """Map policy intent to the least-capable sandbox profile."""

    if reason_code.startswith("SAFE_READ_ONLY") or reason_code == "NON_COMMAND_TOOL":
        return ExecutionProfile.READ_ONLY
    if reason_code in {"NETWORK_COMMAND", "PACKAGE_INSTALL"}:
        return ExecutionProfile.WORKSPACE_WRITE_NETWORK
    return ExecutionProfile.WORKSPACE_WRITE


# Names used by the public Phase 1 seam and by older design notes.
ExecPolicy = CommandAwarePolicy
CommandClassification = ExecClassification
StructuredPolicyResult = PolicyResult
