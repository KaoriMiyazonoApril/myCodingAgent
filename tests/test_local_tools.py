from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import shutil
import signal
import time
import tracemalloc
from types import SimpleNamespace

import pytest

from agent.core.messages import ToolCallBlock
from agent.runtime.policy import ExecutionProfile
from agent.tools.local import create_local_tool_registry
from agent.tools.local import MAX_READ_RETURN_BYTES
from agent.tools.filesystem import WorkspaceFilesystem, content_fingerprint
from agent.tools.process import (
    BubblewrapSandboxBackend,
    CommandSandboxBackend,
    CommandSandboxUnavailableError,
)
from agent.tools.registry import ToolRegistry
from agent.tools.types import ToolDefinition, ToolResult
from tests.sandbox_support import (
    DeterministicSandboxBackend,
    create_test_tool_registry,
)


def create_bubblewrap_tool_registry_or_skip(tmp_path) -> ToolRegistry:
    try:
        return create_local_tool_registry(
            tmp_path,
            sandbox_backend=BubblewrapSandboxBackend(),
        )
    except CommandSandboxUnavailableError as error:
        pytest.skip(str(error))


def test_local_tools_accept_an_explicit_command_sandbox_backend(tmp_path) -> None:
    backend = DeterministicSandboxBackend()
    registry = create_local_tool_registry(tmp_path, sandbox_backend=backend)

    result = registry.execute(
        ToolCallBlock(
            id="sandbox",
            name="run_command",
            arguments={"command": "printf deterministic"},
        )
    )

    assert backend.checked_workspaces == [tmp_path.resolve()]
    assert result.error_code is None
    assert result.metadata["stdout"] == "deterministic"


def test_read_file_requires_the_skill_tool_for_catalog_skill_instructions(
    tmp_path: Path,
) -> None:
    skill_file = tmp_path / ".agents" / "skills" / "alpha" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("private skill body", encoding="utf-8")
    registry = create_local_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="read-skill-directly",
            name="read_file",
            arguments={"path": ".agents/skills/alpha/SKILL.md"},
        )
    )

    assert result.error_code == "SKILL_TOOL_REQUIRED"
    assert "private skill body" not in result.content
    assert "skill" in result.content
    assert "Never use this" in registry.lookup("read_file").description  # type: ignore[union-attr]


def test_closing_local_tool_registry_releases_sandbox_once(tmp_path) -> None:
    backend = DeterministicSandboxBackend()
    registry = create_local_tool_registry(tmp_path, sandbox_backend=backend)

    assert registry.close() is True
    assert registry.close() is False
    result = registry.execute(
        ToolCallBlock(
            id="after-close",
            name="run_command",
            arguments={"command": "printf should-not-run"},
        )
    )

    assert backend.close_calls == 1
    assert result.error_code == "TOOL_REGISTRY_CLOSED"


def test_bubblewrap_close_releases_seccomp_descriptor_idempotently() -> None:
    backend = BubblewrapSandboxBackend()
    read_descriptor, write_descriptor = os.pipe()
    os.close(write_descriptor)
    backend._seccomp_fd = read_descriptor

    backend.close()
    backend.close()

    with pytest.raises(OSError):
        os.fstat(read_descriptor)
    assert backend._seccomp_fd is None


def test_falsey_explicit_sandbox_backend_is_not_replaced(tmp_path) -> None:
    class FalseySandboxBackend(DeterministicSandboxBackend):
        def __bool__(self) -> bool:
            return False

    backend = FalseySandboxBackend()
    registry = create_local_tool_registry(tmp_path, sandbox_backend=backend)

    result = registry.execute(
        ToolCallBlock(
            id="falsey_sandbox",
            name="run_command",
            arguments={"command": "printf explicit"},
        )
    )

    assert backend.checked_workspaces == [tmp_path.resolve()]
    assert result.metadata["stdout"] == "explicit"


def test_unavailable_injected_sandbox_fails_without_running_a_host_command(
    tmp_path,
) -> None:
    class UnavailableSandboxBackend(DeterministicSandboxBackend):
        def check_available(self, workspace_root: Path) -> None:
            raise CommandSandboxUnavailableError("test sandbox unavailable")

        def _build_command(
            self,
            *,
            workspace_root: Path,
            command: str,
            relative_cwd: str,
        ) -> list[str]:
            raise AssertionError("an unavailable backend must never execute")

    with pytest.raises(CommandSandboxUnavailableError, match="test sandbox unavailable"):
        create_local_tool_registry(
            tmp_path,
            sandbox_backend=UnavailableSandboxBackend(),
        )


def test_failed_bubblewrap_probe_does_not_leave_backend_executable(
    tmp_path, monkeypatch
) -> None:
    backend = BubblewrapSandboxBackend()
    monkeypatch.setattr("agent.tools.process.shutil.which", lambda _: "/usr/bin/bwrap")
    probe_results = iter((SimpleNamespace(returncode=1, stderr=b"user namespaces disabled"),))
    monkeypatch.setattr(
        "agent.tools.process.subprocess.run",
        lambda *args, **kwargs: next(probe_results),
    )

    async def unexpected_process_start(*args, **kwargs):
        raise AssertionError("a backend with a failed probe must not execute")

    monkeypatch.setattr(
        "agent.tools.process.asyncio.create_subprocess_exec",
        unexpected_process_start,
    )

    with pytest.raises(CommandSandboxUnavailableError, match="user namespaces disabled"):
        backend.check_available(tmp_path)
    with pytest.raises(CommandSandboxUnavailableError, match="capability check"):
        asyncio.run(
            backend.execute(
                workspace_root=tmp_path,
                working_directory=tmp_path,
                relative_cwd=".",
                command="true",
                timeout_ms=100,
            )
        )


def test_bubblewrap_profiles_change_mount_and_network_capabilities(
    tmp_path,
) -> None:
    def command(profile: ExecutionProfile) -> list[str]:
        return BubblewrapSandboxBackend._command(
            executable="/usr/bin/bwrap",
            workspace_root=tmp_path,
            command="true",
            relative_cwd=".",
            execution_profile=profile,
        )

    read_only = command(ExecutionProfile.READ_ONLY)
    writable = command(ExecutionProfile.WORKSPACE_WRITE)
    network = command(ExecutionProfile.WORKSPACE_WRITE_NETWORK)
    assert read_only[read_only.index("--ro-bind") + 1 : read_only.index("--chdir")]  # smoke-check command shape
    assert [str(tmp_path), "/workspace"] == read_only[
        read_only.index("--ro-bind", read_only.index("--tmpfs")) + 1 : read_only.index("--chdir")
    ]
    assert [str(tmp_path), "/workspace"] == writable[
        writable.index("--bind") + 1 : writable.index("--chdir")
    ]
    assert "--share-net" not in read_only
    assert "--share-net" not in writable
    assert "--share-net" in network
    assert "--ro-bind" in network
    assert "/etc/resolv.conf" in network


@pytest.fixture(params=("deterministic", "bubblewrap"))
def command_sandbox_backend(request, tmp_path) -> CommandSandboxBackend:
    if request.param == "deterministic":
        return DeterministicSandboxBackend()
    backend = BubblewrapSandboxBackend()
    try:
        backend.check_available(tmp_path)
    except CommandSandboxUnavailableError as error:
        pytest.skip(str(error))
    return backend


def test_command_sandbox_backends_share_execution_contract(
    tmp_path, command_sandbox_backend
) -> None:
    registry = create_local_tool_registry(
        tmp_path,
        sandbox_backend=command_sandbox_backend,
    )

    result = registry.execute(
        ToolCallBlock(
            id="contract",
            name="run_command",
            arguments={"command": "printf out; printf err >&2; exit 9"},
        )
    )

    assert result.error_code == "COMMAND_FAILED"
    assert result.metadata["exit_code"] == 9
    assert result.metadata["stdout"] == "out"
    assert result.metadata["stderr"] == "err"


def test_command_sandbox_backends_share_idempotent_close_contract(
    command_sandbox_backend,
) -> None:
    command_sandbox_backend.close()
    command_sandbox_backend.close()

    if isinstance(command_sandbox_backend, DeterministicSandboxBackend):
        assert command_sandbox_backend.close_calls == 1


def test_command_sandbox_backends_share_timeout_contract(
    tmp_path, command_sandbox_backend
) -> None:
    registry = create_local_tool_registry(
        tmp_path,
        sandbox_backend=command_sandbox_backend,
    )

    result = registry.execute(
        ToolCallBlock(
            id="timeout_contract",
            name="run_command",
            arguments={"command": "printf begun; sleep 1", "timeout_ms": 30},
        )
    )

    assert result.error_code == "TIMEOUT"
    assert result.metadata["timed_out"] is True
    assert result.metadata["stdout"] == "begun"


def test_read_file_returns_numbered_page_and_metadata(tmp_path) -> None:
    (tmp_path / "notes.txt").write_text("first\nsecond\nthird\n", encoding="utf-8")
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_read",
            name="read_file",
            arguments={"path": "notes.txt", "offset": 2, "limit": 1},
        )
    )

    assert result.error_code is None
    assert result.content == "2: second"
    assert result.metadata == {
        "path": "notes.txt",
        "requested_offset": 2,
        "requested_limit": 1,
        "returned_lines": 1,
        "total_lines": 3,
        "start_line": 2,
        "end_line": 2,
        "truncated": True,
        "line_truncated": False,
        "returned_bytes": len("2: second".encode("utf-8")),
        "original_selected_bytes": len("second\n".encode("utf-8")),
        "content_fingerprint": content_fingerprint(b"first\nsecond\nthird\n"),
    }


def test_read_text_page_streams_bounded_chunks_across_text_boundaries(
    tmp_path, monkeypatch
) -> None:
    content = "abαxyz\r\nabα\nlast"
    target = tmp_path / "chunked.txt"
    target.write_text(content, encoding="utf-8")
    filesystem = WorkspaceFilesystem(tmp_path)
    original_open = Path.open
    requested_sizes: list[int] = []

    class TrackingReader:
        def __init__(self, source) -> None:
            self._source = source

        def __enter__(self):
            self._source.__enter__()
            return self

        def __exit__(self, *args):
            return self._source.__exit__(*args)

        def read(self, size: int = -1) -> bytes:
            requested_sizes.append(size)
            return self._source.read(size)

    def tracking_open(path: Path, *args, **kwargs):
        source = original_open(path, *args, **kwargs)
        if path == target and args == ("rb",):
            return TrackingReader(source)
        return source

    monkeypatch.setattr("agent.tools.filesystem.READ_CHUNK_BYTES", 4)
    monkeypatch.setattr(Path, "open", tracking_open)
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("paged reads must not load the whole file")
        ),
    )

    page, total_lines, relative, fingerprint = filesystem.read_text_page(
        "chunked.txt", offset=2, limit=1
    )

    assert page == ["abα"]
    assert total_lines == 3
    assert relative == "chunked.txt"
    assert fingerprint == content_fingerprint(content.encode("utf-8"))
    assert len(requested_sizes) > 1
    assert set(requested_sizes) == {4}


@pytest.mark.parametrize(
    "content",
    [
        "",
        "one",
        "one\n",
        "\n",
        "one\n\n",
        "one\r\ntwo\rthree",
        "a\vb\fc\x1cd\x1de\x1ef\x85g\u2028h\u2029",
    ],
)
def test_streamed_read_preserves_splitlines_semantics(
    tmp_path, monkeypatch, content
) -> None:
    target = tmp_path / "lines.txt"
    target.write_text(content, encoding="utf-8")
    filesystem = WorkspaceFilesystem(tmp_path)
    monkeypatch.setattr("agent.tools.filesystem.READ_CHUNK_BYTES", 1)

    page, total_lines, relative, fingerprint = filesystem.read_text_page(
        "lines.txt", offset=1, limit=2000
    )

    assert page == content.splitlines()
    assert total_lines == len(content.splitlines())
    assert relative == "lines.txt"
    assert fingerprint == content_fingerprint(content.encode("utf-8"))


def test_streamed_read_discards_non_requested_long_line_content(tmp_path) -> None:
    target = tmp_path / "long-first-line.txt"
    chunk = b"x" * (64 * 1024)
    with target.open("wb") as output:
        for _ in range(64):
            output.write(chunk)
        output.write(b"\nsecond")
    filesystem = WorkspaceFilesystem(tmp_path)

    tracemalloc.start()
    try:
        page, total_lines, _, _ = filesystem.read_text_page(
            "long-first-line.txt", offset=2, limit=1
        )
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert page == ["second"]
    assert total_lines == 2
    assert peak_bytes < 1024 * 1024


def test_write_file_creates_parent_directories_and_text_file(tmp_path) -> None:
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_write",
            name="write_file",
            arguments={"path": "src/new.py", "content": "print('hello')\n"},
        )
    )

    assert result.error_code is None
    assert (tmp_path / "src" / "new.py").read_text(encoding="utf-8") == "print('hello')\n"
    assert result.metadata == {"path": "src/new.py", "bytes_written": 15}


def test_edit_file_replaces_one_exact_match(tmp_path) -> None:
    source = tmp_path / "app.py"
    source.write_text("value = 'old'\n", encoding="utf-8")
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_edit",
            name="edit_file",
            arguments={
                "path": "app.py",
                "old_string": "'old'",
                "new_string": "'new'",
            },
        )
    )

    assert result.error_code is None
    assert source.read_text(encoding="utf-8") == "value = 'new'\n"
    assert result.metadata == {"path": "app.py", "replacements": 1}


def test_glob_returns_sorted_workspace_relative_regular_files(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "b.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "notes.txt").write_text("", encoding="utf-8")
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_glob",
            name="glob",
            arguments={"path": "src", "pattern": "*.py"},
        )
    )

    assert result.error_code is None
    assert result.content == "src/a.py\nsrc/b.py"
    assert result.metadata == {"path": "src", "matches": 2, "truncated": False}


def test_grep_returns_matching_paths_line_numbers_and_lines(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("first\nneedle here\n", encoding="utf-8")
    (tmp_path / "src" / "skip.txt").write_text("needle too\n", encoding="utf-8")
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_grep",
            name="grep",
            arguments={"pattern": "needle", "include": "*.py"},
        )
    )

    assert result.error_code is None
    assert result.content == "src/main.py:2: needle here"
    assert result.metadata == {"path": ".", "matches": 1, "truncated": False}


def test_grep_returns_lines_in_file_order(tmp_path) -> None:
    source = tmp_path / "main.py"
    source.write_text("\n".join(["needle"] * 10) + "\n", encoding="utf-8")
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(id="call_order", name="grep", arguments={"pattern": "needle"})
    )

    assert result.error_code is None
    assert result.content.splitlines() == [f"main.py:{line}: needle" for line in range(1, 11)]


def test_grep_skips_non_text_files_and_keeps_searching(tmp_path) -> None:
    (tmp_path / "binary.bin").write_bytes(b"needle\x00")
    (tmp_path / "main.py").write_text("needle\n", encoding="utf-8")
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(id="call_binary_search", name="grep", arguments={"pattern": "needle"})
    )

    assert result.error_code is None
    assert result.content == "main.py:1: needle"


def test_glob_uses_relative_pathlib_matching_for_recursive_patterns(tmp_path) -> None:
    (tmp_path / "src" / "nested").mkdir(parents=True)
    (tmp_path / "src" / "root.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "nested" / "deep.py").write_text("", encoding="utf-8")
    registry = create_test_tool_registry(tmp_path)

    direct = registry.execute(
        ToolCallBlock(
            id="call_direct_glob",
            name="glob",
            arguments={"path": "src", "pattern": "*.py"},
        )
    )
    recursive = registry.execute(
        ToolCallBlock(
            id="call_recursive_glob",
            name="glob",
            arguments={"path": "src", "pattern": "**/*.py"},
        )
    )

    assert direct.error_code is None
    assert direct.content == "src/root.py"
    assert recursive.error_code is None
    assert recursive.content == "src/nested/deep.py\nsrc/root.py"


def test_search_follows_internal_workspace_symlinks_at_access_time(tmp_path) -> None:
    (tmp_path / "other.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "link.txt").symlink_to("../other.txt")
    registry = create_test_tool_registry(tmp_path)

    grep_result = registry.execute(
        ToolCallBlock(
            id="call_subtree_grep",
            name="grep",
            arguments={"path": "sub", "pattern": "needle"},
        )
    )
    glob_result = registry.execute(
        ToolCallBlock(
            id="call_subtree_glob",
            name="glob",
            arguments={"path": "sub", "pattern": "*.txt"},
        )
    )

    assert grep_result.error_code is None
    assert grep_result.content == "sub/link.txt:1: needle"
    assert glob_result.error_code is None
    assert glob_result.content == "sub/link.txt"


def test_glob_rejects_absolute_or_escaping_patterns(tmp_path) -> None:
    registry = create_test_tool_registry(tmp_path)

    for pattern in ("/outside/*.py", "../*.py"):
        result = registry.execute(
            ToolCallBlock(
                id="call_invalid_glob",
                name="glob",
                arguments={"pattern": pattern},
            )
        )
        assert result.error_code == "INVALID_ARGUMENTS"


def test_write_file_reports_a_file_parent_as_an_operational_error(tmp_path) -> None:
    (tmp_path / "parent").write_text("not a directory", encoding="utf-8")
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_file_parent",
            name="write_file",
            arguments={"path": "parent/child.txt", "content": "text"},
        )
    )

    assert result.error_code == "IO_ERROR"


def test_run_command_captures_separate_output_and_nonzero_exit(tmp_path) -> None:
    (tmp_path / "work").mkdir()
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_command",
            name="run_command",
            arguments={
                "command": "printf out; printf err >&2; exit 7",
                "cwd": "work",
            },
        )
    )

    assert result.error_code == "COMMAND_FAILED"
    assert result.is_error is True
    assert result.metadata["cwd"] == "work"
    assert result.metadata["exit_code"] == 7
    assert result.metadata["timed_out"] is False
    assert result.metadata["stdout"] == "out"
    assert result.metadata["stderr"] == "err"
    assert result.metadata["command_succeeded"] is False
    assert result.content.startswith("command exited with status 7")


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is unavailable")
def test_run_command_cannot_read_outside_workspace(tmp_path) -> None:
    registry = create_bubblewrap_tool_registry_or_skip(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_isolation",
            name="run_command",
            arguments={
                "command": (
                    "if test -r /etc/passwd; then printf exposed; "
                    "else printf isolated; fi"
                )
            },
        )
    )

    assert result.error_code is None
    assert result.metadata["stdout"] == "isolated"
    assert result.metadata["sandboxed"] is True


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is unavailable")
def test_run_command_can_write_inside_without_modifying_host_sibling(tmp_path) -> None:
    outside = tmp_path.parent / "outside-command.txt"
    outside.write_text("unchanged", encoding="utf-8")
    registry = create_bubblewrap_tool_registry_or_skip(tmp_path)

    inside = registry.execute(
        ToolCallBlock(
            id="call_inside_write",
            name="run_command",
            arguments={"command": "printf created > generated.txt"},
        )
    )
    outside_attempt = registry.execute(
        ToolCallBlock(
            id="call_outside_write",
            name="run_command",
            arguments={"command": "printf changed > ../outside-command.txt"},
        )
    )

    assert inside.metadata["command_succeeded"] is True
    assert (tmp_path / "generated.txt").read_text(encoding="utf-8") == "created"
    assert outside_attempt.error_code is None
    assert outside.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is unavailable")
def test_production_sandbox_allows_workspace_links_when_target_stays_inside(
    tmp_path,
) -> None:
    (tmp_path / "target.txt").write_text("target", encoding="utf-8")
    registry = create_bubblewrap_tool_registry_or_skip(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="blocked-links",
            name="run_command",
            arguments={
                "command": (
                    "ln -s target.txt symbolic.txt && "
                    "ln target.txt hard.txt && "
                    "test -f symbolic.txt -a -f hard.txt"
                )
            },
        )
    )

    assert result.error_code is None
    assert (tmp_path / "symbolic.txt").is_symlink()
    assert (tmp_path / "hard.txt").stat().st_ino == (tmp_path / "target.txt").stat().st_ino


def test_local_tool_composition_checks_command_isolation_capability_early(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("agent.tools.process.shutil.which", lambda _: None)

    with pytest.raises(RuntimeError, match="bubblewrap"):
        create_local_tool_registry(tmp_path)
    assert not (tmp_path / "must-not-run").exists()


def test_local_tool_composition_rejects_installed_but_unusable_bubblewrap(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("agent.tools.process.shutil.which", lambda _: "/usr/bin/bwrap")
    monkeypatch.setattr(
        "agent.tools.process.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stderr=b"user namespaces disabled"
        ),
    )

    with pytest.raises(RuntimeError, match="user namespaces disabled"):
        create_local_tool_registry(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="setsid is POSIX-only")
def test_timeout_returns_even_if_detached_descendant_keeps_pipe_open(tmp_path) -> None:
    registry = create_bubblewrap_tool_registry_or_skip(tmp_path)
    start = time.monotonic()

    result = registry.execute(
        ToolCallBlock(
            id="call_detached_timeout",
            name="run_command",
            arguments={
                "command": "setsid sh -c 'sleep 1' & printf begun; sleep 1",
                "timeout_ms": 30,
            },
        )
    )

    assert result.error_code == "TIMEOUT"
    assert time.monotonic() - start < 0.5
    assert result.metadata["stdout"] == "begun"


@pytest.mark.skipif(os.name == "nt", reason="setsid is POSIX-only")
def test_deterministic_timeout_has_bounded_wait_for_detached_pipe_owner(
    tmp_path,
) -> None:
    registry = create_local_tool_registry(
        tmp_path,
        sandbox_backend=DeterministicSandboxBackend(),
    )
    start = time.monotonic()

    result = registry.execute(
        ToolCallBlock(
            id="call_deterministic_detached_timeout",
            name="run_command",
            arguments={
                "command": "setsid sh -c 'sleep 1' & printf begun; sleep 1",
                "timeout_ms": 30,
            },
        )
    )

    assert result.error_code == "TIMEOUT"
    assert time.monotonic() - start < 0.5
    assert result.metadata["stdout"] == "begun"


@pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir(), reason="FD accounting requires procfs"
)
def test_repeated_detached_pipe_timeouts_do_not_retain_output_fds(tmp_path) -> None:
    registry = create_local_tool_registry(
        tmp_path,
        sandbox_backend=DeterministicSandboxBackend(),
    )
    pid_file = tmp_path / "detached-pids.txt"

    async def scenario() -> tuple[int, int]:
        before = len(os.listdir("/proc/self/fd"))
        try:
            for _ in range(5):
                result = await registry.execute_async(
                    ToolCallBlock(
                        id="detached-fd",
                        name="run_command",
                        arguments={
                            "command": (
                                "setsid sh -c 'echo $$ >> detached-pids.txt; "
                                "sleep 1000' & printf x; sleep 1"
                            ),
                            "timeout_ms": 30,
                        },
                    )
                )
                assert result.error_code == "TIMEOUT"
            await asyncio.sleep(0)
            return before, len(os.listdir("/proc/self/fd"))
        finally:
            if pid_file.exists():
                for raw_pid in pid_file.read_text(encoding="utf-8").splitlines():
                    try:
                        os.kill(int(raw_pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    before, after = asyncio.run(scenario())

    assert after <= before + 2


def test_task_cancellation_cleanup_has_a_fixed_wait_budget() -> None:
    async def scenario() -> tuple[float, bool]:
        release = asyncio.Event()

        async def cancellation_resistant() -> None:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        task = asyncio.create_task(cancellation_resistant())
        await asyncio.sleep(0)
        start = time.monotonic()
        await CommandSandboxBackend._cancel_tasks([task])
        elapsed = time.monotonic() - start
        still_running = not task.done()
        release.set()
        await task
        return elapsed, still_running

    elapsed, still_running = asyncio.run(scenario())

    assert elapsed < 0.3
    assert still_running


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX-only")
def test_completed_command_returns_even_if_background_child_keeps_pipe_open(tmp_path) -> None:
    registry = create_bubblewrap_tool_registry_or_skip(tmp_path)
    start = time.monotonic()

    result = registry.execute(
        ToolCallBlock(
            id="call_background_pipe",
            name="run_command",
            arguments={"command": "sleep 1 & printf done"},
        )
    )

    assert result.error_code is None
    assert time.monotonic() - start < 0.5
    assert result.metadata["stdout"] == "done"


def test_read_out_of_range_and_non_text_files_return_structured_results(tmp_path) -> None:
    (tmp_path / "short.txt").write_text("only\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\xff\x00")
    registry = create_test_tool_registry(tmp_path)

    page = registry.execute(
        ToolCallBlock(
            id="call_page", name="read_file", arguments={"path": "short.txt", "offset": 4}
        )
    )
    non_text = registry.execute(
        ToolCallBlock(id="call_binary", name="read_file", arguments={"path": "binary.bin"})
    )

    assert page.error_code is None
    assert page.content == ""
    assert page.metadata["requested_offset"] == 4
    assert page.metadata["start_line"] is None
    assert page.metadata["end_line"] is None
    assert page.metadata["truncated"] is False
    assert non_text.error_code == "NOT_TEXT"


def test_read_file_rejects_files_beyond_its_resource_limit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.tools.filesystem.MAX_TEXT_FILE_BYTES", 32, raising=False
    )
    (tmp_path / "large.txt").write_text("x" * 33, encoding="utf-8")
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_large_read",
            name="read_file",
            arguments={"path": "large.txt", "limit": 1},
        )
    )

    assert result.error_code == "FILE_TOO_LARGE"


def test_read_file_bounds_a_single_multimegabyte_line_by_utf8_bytes(tmp_path) -> None:
    source = ("界" * (MAX_READ_RETURN_BYTES * 4)) + "\nsecond"
    (tmp_path / "minified.txt").write_text(source, encoding="utf-8")
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_bounded_read",
            name="read_file",
            arguments={"path": "minified.txt", "limit": 2},
        )
    )

    returned = result.content.encode("utf-8")
    assert result.error_code is None
    assert len(returned) <= MAX_READ_RETURN_BYTES
    assert result.metadata["truncated"] is True
    assert result.metadata["line_truncated"] is True
    assert result.metadata["returned_bytes"] == len(returned)
    assert result.metadata["original_selected_bytes"] == len(source.encode("utf-8"))
    assert result.metadata["returned_lines"] == 1
    assert result.metadata["start_line"] == 1
    assert result.metadata["end_line"] == 1
    assert "truncated" in result.content


def test_read_file_byte_boundary_does_not_split_utf8_or_claim_unreturned_lines(
    tmp_path,
) -> None:
    source = "前" * (MAX_READ_RETURN_BYTES // 3) + "\nsecond\nthird"
    (tmp_path / "unicode.txt").write_text(source, encoding="utf-8")
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_unicode_bounded_read",
            name="read_file",
            arguments={"path": "unicode.txt", "limit": 3},
        )
    )

    assert result.error_code is None
    result.content.encode("utf-8")
    assert result.metadata["returned_bytes"] == len(result.content.encode("utf-8"))
    assert result.metadata["end_line"] == 1
    assert result.metadata["line_truncated"] is True
    assert result.metadata["total_lines"] == 3


def test_read_file_reports_truthful_crlf_bytes_across_chunks(
    tmp_path, monkeypatch
) -> None:
    source = b"one\r\ntwo\r\n"
    (tmp_path / "crlf.txt").write_bytes(source)
    monkeypatch.setattr("agent.tools.filesystem.READ_CHUNK_BYTES", 4)
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_crlf_bounded_read",
            name="read_file",
            arguments={"path": "crlf.txt", "offset": 1, "limit": 2},
        )
    )

    assert result.error_code is None
    assert result.content == "1: one\n2: two"
    assert result.metadata["returned_bytes"] == len(result.content.encode("utf-8"))
    assert result.metadata["original_selected_bytes"] == len(source)
    assert result.metadata["total_lines"] == 2
    assert result.metadata["truncated"] is False


def test_write_overwrite_preserves_mode_and_edit_failure_is_immutable(tmp_path) -> None:
    script = tmp_path / "script.sh"
    script.write_text("one\none\n", encoding="utf-8")
    script.chmod(0o755)
    registry = create_test_tool_registry(tmp_path)

    write = registry.execute(
        ToolCallBlock(
            id="call_overwrite",
            name="write_file",
            arguments={"path": "script.sh", "content": "one\none\n"},
        )
    )
    ambiguous = registry.execute(
        ToolCallBlock(
            id="call_ambiguous",
            name="edit_file",
            arguments={"path": "script.sh", "old_string": "one", "new_string": "two"},
        )
    )
    assert ambiguous.error_code == "EDIT_AMBIGUOUS"
    assert script.read_text(encoding="utf-8") == "one\none\n"
    global_edit = registry.execute(
        ToolCallBlock(
            id="call_global",
            name="edit_file",
            arguments={
                "path": "script.sh",
                "old_string": "one",
                "new_string": "two",
                "replace_all": True,
            },
        )
    )

    assert write.error_code is None
    assert os.stat(script).st_mode & 0o777 == 0o755
    assert script.read_text(encoding="utf-8") == "two\ntwo\n"
    assert global_edit.metadata["replacements"] == 2


def test_search_skips_default_ignored_directories_but_honors_explicit_path(tmp_path) -> None:
    ignored = tmp_path / "node_modules"
    ignored.mkdir()
    (ignored / "package.js").write_text("needle\n", encoding="utf-8")
    (tmp_path / "app.js").write_text("needle\n", encoding="utf-8")
    registry = create_test_tool_registry(tmp_path)

    default_search = registry.execute(
        ToolCallBlock(id="call_default", name="grep", arguments={"pattern": "needle"})
    )
    explicit_search = registry.execute(
        ToolCallBlock(
            id="call_explicit",
            name="grep",
            arguments={"path": "node_modules", "pattern": "needle"},
        )
    )
    invalid_regex = registry.execute(
        ToolCallBlock(id="call_regex", name="grep", arguments={"pattern": "["})
    )

    assert default_search.content == "app.js:1: needle"
    assert explicit_search.content == "node_modules/package.js:1: needle"
    assert invalid_regex.error_code == "INVALID_REGEX"


def test_workspace_escapes_unknown_tools_and_duplicate_registration_are_structured(tmp_path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "external.txt").symlink_to(outside)
    registry = create_test_tool_registry(tmp_path)

    escaped = registry.execute(
        ToolCallBlock(id="call_escape", name="read_file", arguments={"path": "../outside.txt"})
    )
    linked = registry.execute(
        ToolCallBlock(id="call_link", name="read_file", arguments={"path": "external.txt"})
    )
    unknown = registry.execute(
        ToolCallBlock(id="call_unknown", name="missing", arguments={})
    )
    duplicate = ToolRegistry()
    definition = ToolDefinition("tool", "test", {"type": "object"})
    duplicate.register(definition, lambda _: unknown)

    assert escaped.error_code == "WORKSPACE_ESCAPE"
    assert linked.error_code == "WORKSPACE_ESCAPE"
    assert unknown.error_code == "UNKNOWN_TOOL"
    try:
        duplicate.register(definition, lambda _: unknown)
    except ValueError as error:
        assert "already registered" in str(error)
    else:
        raise AssertionError("duplicate registration must fail")


def test_symlink_loops_are_reported_as_workspace_path_errors(tmp_path) -> None:
    (tmp_path / "loop").symlink_to("loop")
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(id="call_loop", name="read_file", arguments={"path": "loop"})
    )

    assert result.error_code == "IO_ERROR"


def test_file_tools_allow_hard_links_and_internal_symlinked_parent_components(
    tmp_path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("content", encoding="utf-8")
    hard_link = tmp_path / "hard.txt"
    os.link(source, hard_link)
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    (real_directory / "nested.txt").write_text("nested", encoding="utf-8")
    (tmp_path / "linked-parent").symlink_to(real_directory, target_is_directory=True)
    registry = create_test_tool_registry(tmp_path)

    hard_result = registry.execute(
        ToolCallBlock(id="hard", name="read_file", arguments={"path": "hard.txt"})
    )
    parent_result = registry.execute(
        ToolCallBlock(
            id="parent-link",
            name="read_file",
            arguments={"path": "linked-parent/nested.txt"},
        )
    )

    assert hard_result.error_code is None
    assert hard_result.content == "1: content"
    assert parent_result.error_code is None
    assert parent_result.content == "1: nested"


def test_write_file_preserves_internal_hard_link_aliases(tmp_path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("before", encoding="utf-8")
    alias = tmp_path / "alias.txt"
    os.link(source, alias)
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="write-hard-link",
            name="write_file",
            arguments={"path": "alias.txt", "content": "after"},
        )
    )

    assert result.error_code is None
    assert source.read_text(encoding="utf-8") == "after"
    assert alias.read_text(encoding="utf-8") == "after"
    assert source.stat().st_ino == alias.stat().st_ino


def test_tool_composition_canonicalizes_a_workspace_with_a_symlink_parent(tmp_path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    workspace = real_parent / "workspace"
    workspace.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    filesystem = WorkspaceFilesystem(linked_parent / "workspace")
    assert filesystem.root == workspace.resolve()


def test_filesystem_rejects_paths_with_embedded_nul_as_invalid_arguments(tmp_path) -> None:
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(id="nul", name="read_file", arguments={"path": "bad\x00path"})
    )

    assert result.error_code == "INVALID_ARGUMENTS"


def test_all_filesystem_definitions_are_closed_object_schemas(tmp_path) -> None:
    registry = create_test_tool_registry(tmp_path)
    definitions = registry.definitions()

    assert {definition.name for definition in definitions} == {
        "read_file",
        "write_file",
        "edit_file",
        "apply_patch",
        "glob",
        "grep",
        "run_command",
        "exec_command",
        "write_stdin",
    }
    assert all(definition.parameters["additionalProperties"] is False for definition in definitions)
    assert registry.lookup("read_file").name == "read_file"
    assert registry.lookup("missing") is None


def test_registry_uses_tool_schema_for_validation_and_defaults() -> None:
    registry = ToolRegistry()
    received: list[dict[str, object]] = []
    registry.register(
        ToolDefinition(
            name="sample",
            description="sample",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "limit": {"type": "integer", "default": 3},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        ),
        lambda arguments: received.append(arguments) or ToolResult("ok", {}),
    )

    valid = registry.execute(
        ToolCallBlock(id="valid", name="sample", arguments={"name": "value"})
    )
    invalid = registry.execute(
        ToolCallBlock(id="invalid", name="sample", arguments={"name": ""})
    )

    assert valid.error_code is None
    assert received == [{"name": "value", "limit": 3}]
    assert invalid.error_code == "INVALID_ARGUMENTS"


def test_tool_schema_validation_fails_closed_for_unsupported_types() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="unsupported",
            description="unsupported schema",
            parameters={
                "type": "object",
                "properties": {"items": {"type": "number"}},
                "required": [],
                "additionalProperties": False,
            },
        ),
        lambda _: ToolResult("must not execute", {}),
    )

    result = registry.execute(
        ToolCallBlock(id="unsupported", name="unsupported", arguments={})
    )

    assert result.error_code == "INVALID_ARGUMENTS"
    assert "unsupported schema type" in result.content


def test_glob_stops_after_detecting_the_first_truncated_match(
    tmp_path, monkeypatch
) -> None:
    registry = create_test_tool_registry(tmp_path)

    def files(*_args, **_kwargs):
        for index in range(201):
            yield tmp_path / f"{index:03}.py", f"{index:03}.py"
        raise AssertionError("glob scanned beyond the truncation sentinel")

    monkeypatch.setattr("agent.tools.filesystem.WorkspaceFilesystem.regular_files", files)

    result = registry.execute(
        ToolCallBlock(
            id="bounded_glob", name="glob", arguments={"pattern": "*.py"}
        )
    )

    assert result.error_code is None
    assert result.metadata == {"path": ".", "matches": 200, "truncated": True}


def test_grep_stops_after_detecting_the_first_truncated_match(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "matches.txt"
    source.write_text("needle\n" * 201, encoding="utf-8")
    registry = create_test_tool_registry(tmp_path)

    def files(*_args, **_kwargs):
        yield source, "matches.txt"
        raise AssertionError("grep scanned beyond the truncation sentinel")

    monkeypatch.setattr("agent.tools.filesystem.WorkspaceFilesystem.regular_files", files)

    result = registry.execute(
        ToolCallBlock(id="bounded_grep", name="grep", arguments={"pattern": "needle"})
    )

    assert result.error_code is None
    assert result.metadata == {"path": ".", "matches": 200, "truncated": True}


def test_grep_marks_results_incomplete_at_scanned_file_limit(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("agent.tools.local.MAX_SCANNED_FILES", 1, raising=False)
    (tmp_path / "a.txt").write_text("first\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("needle\n", encoding="utf-8")
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(id="file_limit", name="grep", arguments={"pattern": "needle"})
    )

    assert result.error_code is None
    assert result.content == ""
    assert result.metadata["truncated"] is True


def test_grep_skips_oversized_files_and_marks_results_incomplete(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("agent.tools.local.MAX_SEARCH_FILE_BYTES", 16, raising=False)
    (tmp_path / "large.txt").write_text("needle " * 3, encoding="utf-8")
    (tmp_path / "small.txt").write_text("needle\n", encoding="utf-8")
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(id="file_bytes", name="grep", arguments={"pattern": "needle"})
    )

    assert result.error_code is None
    assert result.content == "small.txt:1: needle"
    assert result.metadata["truncated"] is True


def test_grep_bounds_catastrophic_regex_matching_time(tmp_path) -> None:
    (tmp_path / "input.txt").write_text("a" * 1000 + "!\n", encoding="utf-8")
    registry = create_test_tool_registry(tmp_path)
    start = time.monotonic()

    result = registry.execute(
        ToolCallBlock(id="redos", name="grep", arguments={"pattern": "(a+)+$"})
    )

    assert result.error_code == "REGEX_TIMEOUT"
    assert time.monotonic() - start < 0.5


def test_invalid_parsed_arguments_become_recoverable_tool_error(tmp_path) -> None:
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_bad",
            name="read_file",
            arguments={},
            arguments_error="invalid JSON arguments",
        )
    )

    assert result.error_code == "INVALID_ARGUMENTS"
    assert result.metadata == {"tool": "read_file", "tool_call_id": "call_bad"}


def test_async_registry_execution_does_not_block_event_loop() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="slow",
            description="slow",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        ),
        lambda _: (time.sleep(0.1), ToolResult("done", {}))[1],
    )

    async def scenario() -> bool:
        execution = asyncio.create_task(
            registry.execute_async(ToolCallBlock(id="slow", name="slow", arguments={}))
        )
        await asyncio.sleep(0.02)
        responsive = not execution.done()
        await execution
        return responsive

    assert asyncio.run(scenario()) is True


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX-only")
def test_cancelling_sandbox_command_terminates_its_process_group(
    tmp_path, command_sandbox_backend
) -> None:
    registry = create_local_tool_registry(
        tmp_path,
        sandbox_backend=command_sandbox_backend,
    )
    marker = tmp_path / "marker.txt"

    async def scenario() -> None:
        execution = asyncio.create_task(
            registry.execute_async(
                ToolCallBlock(
                    id="cancel",
                    name="run_command",
                    arguments={
                        "command": (
                            "printf started > marker.txt; "
                            "(sleep 0.2; printf leaked > marker.txt) & sleep 1"
                        )
                    },
                )
            )
        )
        for _ in range(50):
            if marker.exists():
                break
            await asyncio.sleep(0.01)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
        await asyncio.sleep(0.4)

    asyncio.run(scenario())

    assert marker.read_text(encoding="utf-8") == "started"


def test_registry_logs_internal_tool_exceptions(caplog) -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="broken",
            description="broken",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        ),
        lambda _: (_ for _ in ()).throw(RuntimeError("developer details")),
    )

    with caplog.at_level(logging.ERROR, logger="agent.tools.registry"):
        result = registry.execute(
            ToolCallBlock(id="broken", name="broken", arguments={})
        )

    assert result.error_code == "INTERNAL_ERROR"
    assert result.content == "unexpected internal tool error"
    assert "developer details" in caplog.text


def test_path_schemas_reject_empty_paths_like_local_validation(tmp_path) -> None:
    registry = create_test_tool_registry(tmp_path)

    for name in ("read_file", "write_file", "edit_file"):
        assert registry.lookup(name).parameters["properties"]["path"]["minLength"] == 1
    for name, property_name in (("glob", "path"), ("grep", "path"), ("run_command", "cwd")):
        assert registry.lookup(name).parameters["properties"][property_name]["minLength"] == 1


def test_run_command_timeout_keeps_available_output_and_marks_tool_failure(tmp_path) -> None:
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_timeout",
            name="run_command",
            arguments={"command": "printf begun; sleep 1", "timeout_ms": 30},
        )
    )

    assert result.error_code == "TIMEOUT"
    assert result.metadata["timed_out"] is True
    assert result.metadata["stdout"] == "begun"


def test_run_command_marks_bounded_output_as_truncated(tmp_path) -> None:
    registry = create_test_tool_registry(tmp_path)

    result = registry.execute(
        ToolCallBlock(
            id="call_large_output",
            name="run_command",
            arguments={"command": "yes x | head -c 102401"},
        )
    )

    assert result.error_code is None
    assert result.metadata["stdout_truncated"] is True
    assert result.metadata["stderr_truncated"] is False
    assert len(result.metadata["stdout"].encode("utf-8")) <= 100 * 1024
