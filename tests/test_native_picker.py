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


def _picker(factory):
    return NativeWindowsFolderPicker(
        is_wsl=lambda: True,
        which=lambda name: f"/fake/{name}",
        subprocess_factory=factory,
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_capability_is_available_only_when_wsl_interop_tools_exist() -> None:
    available = NativeWindowsFolderPicker(
        is_wsl=lambda: True,
        which=lambda name: f"/fake/{name}",
    ).capability()
    unavailable = NativeWindowsFolderPicker(
        is_wsl=lambda: True,
        which=lambda name: None,
    ).capability()
    unsupported = NativeWindowsFolderPicker(
        is_wsl=lambda: False,
        which=lambda name: f"/fake/{name}",
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
    assert args[:6] == (
        "/fake/powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
    )
    assert args[6:8] == ("-Sta", "-Command")
    assert "System.Windows.Forms.FolderBrowserDialog" in args[8]
    assert kwargs["stdout"] is asyncio.subprocess.PIPE
    assert "shell" not in kwargs


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
