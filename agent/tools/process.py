"""Non-interactive, workspace-scoped command execution."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import OrderedDict, deque
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
import inspect
import os
import pty
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from typing import Any
from typing import TYPE_CHECKING
from uuid import uuid4

from agent.tools.filesystem import ToolOperationError, WorkspaceFilesystem, truncate_utf8
from agent.tools.types import ToolResult

if TYPE_CHECKING:
    from agent.runtime.policy import ExecutionProfile


OUTPUT_LIMIT_BYTES = 100 * 1024


class CommandSandboxUnavailableError(RuntimeError):
    """The configured command sandbox cannot run on this host."""


@dataclass(frozen=True, slots=True)
class SandboxExecution:
    """Raw, bounded outcome returned by a command sandbox backend."""

    duration_ms: int
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool


@dataclass(slots=True)
class _SpawnedProcess:
    process: asyncio.subprocess.Process
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader | None
    stdin_fd: int | None = None
    output_transport: asyncio.BaseTransport | None = None
    merged_output: bool = False


class CommandSandboxBackend(ABC):
    """Capability-probed, cancellable command execution boundary."""

    def close(self) -> None:
        """Release adapter-owned resources; repeated calls are safe."""

    @abstractmethod
    def check_available(self, workspace_root: Path) -> None:
        """Raise when this backend cannot protect the selected workspace."""

    @abstractmethod
    def _build_command(
        self,
        *,
        workspace_root: Path,
        command: str,
        relative_cwd: str,
        execution_profile: "ExecutionProfile | None" = None,
    ) -> list[str]:
        """Return the isolated process invocation for one command."""

    async def execute(
        self,
        *,
        workspace_root: Path,
        working_directory: Path,
        relative_cwd: str,
        command: str,
        timeout_ms: int,
        execution_profile: "ExecutionProfile | None" = None,
    ) -> SandboxExecution:
        """Execute one command; cancellation terminates its process group."""

        invocation = self._build_invocation(
            workspace_root=workspace_root,
            command=command,
            relative_cwd=relative_cwd,
            execution_profile=execution_profile,
        )
        start = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *invocation,
                cwd=working_directory,
                stdin=subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                pass_fds=self._pass_fds(),
            )
        except OSError as error:
            raise ToolOperationError(
                "PROCESS_START_FAILED", f"could not start command: {error}"
            ) from error

        assert process.stdout is not None
        assert process.stderr is not None
        stdout_capture = _BoundedOutput()
        stderr_capture = _BoundedOutput()
        capture_tasks = [
            asyncio.create_task(self._capture(process.stdout, stdout_capture)),
            asyncio.create_task(self._capture(process.stderr, stderr_capture)),
        ]
        wait_task = asyncio.create_task(process.wait())
        timed_out = False
        try:
            try:
                await asyncio.wait_for(
                    asyncio.shield(wait_task), timeout=timeout_ms / 1000
                )
            except TimeoutError:
                timed_out = True
                await self._terminate_process_group(process, wait_task)
            await self._finish_capture(process, capture_tasks)
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(
                self._cleanup_cancelled(process, wait_task, capture_tasks)
            )
            await asyncio.shield(cleanup)
            raise

        stdout_text, stdout_truncated = stdout_capture.result()
        stderr_text, stderr_truncated = stderr_capture.result()
        return SandboxExecution(
            duration_ms=round((time.monotonic() - start) * 1000),
            exit_code=process.returncode,
            timed_out=timed_out,
            stdout=stdout_text,
            stderr=stderr_text,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

    async def start_session(
        self,
        *,
        workspace_root: Path,
        working_directory: Path,
        relative_cwd: str,
        command: str,
        tty: bool,
        execution_profile: "ExecutionProfile | None" = None,
    ) -> _SpawnedProcess:
        """Start a persistent sandboxed process using the existing invocation seam."""

        invocation = self._build_invocation(
            workspace_root=workspace_root,
            command=command,
            relative_cwd=relative_cwd,
            execution_profile=execution_profile,
        )

        if not tty:
            try:
                process = await asyncio.create_subprocess_exec(
                    *invocation,
                    cwd=working_directory,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    pass_fds=self._pass_fds(),
                )
            except OSError as error:
                raise ToolOperationError(
                    "PROCESS_START_FAILED", f"could not start command: {error}"
                ) from error
            assert process.stdout is not None
            assert process.stderr is not None
            return _SpawnedProcess(process, process.stdout, process.stderr)

        if not hasattr(os, "openpty"):
            raise ToolOperationError("PTY_UNAVAILABLE", "interactive PTY is unavailable")
        master_fd, slave_fd = os.openpty()
        read_fd = os.dup(master_fd)
        try:
            process = await asyncio.create_subprocess_exec(
                *invocation,
                cwd=working_directory,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                pass_fds=self._pass_fds(),
            )
        except OSError as error:
            os.close(master_fd)
            os.close(slave_fd)
            os.close(read_fd)
            raise ToolOperationError(
                "PROCESS_START_FAILED", f"could not start command: {error}"
            ) from error
        os.close(slave_fd)
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        try:
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: protocol,
                os.fdopen(read_fd, "rb", buffering=0),
            )
        except Exception as error:
            for descriptor in (master_fd, read_fd):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            self._signal_process_group(process.pid, signal.SIGKILL)
            raise ToolOperationError(
                "PROCESS_START_FAILED", "could not attach interactive PTY"
            ) from error
        return _SpawnedProcess(
            process,
            reader,
            None,
            stdin_fd=master_fd,
            output_transport=transport,
            merged_output=True,
        )

    def _build_invocation(
        self,
        *,
        workspace_root: Path,
        command: str,
        relative_cwd: str,
        execution_profile: "ExecutionProfile | None",
    ) -> list[str]:
        """Call old injected backends while exposing the profile to new ones."""

        try:
            parameters = inspect.signature(self._build_command).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "execution_profile" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            return self._build_command(
                workspace_root=workspace_root,
                command=command,
                relative_cwd=relative_cwd,
                execution_profile=execution_profile,
            )
        return self._build_command(
            workspace_root=workspace_root,
            command=command,
            relative_cwd=relative_cwd,
        )

    def _pass_fds(self) -> tuple[int, ...]:
        return ()

    @staticmethod
    async def _capture(
        stream: asyncio.StreamReader, capture: _BoundedOutput
    ) -> None:
        while chunk := await stream.read(8192):
            capture.append(chunk)

    async def _finish_capture(
        self,
        process: asyncio.subprocess.Process,
        tasks: list[asyncio.Task[None]],
    ) -> None:
        try:
            await asyncio.wait_for(
                asyncio.gather(*(asyncio.shield(task) for task in tasks)),
                timeout=0.1,
            )
        except TimeoutError:
            self._signal_process_group(process.pid, signal.SIGTERM)
            self._close_output_transports(process)
            await self._cancel_tasks(tasks)

    async def _cleanup_cancelled(
        self,
        process: asyncio.subprocess.Process,
        wait_task: asyncio.Task[int],
        capture_tasks: list[asyncio.Task[None]],
    ) -> None:
        await self._terminate_process_group(process, wait_task)
        self._close_output_transports(process)
        await self._cancel_tasks(capture_tasks)

    async def _terminate_process_group(
        self,
        process: asyncio.subprocess.Process,
        wait_task: asyncio.Task[int],
    ) -> None:
        self._signal_process_group(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(wait_task), timeout=0.1)
        except TimeoutError:
            self._signal_process_group(process.pid, signal.SIGKILL)
            try:
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=0.1)
            except TimeoutError:
                process.kill()
                self._close_output_transports(process)
                await self._cancel_tasks([wait_task])

    @staticmethod
    def _close_output_transports(process: asyncio.subprocess.Process) -> None:
        """Close asyncio's pipe transports after the owned process is killed."""

        for stream in (process.stdout, process.stderr):
            transport = getattr(stream, "_transport", None)
            if transport is not None:
                transport.close()

    @staticmethod
    def _signal_process_group(process_id: int, signal_number: signal.Signals) -> None:
        try:
            os.killpg(process_id, signal_number)
        except OSError:
            pass

    @staticmethod
    async def _cancel_tasks(tasks: list[asyncio.Task[Any]]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        if not tasks:
            return
        done, _ = await asyncio.wait(tasks, timeout=0.1)
        for task in done:
            if not task.cancelled():
                task.exception()


class BubblewrapSandboxBackend(CommandSandboxBackend):
    """Production Linux bubblewrap command sandbox."""

    def __init__(self) -> None:
        self._executable: str | None = None
        self._seccomp_fd: int | None = None

    def check_available(self, workspace_root: Path) -> None:
        """Fail during runtime composition when bubblewrap cannot execute."""
        self._reset_capability()
        if not sys.platform.startswith("linux"):
            raise CommandSandboxUnavailableError(
                "bubblewrap command isolation requires Linux or WSL2"
            )
        executable = shutil.which("bwrap")
        if executable is None:
            raise CommandSandboxUnavailableError(
                "bubblewrap is required for workspace command isolation"
            )
        probe_command = self._command(
            executable=executable,
            workspace_root=workspace_root,
            command="true",
            relative_cwd=".",
        )
        try:
            probe = subprocess.run(
                probe_command,
                cwd=workspace_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=3,
                check=False,
                pass_fds=self._pass_fds(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self._reset_capability()
            raise CommandSandboxUnavailableError(
                f"bubblewrap capability probe failed: {error}"
            ) from error
        if probe.returncode != 0:
            details = probe.stderr.decode("utf-8", errors="replace").strip()
            suffix = f": {details}" if details else ""
            self._reset_capability()
            raise CommandSandboxUnavailableError(
                f"bubblewrap capability probe failed with status {probe.returncode}{suffix}"
            )
        self._executable = executable

    def _build_command(
        self,
        *,
        workspace_root: Path,
        command: str,
        relative_cwd: str,
        execution_profile: "ExecutionProfile | None" = None,
    ) -> list[str]:
        if self._executable is None:
            raise CommandSandboxUnavailableError(
                "bubblewrap backend must pass its capability check before use"
            )
        return self._command(
            executable=self._executable,
            workspace_root=workspace_root,
            command=command,
            relative_cwd=relative_cwd,
            execution_profile=execution_profile,
            seccomp_fd=self._seccomp_fd,
        )

    def _pass_fds(self) -> tuple[int, ...]:
        return () if self._seccomp_fd is None else (self._seccomp_fd,)

    def close(self) -> None:
        """Release the capability-probe descriptor owned by this backend."""

        self._reset_capability()

    @staticmethod
    def _command(
        *,
        executable: str,
        workspace_root: Path,
        command: str,
        relative_cwd: str,
        seccomp_fd: int | None = None,
        execution_profile: "ExecutionProfile | None" = None,
    ) -> list[str]:
        sandbox_cwd = "/workspace"
        if relative_cwd != ".":
            sandbox_cwd = f"/workspace/{relative_cwd}"
        profile_value = getattr(execution_profile, "value", execution_profile)
        if profile_value is None:
            profile_value = "workspace_write"
        writable = profile_value != "read_only"
        network = profile_value == "workspace_write_network"
        invocation = [
            executable,
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--ro-bind",
            "/usr",
            "/usr",
            "--symlink",
            "usr/bin",
            "/bin",
            "--symlink",
            "usr/sbin",
            "/sbin",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib64",
            "/lib64",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/home",
            "--bind" if writable else "--ro-bind",
            str(workspace_root),
            "/workspace",
            "--chdir",
            sandbox_cwd,
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/local/bin:/usr/bin:/bin",
            "--setenv",
            "HOME",
            "/tmp",
            "/usr/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            command,
        ]
        if network:
            # ``--unshare-all`` isolates network by default. Share only the
            # network namespace for the explicit network profile and expose
            # the minimum read-only resolver/TLS configuration it needs.
            share_index = invocation.index("--die-with-parent") + 1
            invocation[share_index:share_index] = ["--share-net"]
            chdir_index = invocation.index("--chdir")
            invocation[chdir_index:chdir_index] = [
                "--dir",
                "/etc",
                "--ro-bind",
                "/etc/resolv.conf",
                "/etc/resolv.conf",
                "--ro-bind",
                "/etc/hosts",
                "/etc/hosts",
                "--ro-bind",
                "/etc/ssl",
                "/etc/ssl",
            ]
        if seccomp_fd is not None:
            # Older integrations may still provide a seccomp descriptor. It
            # is intentionally optional because link creation is not the
            # workspace containment seam; existing internal aliases remain
            # valid and are checked by the filesystem at access time.
            seccomp_index = invocation.index("--cap-drop") + 2
            invocation[seccomp_index:seccomp_index] = ["--seccomp", str(seccomp_fd)]
        return invocation

    def _reset_capability(self) -> None:
        self._executable = None
        if self._seccomp_fd is not None:
            os.close(self._seccomp_fd)
            self._seccomp_fd = None

class _BoundedOutput:
    """Retain bounded head/tail output while continuously draining a stream."""

    _MARKER = b"\n... output truncated ...\n"

    def __init__(self) -> None:
        self._head = bytearray()
        self._tail = bytearray()
        self._total_bytes = 0

    def append(self, chunk: bytes) -> None:
        self._total_bytes += len(chunk)
        if len(self._head) < OUTPUT_LIMIT_BYTES:
            needed = OUTPUT_LIMIT_BYTES - len(self._head)
            self._head.extend(chunk[:needed])
        self._tail.extend(chunk)
        if len(self._tail) > OUTPUT_LIMIT_BYTES:
            del self._tail[: len(self._tail) - OUTPUT_LIMIT_BYTES]

    def result(self) -> tuple[str, bool]:
        if self._total_bytes <= OUTPUT_LIMIT_BYTES:
            return bytes(self._head).decode("utf-8", errors="replace"), False
        retained_bytes = OUTPUT_LIMIT_BYTES - len(self._MARKER)
        head_size = retained_bytes // 2
        tail_size = retained_bytes - head_size
        kept = (
            bytes(self._head[:head_size])
            + self._MARKER
            + bytes(self._tail[-tail_size:])
        )
        return kept.decode("utf-8", errors="replace"), True


class CommandRunner:
    """Execute one command from a filesystem-validated initial directory."""

    def __init__(
        self,
        filesystem: WorkspaceFilesystem,
        sandbox_backend: CommandSandboxBackend | None = None,
    ) -> None:
        self._filesystem = filesystem
        self._sandbox = (
            sandbox_backend
            if sandbox_backend is not None
            else BubblewrapSandboxBackend()
        )
        self._sandbox.check_available(self._filesystem.root)
        self._execution_profile: "ExecutionProfile | None" = None

    def close(self) -> None:
        """Release resources owned by the configured sandbox backend."""

        self._sandbox.close()

    @property
    def sandbox(self) -> CommandSandboxBackend:
        """Expose the already-probed sandbox to the per-Thread session manager."""

        return self._sandbox

    def set_execution_profile(self, profile: "ExecutionProfile | None") -> None:
        """Set the profile used by the next one-shot command."""

        self._execution_profile = profile

    def run(
        self,
        command: str,
        cwd: str,
        timeout_ms: int,
        execution_profile: "ExecutionProfile | None" = None,
    ) -> ToolResult:
        """Synchronous entry point for CLI code and synchronous tests."""

        return asyncio.run(
            self.run_async(command, cwd, timeout_ms, execution_profile=execution_profile)
        )

    async def run_async(
        self,
        command: str,
        cwd: str,
        timeout_ms: int,
        *,
        execution_profile: "ExecutionProfile | None" = None,
    ) -> ToolResult:
        """Run a command with cancellable process-group ownership."""

        working_directory, relative_cwd = self._filesystem.resolve(cwd)
        if not working_directory.exists():
            raise ToolOperationError("NOT_FOUND", f"directory not found: {relative_cwd}")
        if not working_directory.is_dir():
            raise ToolOperationError("NOT_A_FILE", f"not a directory: {relative_cwd}")

        execution = await self._sandbox.execute(
            workspace_root=self._filesystem.root,
            working_directory=working_directory,
            command=command,
            relative_cwd=relative_cwd,
            timeout_ms=timeout_ms,
            execution_profile=(
                self._execution_profile
                if execution_profile is None
                else execution_profile
            ),
        )
        metadata = {
            "command": command,
            "cwd": relative_cwd,
            "duration_ms": execution.duration_ms,
            "exit_code": execution.exit_code,
            "timed_out": execution.timed_out,
            "stdout": execution.stdout,
            "stderr": execution.stderr,
            "stdout_truncated": execution.stdout_truncated,
            "stderr_truncated": execution.stderr_truncated,
            "command_succeeded": (
                not execution.timed_out and execution.exit_code == 0
            ),
            "sandboxed": True,
            "execution_profile": getattr(
                execution_profile
                if execution_profile is not None
                else self._execution_profile,
                "value",
                execution_profile if execution_profile is not None else self._execution_profile,
            ) or "workspace_write",
        }
        if execution.timed_out:
            status = f"command timed out after {timeout_ms} ms"
            error_code = "TIMEOUT"
        elif execution.exit_code == 0:
            status = "command completed successfully"
            error_code = None
        else:
            status = f"command exited with status {execution.exit_code}"
            error_code = "COMMAND_FAILED"
        content = (
            f"{status}\nstdout:\n{execution.stdout}\nstderr:\n{execution.stderr}"
        )
        content, content_truncated = truncate_utf8(content, OUTPUT_LIMIT_BYTES)
        metadata["content_truncated"] = content_truncated
        return ToolResult(content=content, metadata=metadata, error_code=error_code)


@dataclass(slots=True)
class _PendingOutput:
    stream: str
    content: bytes


class ProcessSession:
    """One bounded, incrementally readable child process session."""

    def __init__(
        self,
        *,
        session_id: str,
        owner_thread_id: str | None,
        owner_turn_id: str | None,
        command: str,
        cwd: str,
        tty: bool,
        spawned: _SpawnedProcess,
        event_sink: Callable[[str, dict[str, Any]], object] | None,
        on_terminal: Callable[[str], None] | None,
        idle_timeout_seconds: float,
        timeout_ms: int,
        execution_profile: "ExecutionProfile | None" = None,
    ) -> None:
        self.session_id = session_id
        # Ownership is deliberately captured once.  A ProcessSession may
        # outlive the Turn that created it, but it must never be rebound to a
        # later Turn merely because that Turn is currently executing.
        self._owner_thread_id = owner_thread_id
        self._owner_turn_id = owner_turn_id
        self.command = command
        self.cwd = cwd
        self.tty = tty
        self._spawned = spawned
        self._process = spawned.process
        self._event_sink = event_sink
        self._on_terminal = on_terminal
        self._idle_timeout_seconds = idle_timeout_seconds
        self._timeout_ms = timeout_ms
        self.execution_profile = execution_profile
        self._pending: deque[_PendingOutput] = deque()
        self._pending_bytes = {"stdout": 0, "stderr": 0}
        self._pending_truncated = {"stdout": False, "stderr": False}
        self._captures = {"stdout": _BoundedOutput(), "stderr": _BoundedOutput()}
        self._activity = asyncio.Event()
        self._exited = False
        self._timed_out = False
        self._idle_timed_out = False
        self._closed = False
        self._cancelled = False
        self._last_interaction = time.monotonic()
        self._interaction_lock = asyncio.Lock()
        self._output_tasks: list[asyncio.Task[None]] = []
        self._wait_task = asyncio.create_task(self._watch_process())
        self._timeout_task = asyncio.create_task(self._watch_timeout())
        self._idle_task = asyncio.create_task(self._watch_idle())
        self._output_tasks.append(
            asyncio.create_task(self._read_output("stdout", spawned.stdout))
        )
        if spawned.stderr is not None:
            self._output_tasks.append(
                asyncio.create_task(self._read_output("stderr", spawned.stderr))
            )

        self._terminal_result: ToolResult | None = None

    @property
    def running(self) -> bool:
        return not self._exited and self._process.returncode is None

    @property
    def owner_thread_id(self) -> str | None:
        """Immutable Thread owner captured at process creation."""

        return self._owner_thread_id

    @property
    def owner_turn_id(self) -> str | None:
        """Immutable creator Turn owner captured at process creation."""

        return self._owner_turn_id

    @property
    def exited(self) -> bool:
        return self._exited

    async def read(self, yield_time_ms: int) -> ToolResult:
        """Return only output accumulated since the previous read."""
        async with self._interaction_lock:
            return await self._read_unlocked(yield_time_ms)

    async def interact(self, chars: str, yield_time_ms: int) -> ToolResult:
        """Serialize one optional stdin write and its output poll.

        Keeping the write and read under one lock prevents two concurrent
        ``write_stdin`` requests from crossing wires (for example, one call
        reading output produced by another call's input).
        """

        async with self._interaction_lock:
            if chars:
                await self._write_unlocked(chars)
            return await self._read_unlocked(yield_time_ms)

    async def _read_unlocked(self, yield_time_ms: int) -> ToolResult:
        self._last_interaction = time.monotonic()
        deadline = time.monotonic() + yield_time_ms / 1000
        while self.running and yield_time_ms > 0 and time.monotonic() < deadline:
            self._activity.clear()
            if not self.running:
                break
            try:
                await asyncio.wait_for(
                    self._activity.wait(),
                    timeout=max(0.0, deadline - time.monotonic()),
                )
            except TimeoutError:
                break
        # A child may have produced its last bytes just before wait() made
        # the return code observable. Give the watcher one scheduling turn
        # so the terminal event is ordered after final output.
        await asyncio.sleep(0)
        if self._process.returncode is not None and not self._exited:
            try:
                await asyncio.wait_for(asyncio.shield(self._wait_task), timeout=0.05)
            except TimeoutError:
                pass
        return self._result()

    async def write(self, chars: str) -> None:
        """Write raw stdin, treating a standalone Ctrl-C as SIGINT."""
        async with self._interaction_lock:
            await self._write_unlocked(chars)

    async def _write_unlocked(self, chars: str) -> None:
        if not isinstance(chars, str):
            raise ToolOperationError("INVALID_ARGUMENTS", "chars must be a string")
        self._last_interaction = time.monotonic()
        if not self.running:
            raise ToolOperationError("STDIN_CLOSED", "session stdin is closed")
        if chars == "\x03":
            CommandSandboxBackend._signal_process_group(self._process.pid, signal.SIGINT)
            return
        if self.tty:
            if self._spawned.stdin_fd is None:
                raise ToolOperationError("STDIN_CLOSED", "session stdin is closed")
            try:
                await asyncio.to_thread(os.write, self._spawned.stdin_fd, chars.encode())
            except (BrokenPipeError, OSError) as error:
                raise ToolOperationError("STDIN_CLOSED", "session stdin is closed") from error
            return
        stdin = self._process.stdin
        if stdin is None:
            raise ToolOperationError("STDIN_CLOSED", "session stdin is closed")
        try:
            stdin.write(chars.encode())
            await stdin.drain()
        except (BrokenPipeError, ConnectionError) as error:
            raise ToolOperationError("STDIN_CLOSED", "session stdin is closed") from error

    async def close(self) -> None:
        """Terminate and reap this process, cancelling all reader/watchdog tasks."""

        if self._closed:
            return
        self._closed = True
        self._cancelled = True
        if self.running:
            await self._terminate()
        self._close_transports()
        tasks = [self._timeout_task, self._idle_task, *self._output_tasks]
        current = asyncio.current_task()
        for task in tasks:
            if task is not current and not task.done():
                task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=0.2
            )
        except TimeoutError:
            for task in tasks:
                if not task.done():
                    task.cancel()
        if self._wait_task is not current and not self._wait_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._wait_task), timeout=0.2)
            except (TimeoutError, asyncio.CancelledError):
                self._wait_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(self._process.wait()), timeout=0.2)
        except (TimeoutError, asyncio.CancelledError):
            pass
        self._event_sink = None
        self._on_terminal = None

    def close_sync(self) -> None:
        """Best-effort synchronous kill used by registry/Host close hooks."""

        self._cancelled = True
        if self.running:
            CommandSandboxBackend._signal_process_group(self._process.pid, signal.SIGTERM)
            CommandSandboxBackend._signal_process_group(self._process.pid, signal.SIGKILL)
        self._close_transports()
        self._event_sink = None
        self._on_terminal = None

    def _result(self) -> ToolResult:
        if self._terminal_result is not None:
            return self._terminal_result
        return self._build_result()

    def _build_result(self) -> ToolResult:
        stdout, stderr = self._take_pending()
        status = "running" if self.running else "exited"
        error_code: str | None = None
        if not self.running:
            if self._timed_out:
                error_code = "TIMEOUT"
            elif self._idle_timed_out:
                error_code = "IDLE_TIMEOUT"
            elif self._process.returncode not in (None, 0):
                error_code = "COMMAND_FAILED"
        if self.tty:
            content = stdout
        else:
            content = f"stdout:\n{stdout}\nstderr:\n{stderr}"
        content, content_truncated = truncate_utf8(content, OUTPUT_LIMIT_BYTES)
        return ToolResult(
            content=content,
            metadata={
                "status": status,
                "session_id": self.session_id,
                "owner_thread_id": self.owner_thread_id,
                "owner_turn_id": self.owner_turn_id,
                "cwd": self.cwd,
                "command": self.command,
                "execution_profile": getattr(
                    self.execution_profile,
                    "value",
                    self.execution_profile,
                ) or "workspace_write",
                "stdout": stdout,
                "stderr": stderr,
                "stdout_delta": stdout,
                "stderr_delta": stderr,
                "exit_code": self._process.returncode if not self.running else None,
                "command_succeeded": (
                    not self.running
                    and not self._timed_out
                    and not self._idle_timed_out
                    and self._process.returncode == 0
                ),
                "tty": self.tty,
                "stderr_merged": self._spawned.merged_output,
                "timed_out": self._timed_out,
                "idle_timed_out": self._idle_timed_out,
                "stdout_truncated": self._pending_truncated["stdout"],
                "stderr_truncated": self._pending_truncated["stderr"],
                "output_truncated": any(self._pending_truncated.values()),
                "content_truncated": content_truncated,
            },
            error_code=error_code,
        )

    async def _read_output(self, stream: str, reader: asyncio.StreamReader) -> None:
        try:
            while chunk := await reader.read(8192):
                self._captures[stream].append(chunk)
                self._pending.append(_PendingOutput(stream, chunk))
                self._pending_bytes[stream] += len(chunk)
                while self._pending_bytes[stream] > OUTPUT_LIMIT_BYTES:
                    removed = next(
                        (item for item in self._pending if item.stream == stream), None
                    )
                    if removed is None:
                        break
                    self._pending.remove(removed)
                    self._pending_bytes[stream] -= len(removed.content)
                    self._pending_truncated[stream] = True
                self._emit(
                    "command_output_delta",
                    {
                        "session_id": self.session_id,
                        "owner_thread_id": self.owner_thread_id,
                        "owner_turn_id": self.owner_turn_id,
                        "stream": stream,
                        "text": chunk.decode("utf-8", errors="replace"),
                    },
                )
                self._activity.set()
        except asyncio.CancelledError:
            raise
        except OSError:
            return

    async def _watch_process(self) -> None:
        try:
            await self._process.wait()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(asyncio.shield(task) for task in self._output_tasks)),
                    timeout=0.25,
                )
            except (TimeoutError, OSError):
                self._close_transports()
                for task in self._output_tasks:
                    if not task.done():
                        task.cancel()
        finally:
            self._exited = True
            self._activity.set()
            for task in (self._timeout_task, self._idle_task):
                if not task.done() and task is not asyncio.current_task():
                    task.cancel()
            self._emit(
                "command_completed",
                {
                    "session_id": self.session_id,
                    "owner_thread_id": self.owner_thread_id,
                    "owner_turn_id": self.owner_turn_id,
                    "status": "exited",
                    "exit_code": self._process.returncode,
                    "timed_out": self._timed_out,
                    "idle_timed_out": self._idle_timed_out,
                    "tty": self.tty,
                },
            )
            # Freeze the one final poll result before handing ownership back
            # to ProcessManager.  The manager can now release this object's
            # transports, readers and event callback without losing output
            # that arrived after the caller's last poll.
            self._terminal_result = self._build_result()
            if self._on_terminal is not None:
                self._on_terminal(self.session_id)

    async def _watch_timeout(self) -> None:
        try:
            await asyncio.sleep(self._timeout_ms / 1000)
            if self.running:
                self._timed_out = True
                await self._terminate()
                self._activity.set()
        except asyncio.CancelledError:
            return

    async def _watch_idle(self) -> None:
        try:
            while self.running:
                await asyncio.sleep(min(1.0, self._idle_timeout_seconds))
                if (
                    self.running
                    and time.monotonic() - self._last_interaction
                    >= self._idle_timeout_seconds
                ):
                    self._idle_timed_out = True
                    await self._terminate()
                    self._activity.set()
                    return
        except asyncio.CancelledError:
            return

    async def _terminate(self) -> None:
        if not self.running:
            return
        CommandSandboxBackend._signal_process_group(self._process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(self._process.wait()), timeout=0.1)
            return
        except TimeoutError:
            pass
        CommandSandboxBackend._signal_process_group(self._process.pid, signal.SIGKILL)
        try:
            await asyncio.wait_for(asyncio.shield(self._process.wait()), timeout=0.1)
        except TimeoutError:
            self._process.kill()

    def _take_pending(self) -> tuple[str, str]:
        stdout: list[bytes] = []
        stderr: list[bytes] = []
        while self._pending:
            item = self._pending.popleft()
            self._pending_bytes[item.stream] -= len(item.content)
            (stdout if item.stream == "stdout" else stderr).append(item.content)
        return (
            b"".join(stdout).decode("utf-8", errors="replace"),
            b"".join(stderr).decode("utf-8", errors="replace"),
        )

    def _close_transports(self) -> None:
        if self._spawned.output_transport is not None:
            self._spawned.output_transport.close()
            self._spawned.output_transport = None
        if self._spawned.stdin_fd is not None:
            try:
                os.close(self._spawned.stdin_fd)
            except OSError:
                pass
            self._spawned.stdin_fd = None
        if self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except RuntimeError:
                pass

    def release_completed(self) -> None:
        """Release heavy resources after the watcher has observed process exit.

        This method is intentionally synchronous and must only be called once
        ``_watch_process`` has reached its terminal callback.  It avoids
        retaining PTYs, pipe transports and the Turn emitter while a compact
        completed result is cached by ProcessManager.
        """

        self._close_transports()
        self._event_sink = None
        self._on_terminal = None
        self._pending.clear()
        self._pending_bytes = {"stdout": 0, "stderr": 0}
        self._captures.clear()
        for task in (*self._output_tasks, self._timeout_task, self._idle_task):
            if not task.done():
                task.cancel()
        self._output_tasks.clear()

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._event_sink is not None:
            try:
                self._event_sink(event_type, payload)
            except Exception:
                # Event delivery must not change process lifecycle semantics.
                return


class ProcessManager:
    """Own per-Thread stateful command sessions and their bounded output."""

    def __init__(
        self,
        filesystem: WorkspaceFilesystem,
        *,
        sandbox_backend: CommandSandboxBackend,
        max_sessions: int = 4,
        idle_timeout_seconds: float = 15 * 60,
        event_sink: Callable[[str, dict[str, Any]], object] | None = None,
        owner_thread_id: str | None = None,
        max_completed_sessions: int | None = None,
        max_dead_sessions: int | None = None,
        sandbox_checked: bool = False,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be positive")
        if idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds must be positive")
        self._filesystem = filesystem
        self._sandbox = sandbox_backend
        if not sandbox_checked:
            self._sandbox.check_available(filesystem.root)
        self._max_sessions = max_sessions
        self._idle_timeout_seconds = idle_timeout_seconds
        self._event_sink = event_sink
        self._owner_thread_id = owner_thread_id
        default_retention = max(16, max_sessions * 4)
        self._max_completed_sessions = (
            default_retention
            if max_completed_sessions is None
            else max_completed_sessions
        )
        self._max_dead_sessions = (
            default_retention if max_dead_sessions is None else max_dead_sessions
        )
        if self._max_completed_sessions < 1 or self._max_dead_sessions < 1:
            raise ValueError("session retention limits must be positive")
        self._sessions: dict[str, ProcessSession] = {}
        # Naturally exited sessions are kept separately from active sessions
        # so one final output/exit poll can be collected without counting
        # against the active-session limit.
        self._exited_sessions: OrderedDict[str, ToolResult] = OrderedDict()
        # Keep a small in-memory tombstone for sessions whose one final poll
        # has already been consumed. This distinguishes a known dead session
        # from a typo/unknown id without retaining process resources.
        self._dead_sessions: OrderedDict[str, None] = OrderedDict()
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._sandbox_released = False
        self._closed = False
        self._execution_profile: "ExecutionProfile | None" = None
        self._current_turn_id: str | None = None

    @property
    def active_session_count(self) -> int:
        """Number of live ProcessSession resources owned by this Thread."""

        return len(self._sessions)

    @property
    def completed_session_count(self) -> int:
        """Number of lightweight final results retained for one final poll."""

        return len(self._exited_sessions)

    @property
    def dead_session_count(self) -> int:
        """Number of bounded dead-session tombstones retained."""

        return len(self._dead_sessions)

    def set_owner_thread_id(self, owner_thread_id: str | None) -> None:
        """Set the Thread identity for sessions created from this manager."""

        if (
            self._owner_thread_id is not None
            and owner_thread_id != self._owner_thread_id
        ):
            raise RuntimeError("cannot change Thread identity")
        if self._sessions or self._exited_sessions:
            raise RuntimeError("cannot change Thread identity with live sessions")
        self._owner_thread_id = owner_thread_id

    def set_session_context(
        self,
        *,
        thread_id: str | None = None,
        turn_id: str | None,
    ) -> None:
        """Capture owner IDs used by sessions created by the next execution."""

        if thread_id is not None and thread_id != self._owner_thread_id:
            self.set_owner_thread_id(thread_id)
        self._current_turn_id = turn_id

    def set_execution_profile(self, profile: "ExecutionProfile | None") -> None:
        """Set the profile for newly created sessions."""

        self._execution_profile = profile

    def _on_terminal(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            if self._closed or session._cancelled:
                session.close_sync()
                session.release_completed()
            else:
                final = session._terminal_result
                if final is not None:
                    self._remember_completed(session_id, final)
                session.release_completed()

    def _schedule_cleanup(self, session: ProcessSession) -> None:
        """Retain asynchronous session cleanup until its reader tasks are drained."""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(session.close())
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    def set_event_sink(
        self, event_sink: Callable[[str, dict[str, Any]], object] | None
    ) -> None:
        """Set the sink for sessions created after this call.

        Existing sessions retain the sink captured at creation.  Rebinding
        them here would attribute persistent process output to a later Turn.
        """

        self._event_sink = event_sink

    async def exec(
        self,
        command: str,
        cwd: str = ".",
        yield_time_ms: int = 1000,
        timeout_ms: int = 60_000,
        tty: bool = False,
    ) -> ToolResult:
        if self._closed:
            raise ToolOperationError("PROCESS_MANAGER_CLOSED", "process manager is closed")
        working_directory, relative_cwd = self._filesystem.resolve(cwd)
        if not working_directory.exists():
            raise ToolOperationError("NOT_FOUND", f"directory not found: {relative_cwd}")
        if not working_directory.is_dir():
            raise ToolOperationError("NOT_A_FILE", f"not a directory: {relative_cwd}")
        if sum(session.running for session in self._sessions.values()) >= self._max_sessions:
            raise ToolOperationError("SESSION_LIMIT", "active session limit reached")
        session_id = str(uuid4())
        execution_profile = self._execution_profile
        spawned = await self._sandbox.start_session(
            workspace_root=self._filesystem.root,
            working_directory=working_directory,
            relative_cwd=relative_cwd,
            command=command,
            tty=tty,
            execution_profile=execution_profile,
        )
        session = ProcessSession(
            session_id=session_id,
            owner_thread_id=self._owner_thread_id,
            owner_turn_id=self._current_turn_id,
            command=command,
            cwd=relative_cwd,
            tty=tty,
            spawned=spawned,
            event_sink=self._event_sink,
            on_terminal=self._on_terminal,
            idle_timeout_seconds=self._idle_timeout_seconds,
            timeout_ms=timeout_ms,
            execution_profile=execution_profile,
        )
        self._sessions[session_id] = session
        self._emit(
            "command_started",
            {
                "session_id": session_id,
                "owner_thread_id": self._owner_thread_id,
                "owner_turn_id": self._current_turn_id,
                "command": command,
                "cwd": relative_cwd,
                "tty": tty,
                "execution_profile": getattr(
                    execution_profile,
                    "value",
                    execution_profile,
                ) or "workspace_write",
            },
        )
        try:
            result = await session.read(yield_time_ms)
        except asyncio.CancelledError:
            self._drop_session(session_id)
            await session.close()
            raise
        if result.metadata.get("status") == "exited":
            # The watcher may have moved this record to _exited_sessions. The
            # initial call already collected its final bytes, so it is safe to
            # retire it immediately; a process that exits after returning a
            # running result remains available for one final poll.
            self._drop_session(session_id)
            await session.close()
        return result

    async def write_stdin(
        self,
        session_id: str,
        chars: str = "",
        yield_time_ms: int = 1000,
    ) -> ToolResult:
        if self._closed:
            raise ToolOperationError("PROCESS_MANAGER_CLOSED", "process manager is closed")
        session = self._sessions.get(session_id)
        if session is None:
            completed = self._exited_sessions.get(session_id)
            if completed is not None:
                if chars:
                    self._mark_dead(session_id)
                    self._exited_sessions.pop(session_id, None)
                    raise ToolOperationError("SESSION_DEAD", "process session has exited")
                self._exited_sessions.pop(session_id, None)
                self._mark_dead(session_id)
                return deepcopy(completed)
        if session is None:
            if session_id in self._dead_sessions:
                raise ToolOperationError("SESSION_DEAD", "process session has exited")
            raise ToolOperationError("SESSION_NOT_FOUND", "unknown process session")
        interaction_turn_id = self._current_turn_id
        try:
            if chars and not session.running:
                raise ToolOperationError("SESSION_DEAD", "process session has exited")
            result = await session.interact(chars, yield_time_ms)
        except asyncio.CancelledError:
            # A later Turn may poll or write a Thread-persistent Session without
            # taking ownership of it.  Cancelling that interaction must unwind
            # only the caller; owner-filtered Turn cleanup below will terminate
            # the Session iff this Turn actually created it.
            if session.owner_turn_id == interaction_turn_id:
                self._drop_session(session_id)
                await session.close()
            raise
        if result.metadata.get("status") == "exited":
            self._drop_session(session_id)
            await session.close()
        return result

    def cancel_active(self, owner_turn_id: str | None = None) -> None:
        """Terminate sessions owned by one Turn without touching other Turns.

        ``owner_turn_id=None`` is reserved for Thread/Host shutdown callers
        that explicitly intend to close every remaining session.
        """

        sessions = [
            session
            for session in self._sessions.values()
            if owner_turn_id is None or session.owner_turn_id == owner_turn_id
        ]
        for session in sessions:
            self._sessions.pop(session.session_id, None)
            self._mark_dead(session.session_id)
            session.close_sync()
            self._schedule_cleanup(session)
        # Naturally completed results are no longer active resources and keep
        # their one bounded final-poll contract even if their creator Turn is
        # cancelled after process exit.

    def close(self) -> None:
        """Idempotently terminate sessions and release the sandbox resource."""

        if self._closed:
            return
        self._closed = True
        sessions = [*self._sessions.values()]
        self._sessions.clear()
        self._exited_sessions.clear()
        self._dead_sessions.clear()
        for session in sessions:
            session.close_sync()
        for session in sessions:
            self._schedule_cleanup(session)
        self._release_sandbox()

    async def aclose(self) -> None:
        """Awaitable close for Host lifecycles that can drain child tasks."""

        if (
            self._closed
            and not self._sessions
            and not self._exited_sessions
            and not self._cleanup_tasks
        ):
            self._release_sandbox()
            return
        self._closed = True
        sessions = [*self._sessions.values()]
        self._sessions.clear()
        self._exited_sessions.clear()
        self._dead_sessions.clear()
        await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)
        cleanup_tasks = tuple(self._cleanup_tasks)
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        self._release_sandbox()

    def _release_sandbox(self) -> None:
        if self._sandbox_released:
            return
        self._sandbox_released = True
        self._sandbox.close()

    def _drop_session(self, session_id: str) -> None:
        removed = self._sessions.pop(session_id, None)
        removed = self._exited_sessions.pop(session_id, removed)
        if removed is not None:
            self._mark_dead(session_id)

    def _remember_completed(self, session_id: str, result: ToolResult) -> None:
        self._exited_sessions[session_id] = deepcopy(result)
        self._exited_sessions.move_to_end(session_id)
        while len(self._exited_sessions) > self._max_completed_sessions:
            evicted_id, _ = self._exited_sessions.popitem(last=False)
            self._mark_dead(evicted_id)

    def _mark_dead(self, session_id: str) -> None:
        self._dead_sessions[session_id] = None
        self._dead_sessions.move_to_end(session_id)
        while len(self._dead_sessions) > self._max_dead_sessions:
            self._dead_sessions.popitem(last=False)

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._event_sink is not None:
            try:
                self._event_sink(event_type, payload)
            except Exception:
                return
