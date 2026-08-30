"""Non-interactive, workspace-scoped command execution."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
import ctypes
import ctypes.util
from dataclasses import dataclass
import errno
import os
import pty
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

from agent.tools.filesystem import ToolOperationError, WorkspaceFilesystem
from agent.tools.types import ToolResult


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
    ) -> SandboxExecution:
        """Execute one command; cancellation terminates its process group."""

        invocation = self._build_command(
            workspace_root=workspace_root,
            command=command,
            relative_cwd=relative_cwd,
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
    ) -> _SpawnedProcess:
        """Start a persistent sandboxed process using the existing invocation seam."""

        invocation = self._build_command(
            workspace_root=workspace_root,
            command=command,
            relative_cwd=relative_cwd,
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
        try:
            self._seccomp_fd = self._create_link_blocking_filter()
        except (OSError, ValueError) as error:
            raise CommandSandboxUnavailableError(
                f"workspace link-blocking seccomp is unavailable: {error}"
            ) from error
        probe_command = self._command(
            executable=executable,
            workspace_root=workspace_root,
            command="true",
            relative_cwd=".",
            seccomp_fd=self._seccomp_fd,
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
        enforcement_probe = self._command(
            executable=executable,
            workspace_root=workspace_root,
            command=(
                "touch /tmp/link-target; "
                "ln -s link-target /tmp/symbolic-probe 2>/dev/null && exit 99; "
                "ln /tmp/link-target /tmp/hard-probe 2>/dev/null && exit 99; "
                "exit 0"
            ),
            relative_cwd=".",
            seccomp_fd=self._seccomp_fd,
        )
        try:
            enforcement = subprocess.run(
                enforcement_probe,
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
                f"workspace link-blocking capability probe failed: {error}"
            ) from error
        if enforcement.returncode != 0:
            details = enforcement.stderr.decode("utf-8", errors="replace").strip()
            suffix = f": {details}" if details else ""
            self._reset_capability()
            raise CommandSandboxUnavailableError(
                "workspace link-blocking capability probe failed"
                f" with status {enforcement.returncode}{suffix}"
            )
        self._executable = executable

    def _build_command(
        self,
        *,
        workspace_root: Path,
        command: str,
        relative_cwd: str,
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
        seccomp_fd: int,
    ) -> list[str]:
        sandbox_cwd = "/workspace"
        if relative_cwd != ".":
            sandbox_cwd = f"/workspace/{relative_cwd}"
        return [
            executable,
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--seccomp",
            str(seccomp_fd),
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
            "--bind",
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

    def _reset_capability(self) -> None:
        self._executable = None
        if self._seccomp_fd is not None:
            os.close(self._seccomp_fd)
            self._seccomp_fd = None

    @staticmethod
    def _create_link_blocking_filter() -> int:
        library_name = ctypes.util.find_library("seccomp")
        if library_name is None:
            raise ValueError("libseccomp is required")
        library = ctypes.CDLL(library_name, use_errno=True)
        library.seccomp_init.argtypes = [ctypes.c_uint32]
        library.seccomp_init.restype = ctypes.c_void_p
        library.seccomp_release.argtypes = [ctypes.c_void_p]
        library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        library.seccomp_syscall_resolve_name.restype = ctypes.c_int
        library.seccomp_rule_add.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        library.seccomp_rule_add.restype = ctypes.c_int
        library.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
        library.seccomp_export_bpf.restype = ctypes.c_int

        seccomp_allow = 0x7FFF0000
        seccomp_errno = 0x00050000 | errno.EPERM
        context = library.seccomp_init(seccomp_allow)
        if not context:
            raise OSError(ctypes.get_errno(), "seccomp_init failed")
        descriptor = os.memfd_create("agent-link-seccomp", flags=0)
        try:
            for syscall_name in (b"symlink", b"symlinkat", b"link", b"linkat"):
                syscall_number = library.seccomp_syscall_resolve_name(syscall_name)
                if syscall_number < 0:
                    continue
                result = library.seccomp_rule_add(
                    context,
                    seccomp_errno,
                    syscall_number,
                    0,
                )
                if result < 0:
                    raise OSError(-result, f"could not block {syscall_name.decode()}")
            result = library.seccomp_export_bpf(context, descriptor)
            if result < 0:
                raise OSError(-result, "could not export seccomp filter")
            os.lseek(descriptor, 0, os.SEEK_SET)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise
        finally:
            library.seccomp_release(context)


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

    def close(self) -> None:
        """Release resources owned by the configured sandbox backend."""

        self._sandbox.close()

    @property
    def sandbox(self) -> CommandSandboxBackend:
        """Expose the already-probed sandbox to the per-Thread session manager."""

        return self._sandbox

    def run(self, command: str, cwd: str, timeout_ms: int) -> ToolResult:
        """Synchronous entry point for CLI code and synchronous tests."""

        return asyncio.run(self.run_async(command, cwd, timeout_ms))

    async def run_async(self, command: str, cwd: str, timeout_ms: int) -> ToolResult:
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
        )
        metadata = {
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
        command: str,
        cwd: str,
        tty: bool,
        spawned: _SpawnedProcess,
        event_sink: Callable[[str, dict[str, Any]], object] | None,
        on_terminal: Callable[[str], None] | None,
        idle_timeout_seconds: float,
        timeout_ms: int,
    ) -> None:
        self.session_id = session_id
        self.command = command
        self.cwd = cwd
        self.tty = tty
        self._spawned = spawned
        self._process = spawned.process
        self._event_sink = event_sink
        self._on_terminal = on_terminal
        self._idle_timeout_seconds = idle_timeout_seconds
        self._timeout_ms = timeout_ms
        self._pending: deque[_PendingOutput] = deque()
        self._pending_bytes = {"stdout": 0, "stderr": 0}
        self._pending_truncated = {"stdout": False, "stderr": False}
        self._captures = {"stdout": _BoundedOutput(), "stderr": _BoundedOutput()}
        self._activity = asyncio.Event()
        self._exited = False
        self._timed_out = False
        self._idle_timed_out = False
        self._closed = False
        self._last_interaction = time.monotonic()
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

    @property
    def running(self) -> bool:
        return not self._exited and self._process.returncode is None

    @property
    def exited(self) -> bool:
        return self._exited

    async def read(self, yield_time_ms: int) -> ToolResult:
        """Return only output accumulated since the previous read."""

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
        # A child may have produced its last bytes just before wait() made the
        # return code observable. Give the watcher one scheduling turn so the
        # terminal event is ordered after the final output delta.
        await asyncio.sleep(0)
        if self._process.returncode is not None and not self._exited:
            try:
                await asyncio.wait_for(asyncio.shield(self._wait_task), timeout=0.05)
            except TimeoutError:
                pass
        return self._result()

    async def write(self, chars: str) -> None:
        """Write raw stdin, treating a standalone Ctrl-C as SIGINT."""

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

    def close_sync(self) -> None:
        """Best-effort synchronous kill used by registry/Host close hooks."""

        if self.running:
            CommandSandboxBackend._signal_process_group(self._process.pid, signal.SIGTERM)
            CommandSandboxBackend._signal_process_group(self._process.pid, signal.SIGKILL)
        self._close_transports()

    def _result(self) -> ToolResult:
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
        return ToolResult(
            content=content,
            metadata={
                "status": status,
                "session_id": self.session_id,
                "cwd": self.cwd,
                "command": self.command,
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
                    "status": "exited",
                    "exit_code": self._process.returncode,
                    "timed_out": self._timed_out,
                    "idle_timed_out": self._idle_timed_out,
                    "tty": self.tty,
                },
            )
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
        self._sessions: dict[str, ProcessSession] = {}
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._sandbox_released = False
        self._closed = False

    def _on_terminal(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            session.close_sync()
            self._schedule_cleanup(session)

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
        self._event_sink = event_sink
        for session in self._sessions.values():
            session._event_sink = event_sink

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
        spawned = await self._sandbox.start_session(
            workspace_root=self._filesystem.root,
            working_directory=working_directory,
            relative_cwd=relative_cwd,
            command=command,
            tty=tty,
        )
        session = ProcessSession(
            session_id=session_id,
            command=command,
            cwd=relative_cwd,
            tty=tty,
            spawned=spawned,
            event_sink=self._event_sink,
            on_terminal=self._on_terminal,
            idle_timeout_seconds=self._idle_timeout_seconds,
            timeout_ms=timeout_ms,
        )
        self._sessions[session_id] = session
        self._emit(
            "command_started",
            {
                "session_id": session_id,
                "command": command,
                "cwd": relative_cwd,
                "tty": tty,
            },
        )
        try:
            result = await session.read(yield_time_ms)
        except asyncio.CancelledError:
            self._sessions.pop(session_id, None)
            await session.close()
            raise
        if result.metadata.get("status") == "exited":
            self._sessions.pop(session_id, None)
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
            raise ToolOperationError("SESSION_NOT_FOUND", "unknown process session")
        try:
            if chars:
                await session.write(chars)
            result = await session.read(yield_time_ms)
        except asyncio.CancelledError:
            self._sessions.pop(session_id, None)
            await session.close()
            raise
        if result.metadata.get("status") == "exited":
            self._sessions.pop(session_id, None)
            await session.close()
        return result

    def cancel_active(self) -> None:
        """Terminate all still-running sessions without waiting on the caller."""

        sessions = list(self._sessions.values())
        for session in sessions:
            session.close_sync()
        for session in sessions:
            self._schedule_cleanup(session)

    def close(self) -> None:
        """Idempotently terminate sessions and release the sandbox resource."""

        if self._closed:
            return
        self._closed = True
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            session.close_sync()
        for session in sessions:
            self._schedule_cleanup(session)
        self._release_sandbox()

    async def aclose(self) -> None:
        """Awaitable close for Host lifecycles that can drain child tasks."""

        if self._closed and not self._sessions and not self._cleanup_tasks:
            self._release_sandbox()
            return
        self._closed = True
        sessions = list(self._sessions.values())
        self._sessions.clear()
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

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._event_sink is not None:
            try:
                self._event_sink(event_type, payload)
            except Exception:
                return
