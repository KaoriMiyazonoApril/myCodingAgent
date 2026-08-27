"""Non-interactive, workspace-scoped command execution."""

from __future__ import annotations

import asyncio
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


class _BubblewrapSandbox:
    """Linux bubblewrap command construction kept inside the process module."""

    def __init__(self) -> None:
        self._executable: str | None = None

    def check_available(self, workspace_root: Path) -> None:
        """Fail during runtime composition when bubblewrap cannot execute."""
        if not sys.platform.startswith("linux"):
            raise CommandSandboxUnavailableError(
                "bubblewrap command isolation requires Linux or WSL2"
            )
        executable = shutil.which("bwrap")
        if executable is None:
            raise CommandSandboxUnavailableError(
                "bubblewrap is required for workspace command isolation"
            )
        self._executable = executable
        probe_command = self.build_command(
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

    def build_command(
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
        sandbox_cwd = "/workspace"
        if relative_cwd != ".":
            sandbox_cwd = f"/workspace/{relative_cwd}"
        return [
            self._executable,
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
    ) -> None:
        self._filesystem = filesystem
        self._sandbox = _BubblewrapSandbox()
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

        shell = self._sandbox.build_command(
            workspace_root=self._filesystem.root,
            command=command,
            relative_cwd=relative_cwd,
        )
        start = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *shell,
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

        duration_ms = round((time.monotonic() - start) * 1000)
        stdout_text, stdout_truncated = stdout_capture.result()
        stderr_text, stderr_truncated = stderr_capture.result()
        metadata = {
            "cwd": relative_cwd,
            "duration_ms": duration_ms,
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "command_succeeded": not timed_out and process.returncode == 0,
            "sandboxed": True,
        }
        if timed_out:
            status = f"command timed out after {timeout_ms} ms"
            error_code = "TIMEOUT"
        elif process.returncode == 0:
            status = "command completed successfully"
            error_code = None
        else:
            status = f"command exited with status {process.returncode}"
            error_code = "COMMAND_FAILED"
        content = f"{status}\nstdout:\n{stdout_text}\nstderr:\n{stderr_text}"
        return ToolResult(content=content, metadata=metadata, error_code=error_code)

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
