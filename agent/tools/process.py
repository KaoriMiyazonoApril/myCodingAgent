"""Non-interactive, workspace-scoped one-shot command execution."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import BinaryIO

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
        self._lock = threading.Lock()

    def consume(self, stream: BinaryIO) -> None:
        while chunk := stream.read(8192):
            with self._lock:
                self._total_bytes += len(chunk)
                if len(self._head) < OUTPUT_LIMIT_BYTES:
                    needed = OUTPUT_LIMIT_BYTES - len(self._head)
                    self._head.extend(chunk[:needed])
                self._tail.extend(chunk)
                if len(self._tail) > OUTPUT_LIMIT_BYTES:
                    del self._tail[: len(self._tail) - OUTPUT_LIMIT_BYTES]

    def result(self) -> tuple[str, bool]:
        with self._lock:
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

    def __init__(self, filesystem: WorkspaceFilesystem) -> None:
        self._filesystem = filesystem

    def run(self, command: str, cwd: str, timeout_ms: int) -> ToolResult:
        working_directory, relative_cwd = self._filesystem.resolve(cwd)
        if not working_directory.exists():
            raise ToolOperationError("NOT_FOUND", f"directory not found: {relative_cwd}")
        if not working_directory.is_dir():
            raise ToolOperationError("NOT_A_FILE", f"not a directory: {relative_cwd}")

        shell, sandboxed = self._shell_command(command, relative_cwd)
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
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_thread = threading.Thread(
            target=stdout_capture.consume, args=(process.stdout,), daemon=True
        )
        stderr_thread = threading.Thread(
            target=stderr_capture.consume, args=(process.stderr,), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        try:
            process.wait(timeout=timeout_ms / 1000)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_process_group(process)
        self._join_capture_thread(stdout_thread, process.stdout)
        self._join_capture_thread(stderr_thread, process.stderr)
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
            "sandboxed": sandboxed,
        }
        if timed_out:
            status = f"command timed out after {timeout_ms} ms"
        elif process.returncode == 0:
            status = "command completed successfully"
        else:
            status = f"command exited with status {process.returncode}"
        content = f"{status}\nstdout:\n{stdout_text}\nstderr:\n{stderr_text}"
        if timed_out:
            return ToolResult(content=content, metadata=metadata, error_code="TIMEOUT")
        return ToolResult(content=content, metadata=metadata)

    def _shell_command(self, command: str, relative_cwd: str) -> tuple[list[str], bool]:
        return self._isolated_linux_command(command, relative_cwd), True

    def _isolated_linux_command(self, command: str, relative_cwd: str) -> list[str]:
        if not sys.platform.startswith("linux"):
            raise ToolOperationError(
                "PROCESS_START_FAILED",
                "workspace command isolation is currently available only on Linux",
            )
        bubblewrap = shutil.which("bwrap")
        if bubblewrap is None:
            raise ToolOperationError(
                "PROCESS_START_FAILED",
                "bubblewrap is required for workspace command isolation",
            )
        sandbox_cwd = "/workspace"
        if relative_cwd != ".":
            sandbox_cwd = f"/workspace/{relative_cwd}"
        return [
            bubblewrap,
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
            str(self._filesystem.root),
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

    @staticmethod
    def _join_capture_thread(thread: threading.Thread, stream: BinaryIO) -> None:
        thread.join(timeout=0.1)
        if thread.is_alive():
            stream.close()
            thread.join(timeout=0.05)

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
        if os.name == "nt":
            process.kill()
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            return
        try:
            process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    return
