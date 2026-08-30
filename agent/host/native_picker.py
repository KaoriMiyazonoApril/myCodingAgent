"""Native Windows folder selection for a Host running under WSL2.

The adapter in this module is deliberately small: a native dialog only returns
the user's selection.  Workspace authorization remains the responsibility of
``WorkspaceBrowser`` in the Host transport layer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import contextlib
from dataclasses import dataclass
import inspect
import json
import os
import platform
import shutil
from typing import Any, Protocol


NATIVE_PICKER_SCHEMA_VERSION = 1


class NativePickerError(RuntimeError):
    """Base class for failures that can be safely mapped by Host transport."""

    code = "NATIVE_PICKER_FAILED"


class NativePickerUnsupportedError(NativePickerError):
    code = "NATIVE_PICKER_UNSUPPORTED"


class WindowsInteropUnavailableError(NativePickerError):
    code = "WINDOWS_INTEROP_UNAVAILABLE"


class NativePickerProcessError(NativePickerError):
    code = "NATIVE_PICKER_FAILED"


class NativePickerInvalidResultError(NativePickerError):
    code = "NATIVE_PICKER_INVALID_RESULT"


class WslPathTranslationError(NativePickerError):
    code = "WSL_PATH_TRANSLATION_FAILED"


class NativePickerBusyError(NativePickerError):
    code = "NATIVE_PICKER_BUSY"


@dataclass(frozen=True, slots=True)
class NativePickerCapability:
    """Public capability result returned by the Host endpoint."""

    available: bool
    reason_code: str | None = None
    schema_version: int = NATIVE_PICKER_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "available": self.available,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class NativePickerSelection:
    """Result of the native dialog before WSL path translation."""

    status: str
    windows_path: str | None = None

    @classmethod
    def selected(cls, windows_path: str) -> "NativePickerSelection":
        return cls(status="selected", windows_path=windows_path)

    @classmethod
    def cancelled(cls) -> "NativePickerSelection":
        return cls(status="cancelled")

    @property
    def is_cancelled(self) -> bool:
        return self.status == "cancelled"


class NativePickerAdapter(Protocol):
    """Host seam for platform selection and path translation."""

    def capability(self) -> NativePickerCapability: ...

    async def select(self) -> NativePickerSelection: ...

    async def translate(self, windows_path: str) -> str: ...

    async def close(self) -> None: ...


# Keep the script fixed and trusted.  The selected path crosses the process
# boundary only as JSON; it is never interpolated into PowerShell source.
WINDOWS_FOLDER_DIALOG_SCRIPT = r"""
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Select a workspace folder'
try {
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        [ordered]@{ status = 'selected'; path = $dialog.SelectedPath } |
            ConvertTo-Json -Compress
    } else {
        [ordered]@{ status = 'cancelled' } | ConvertTo-Json -Compress
    }
} finally {
    $dialog.Dispose()
}
""".strip()


SubprocessFactory = Callable[..., Awaitable[Any]]
WhichFunction = Callable[[str], str | None]


class NativeWindowsFolderPicker:
    """Launch a Windows folder dialog and translate its result in WSL.

    The constructor accepts system-boundary seams for deterministic platform
    tests.  Production uses ``asyncio.create_subprocess_exec`` with a fixed
    PowerShell script and the system ``wslpath`` executable.
    """

    _PROCESS_REAP_TIMEOUT_SECONDS = 2.0
    _TASK_REAP_TIMEOUT_SECONDS = 2.0

    def __init__(
        self,
        *,
        is_wsl: Callable[[], bool] | None = None,
        which: WhichFunction = shutil.which,
        powershell_executable: str | None = None,
        wslpath_executable: str | None = None,
        subprocess_factory: SubprocessFactory = asyncio.create_subprocess_exec,
        dialog_script: str = WINDOWS_FOLDER_DIALOG_SCRIPT,
    ) -> None:
        self._is_wsl_override = is_wsl
        self._which = which
        self._powershell_executable = powershell_executable
        self._wslpath_executable = wslpath_executable
        self._subprocess_factory = subprocess_factory
        self._dialog_script = dialog_script
        self._active_task: asyncio.Task[Any] | None = None
        self._active_process: Any | None = None

    def capability(self) -> NativePickerCapability:
        """Detect WSL and both required interop executables without spawning."""

        if not self._running_under_wsl():
            return NativePickerCapability(
                available=False,
                reason_code="NATIVE_PICKER_UNSUPPORTED",
            )

        powershell = self._find_powershell()
        wslpath = self._find_wslpath()
        if powershell is None or wslpath is None:
            return NativePickerCapability(
                available=False,
                reason_code="WINDOWS_INTEROP_UNAVAILABLE",
            )
        return NativePickerCapability(available=True)

    async def select(self) -> NativePickerSelection:
        """Wait for one dialog selection, preserving cancellation as a result."""

        if self._active_task is not None and not self._active_task.done():
            raise NativePickerBusyError("A native picker is already open")
        capability = self.capability()
        if not capability.available:
            if capability.reason_code == "NATIVE_PICKER_UNSUPPORTED":
                raise NativePickerUnsupportedError("Native picker is not supported")
            raise WindowsInteropUnavailableError(
                "Windows interop required by native picker is unavailable"
            )

        task = asyncio.current_task()
        self._active_task = task
        try:
            return await self._run_dialog()
        finally:
            self._active_process = None
            if self._active_task is task:
                self._active_task = None

    async def translate(self, windows_path: str) -> str:
        """Translate one dialog path using system ``wslpath`` via argv."""

        if self._active_task is not None and not self._active_task.done():
            raise NativePickerBusyError("A native picker is already active")
        if not isinstance(windows_path, str) or not windows_path:
            raise WslPathTranslationError("Native picker returned an empty path")
        capability = self.capability()
        if not capability.available:
            if capability.reason_code == "NATIVE_PICKER_UNSUPPORTED":
                raise NativePickerUnsupportedError("Native picker is not supported")
            raise WindowsInteropUnavailableError(
                "Windows interop required by native picker is unavailable"
            )
        executable = self._find_wslpath()
        if executable is None:
            raise WindowsInteropUnavailableError(
                "wslpath is unavailable for Windows path translation"
            )

        task = asyncio.current_task()
        self._active_task = task
        process: Any | None = None
        try:
            process = await self._subprocess_factory(
                executable,
                "-u",
                windows_path,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._active_process = process
            stdout, _stderr = await process.communicate()
        except asyncio.CancelledError:
            if process is not None:
                await self._terminate_process(process)
            raise
        except FileNotFoundError as error:
            raise WslPathTranslationError("wslpath could not be started") from error
        except OSError as error:
            raise WslPathTranslationError("wslpath could not be started") from error
        finally:
            if self._active_process is process:
                self._active_process = None
            if self._active_task is task:
                self._active_task = None

        if getattr(process, "returncode", None) not in (0, None):
            raise WslPathTranslationError("wslpath returned a failure")
        try:
            translated = _decode_translation_output(stdout)
        except (UnicodeDecodeError, TypeError) as error:
            raise WslPathTranslationError("wslpath returned invalid UTF-8") from error
        if not translated or not os.path.isabs(translated) or "\x00" in translated:
            raise WslPathTranslationError("wslpath returned an invalid Linux path")
        return translated

    async def close(self) -> None:
        """Terminate and reap an active child/task; safe to call repeatedly."""

        process = self._active_process
        task = self._active_task
        if process is not None:
            await self._terminate_process(process)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=self._TASK_REAP_TIMEOUT_SECONDS,
                )
        self._active_process = None
        if self._active_task is task:
            self._active_task = None

    async def shutdown(self) -> None:
        """Alias used by callers that name Host resource teardown shutdown."""

        await self.close()

    async def _run_dialog(self) -> NativePickerSelection:
        executable = self._find_powershell()
        if executable is None:
            raise WindowsInteropUnavailableError("PowerShell interop is unavailable")

        process: Any | None = None
        try:
            process = await self._subprocess_factory(
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Sta",
                "-Command",
                self._dialog_script,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._active_process = process
            stdout, _stderr = await process.communicate()
        except asyncio.CancelledError:
            if process is not None:
                await self._terminate_process(process)
            raise
        except FileNotFoundError as error:
            raise WindowsInteropUnavailableError(
                "PowerShell interop could not be started"
            ) from error
        except OSError as error:
            raise NativePickerProcessError("Native picker process could not start") from error
        finally:
            if self._active_process is process:
                self._active_process = None

        if getattr(process, "returncode", None) not in (0, None):
            raise NativePickerProcessError("Native picker process failed")
        return _parse_dialog_result(stdout)

    def _find_powershell(self) -> str | None:
        if self._powershell_executable is not None:
            return self._powershell_executable
        for name in ("powershell.exe", "pwsh.exe"):
            executable = self._which(name)
            if executable:
                self._powershell_executable = executable
                return executable
        return None

    def _find_wslpath(self) -> str | None:
        if self._wslpath_executable is not None:
            return self._wslpath_executable
        executable = self._which("wslpath")
        if executable:
            self._wslpath_executable = executable
        return executable

    def _running_under_wsl(self) -> bool:
        if self._is_wsl_override is not None:
            return bool(self._is_wsl_override())
        if platform.system().lower() != "linux":
            return False
        release = platform.release().lower()
        if "microsoft" in release or "wsl" in release:
            return True
        # WSL_INTEROP is set by WSL for normal distro processes and is a useful
        # fallback when a test/container masks the kernel release string.
        return bool(os.environ.get("WSL_INTEROP"))

    async def _terminate_process(self, process: Any) -> None:
        returncode = getattr(process, "returncode", None)
        if returncode is None:
            terminate = getattr(process, "terminate", None)
            if callable(terminate):
                with contextlib.suppress(OSError):
                    terminate()
            wait = getattr(process, "wait", None)
            if callable(wait):
                await self._bounded_wait(wait)
        if getattr(process, "returncode", None) is None:
            kill = getattr(process, "kill", None)
            if callable(kill):
                with contextlib.suppress(OSError):
                    kill()
            wait = getattr(process, "wait", None)
            if callable(wait):
                await self._bounded_wait(wait)

    async def _bounded_wait(self, wait: Callable[[], Any]) -> None:
        with contextlib.suppress(asyncio.CancelledError, OSError, TimeoutError):
            result = wait()
            if inspect.isawaitable(result):
                await asyncio.wait_for(
                    result,
                    timeout=self._PROCESS_REAP_TIMEOUT_SECONDS,
                )


def _parse_dialog_result(stdout: bytes | str | None) -> NativePickerSelection:
    try:
        if isinstance(stdout, bytes):
            text = stdout.decode("utf-8")
        elif isinstance(stdout, str):
            text = stdout
        else:
            raise TypeError("dialog output is not text")
        payload = json.loads(text.strip())
    except (UnicodeDecodeError, TypeError, json.JSONDecodeError) as error:
        raise NativePickerInvalidResultError(
            "Native picker returned malformed JSON"
        ) from error
    if not isinstance(payload, Mapping):
        raise NativePickerInvalidResultError("Native picker result is not an object")
    status = payload.get("status")
    if status == "cancelled":
        return NativePickerSelection.cancelled()
    if status == "selected":
        path = payload.get("path")
        if not isinstance(path, str) or not path or not path.strip():
            raise NativePickerInvalidResultError(
                "Native picker selected result has no path"
            )
        return NativePickerSelection.selected(path)
    raise NativePickerInvalidResultError("Native picker returned an unknown status")


def _decode_translation_output(stdout: bytes | str | None) -> str:
    if isinstance(stdout, bytes):
        text = stdout.decode("utf-8")
    elif isinstance(stdout, str):
        text = stdout
    else:
        raise TypeError("translation output is not text")
    # wslpath emits one newline.  Remove line endings only; spaces are part of
    # a legitimate path and must survive the boundary unchanged.
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith(("\n", "\r")):
        text = text[:-1]
    if "\n" in text or "\r" in text:
        return ""
    return text


__all__ = [
    "NATIVE_PICKER_SCHEMA_VERSION",
    "WINDOWS_FOLDER_DIALOG_SCRIPT",
    "NativePickerAdapter",
    "NativePickerBusyError",
    "NativePickerCapability",
    "NativePickerError",
    "NativePickerInvalidResultError",
    "NativePickerProcessError",
    "NativePickerSelection",
    "NativePickerUnsupportedError",
    "NativeWindowsFolderPicker",
    "WindowsInteropUnavailableError",
    "WslPathTranslationError",
]
