"""Non-interactive, workspace-scoped one-shot command execution."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time

from agent.tools.filesystem import ToolOperationError, WorkspaceFilesystem
from agent.tools.types import ToolResult


OUTPUT_LIMIT_BYTES = 100 * 1024


class _BoundedOutput:
    """Drain a process stream while retaining only its useful bounded portions."""

    _MARKER = b"\n... output truncated ...\n"

    def __init__(self) -> None:
        self._head = bytearray()
        self._tail = bytearray()
        self._total_bytes = 0

    def consume(self, stream: object) -> None:
        while chunk := stream.read(8192):
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
        kept = bytes(self._head[:head_size]) + self._MARKER + bytes(self._tail[-tail_size:])
        return kept.decode("utf-8", errors="replace"), True


class CommandRunner:
    """Execute one command from a filesystem-validated initial directory."""

    def __init__(self, filesystem: WorkspaceFilesystem) -> None:
        self._filesystem = filesystem

    def run(self, command: object, cwd: object, timeout_ms: object) -> ToolResult:
        if not isinstance(command, str) or not command:
            raise ToolOperationError("INVALID_ARGUMENTS", "command must be a non-empty string")
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 1 <= timeout_ms <= 300000:
            raise ToolOperationError(
                "INVALID_ARGUMENTS", "timeout_ms must be an integer from 1 through 300000"
            )
        working_directory, relative_cwd = self._filesystem.resolve(cwd)
        if not working_directory.exists():
            raise ToolOperationError("NOT_FOUND", f"directory not found: {relative_cwd}")
        if not working_directory.is_dir():
            raise ToolOperationError("NOT_A_FILE", f"not a directory: {relative_cwd}")

        shell = self._shell_command(command)
        start = time.monotonic()
        try:
            process = subprocess.Popen(
                shell,
                cwd=working_directory,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=os.name != "nt",
            )
        except OSError as error:
            raise ToolOperationError("PROCESS_START_FAILED", f"could not start command: {error}") from error

        stdout_capture = _BoundedOutput()
        stderr_capture = _BoundedOutput()
        stdout_thread = threading.Thread(target=stdout_capture.consume, args=(process.stdout,))
        stderr_thread = threading.Thread(target=stderr_capture.consume, args=(process.stderr,))
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        try:
            process.wait(timeout=timeout_ms / 1000)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_process_group(process)
            process.wait()
        stdout_thread.join()
        stderr_thread.join()
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
        }
        content = f"stdout:\n{stdout_text}\nstderr:\n{stderr_text}"
        if timed_out:
            return ToolResult(content=content, metadata=metadata, error_code="TIMEOUT")
        return ToolResult(content=content, metadata=metadata)

    @staticmethod
    def _shell_command(command: str) -> list[str]:
        if os.name == "nt":
            shell = shutil.which("pwsh") or shutil.which("powershell")
            if shell is None:
                raise ToolOperationError("PROCESS_START_FAILED", "PowerShell is unavailable")
            return [shell, "-NoProfile", "-NonInteractive", "-Command", command]
        shell = shutil.which("bash") or shutil.which("sh")
        if shell is None:
            raise ToolOperationError("PROCESS_START_FAILED", "no supported shell is available")
        return [shell, "-c", command]

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
        if os.name == "nt":
            process.kill()
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
