from __future__ import annotations

import asyncio
import json

import pytest

from agent.host.native_picker import (
    NativePickerBusyError,
    NativePickerInvalidResultError,
    NativePickerProcessError,
    NativeWindowsFolderPicker,
    WslPathTranslationError,
)


class _Process:
    def __init__(self, output: bytes, *, returncode: int = 0) -> None:
        self.output = output
        self.returncode = None
        self._final_returncode = returncode
        self.terminated = False

    async def communicate(self):
        await asyncio.sleep(0)
        self.returncode = self._final_returncode
        return self.output, b""

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    async def wait(self) -> int:
        return self.returncode or 0

    def kill(self) -> None:
        self.terminated = True
        self.returncode = -9


class _Stream:
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = list(chunks)

    async def readline(self) -> bytes:
        await asyncio.sleep(0)
        return self._chunks.pop(0) if self._chunks else b""

    async def read(self) -> bytes:
        await asyncio.sleep(0)
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data


class _StreamingProcess(_Process):
    def __init__(self, pid_line: bytes, result: bytes) -> None:
        super().__init__(b"")
        self.stdout = _Stream(pid_line, result)
        self.stderr = _Stream()

    async def wait(self) -> int:
        self.returncode = self._final_returncode
        return self.returncode


def _picker(factory):
    return NativeWindowsFolderPicker(
        is_wsl=lambda: True,
        which=lambda name: f"/fake/{name}",
        windows_launcher_executable="/fake/init",
        subprocess_factory=factory,
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_capability_is_available_only_when_wsl_interop_tools_exist() -> None:
    available = NativeWindowsFolderPicker(
        is_wsl=lambda: True,
        which=lambda name: f"/fake/{name}",
        windows_launcher_executable="/fake/init",
    ).capability()
    unavailable = NativeWindowsFolderPicker(
        is_wsl=lambda: True,
        which=lambda name: None,
        windows_launcher_executable="/fake/init",
    ).capability()
    unsupported = NativeWindowsFolderPicker(
        is_wsl=lambda: False,
        which=lambda name: f"/fake/{name}",
        windows_launcher_executable="/fake/init",
    ).capability()

    assert available.available is True
    assert available.reason_code is None
    assert unavailable.reason_code == "WINDOWS_INTEROP_UNAVAILABLE"
    assert unsupported.reason_code == "NATIVE_PICKER_UNSUPPORTED"


@pytest.mark.anyio
async def test_select_preserves_cancel_and_uses_fixed_argv_for_unicode_path() -> None:
    calls = []
    process = _Process(
        json.dumps(
            {"status": "selected", "path": r"C:\Work 中文\space"},
            ensure_ascii=False,
        ).encode()
    )

    async def factory(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    picker = _picker(factory)
    result = await picker.select()

    assert result.status == "selected"
    assert result.windows_path == r"C:\Work 中文\space"
    args, kwargs = calls[0]
    assert args[:7] == (
        "/fake/init",
        "/fake/powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
    )
    assert args[7:9] == ("-Sta", "-Command")
    assert "System.Windows.Forms.FolderBrowserDialog" in args[9]
    assert "status = 'started'; pid = $PID" in args[9]
    assert kwargs["stdout"] is asyncio.subprocess.PIPE
    assert "shell" not in kwargs


def test_capability_requires_a_windows_launch_bridge() -> None:
    picker = NativeWindowsFolderPicker(
        is_wsl=lambda: True,
        which=lambda name: f"/fake/{name}",
        windows_launcher_executable="",
        direct_interop_available=lambda: False,
    )

    capability = picker.capability()

    assert capability.available is False
    assert capability.reason_code == "WINDOWS_INTEROP_UNAVAILABLE"


@pytest.mark.anyio
async def test_cancel_is_a_normal_selection_result() -> None:
    async def factory(*args, **kwargs):
        return _Process(b'{"status":"cancelled"}')

    result = await _picker(factory).select()

    assert result.status == "cancelled"
    assert result.windows_path is None


@pytest.mark.anyio
async def test_translate_passes_raw_unicode_windows_path_and_keeps_spaces() -> None:
    calls = []

    async def factory(*args, **kwargs):
        calls.append((args, kwargs))
        return _Process("/mnt/c/Work 中文/project\n".encode())

    result = await _picker(factory).translate(r"C:\Work 中文\project")

    assert result == "/mnt/c/Work 中文/project"
    args, kwargs = calls[0]
    assert args == ("/fake/wslpath", "-u", r"C:\Work 中文\project")
    assert "shell" not in kwargs


@pytest.mark.anyio
async def test_malformed_process_output_and_translation_failure_are_typed() -> None:
    async def malformed(*args, **kwargs):
        return _Process(b"not-json")

    with pytest.raises(NativePickerInvalidResultError):
        await _picker(malformed).select()

    async def failed(*args, **kwargs):
        return _Process(b"", returncode=1)

    with pytest.raises(NativePickerProcessError):
        await _picker(failed).select()
    with pytest.raises(WslPathTranslationError):
        await _picker(failed).translate(r"C:\project")


@pytest.mark.anyio
async def test_translation_rejects_non_absolute_or_multiline_output() -> None:
    async def factory(*args, **kwargs):
        return _Process(b"relative/path\nsecond-line\n")

    with pytest.raises(WslPathTranslationError):
        await _picker(factory).translate(r"C:\project")


@pytest.mark.anyio
async def test_select_is_single_flight_and_close_is_idempotent() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    process = _Process(b'{"status":"cancelled"}')

    async def factory(*args, **kwargs):
        started.set()
        await release.wait()
        return process

    picker = _picker(factory)
    first = asyncio.create_task(picker.select())
    await started.wait()
    with pytest.raises(NativePickerBusyError):
        await picker.select()
    await picker.close()
    await picker.close()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first


@pytest.mark.anyio
async def test_close_terminates_windows_dialog_pid_before_wsl_wrapper() -> None:
    dialog = _StreamingProcess(
        b'{"status":"started","pid":4242}\n',
        b'{"status":"cancelled"}\n',
    )
    dialog_release = asyncio.Event()
    terminated_windows_pids: list[int] = []

    async def factory(*args, **kwargs):
        return dialog

    async def terminate_windows_process(pid: int) -> None:
        terminated_windows_pids.append(pid)
        dialog_release.set()

    original_read = dialog.stdout.read

    async def blocked_read() -> bytes:
        await dialog_release.wait()
        return await original_read()

    dialog.stdout.read = blocked_read
    picker = NativeWindowsFolderPicker(
        is_wsl=lambda: True,
        which=lambda name: f"/fake/{name}",
        windows_launcher_executable="/fake/init",
        subprocess_factory=factory,
        windows_process_terminator=terminate_windows_process,
    )

    selection = asyncio.create_task(picker.select())
    while picker._active_windows_pid is None:
        await asyncio.sleep(0)
    await picker.close()

    assert terminated_windows_pids == [4242]
    assert dialog.terminated is True
    with pytest.raises(asyncio.CancelledError):
        await selection


@pytest.mark.anyio
async def test_dialog_rejects_missing_or_malformed_pid_handshake() -> None:
    async def missing(*args, **kwargs):
        return _StreamingProcess(b'{"status":"cancelled"}\n', b"")

    with pytest.raises(NativePickerInvalidResultError):
        await _picker(missing).select()

    async def malformed(*args, **kwargs):
        return _StreamingProcess(b'{"status":"started","pid":"oops"}\n', b"")

    with pytest.raises(NativePickerInvalidResultError):
        await _picker(malformed).select()
