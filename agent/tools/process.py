"""Non-interactive, workspace-scoped command execution."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

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


class CommandSandboxBackend(ABC):
    """Capability-probed, cancellable command execution boundary."""

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
            await self._cancel_tasks(tasks)

    async def _cleanup_cancelled(
        self,
        process: asyncio.subprocess.Process,
        wait_task: asyncio.Task[int],
        capture_tasks: list[asyncio.Task[None]],
    ) -> None:
        await self._terminate_process_group(process, wait_task)
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
                await asyncio.gather(wait_task, return_exceptions=True)

    @staticmethod
    def _signal_process_group(process_id: int, signal_number: signal.Signals) -> None:
        try:
            os.killpg(process_id, signal_number)
        except OSError:
            pass

    @staticmethod
    async def _cancel_tasks(tasks: list[asyncio.Task[None]]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class BubblewrapSandboxBackend(CommandSandboxBackend):
    """Production Linux bubblewrap command sandbox."""

    def __init__(self) -> None:
        self._executable: str | None = None

    def check_available(self, workspace_root: Path) -> None:
        """Fail during runtime composition when bubblewrap cannot execute."""
        self._executable = None
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
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CommandSandboxUnavailableError(
                f"bubblewrap capability probe failed: {error}"
            ) from error
        if probe.returncode != 0:
            details = probe.stderr.decode("utf-8", errors="replace").strip()
            suffix = f": {details}" if details else ""
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
        )

    @staticmethod
    def _command(
        *,
        executable: str,
        workspace_root: Path,
        command: str,
        relative_cwd: str,
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
