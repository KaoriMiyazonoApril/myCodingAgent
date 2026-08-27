"""Deterministic command-sandbox adapter for tests at the public seam."""

from __future__ import annotations

from pathlib import Path

from agent.tools.local import create_local_tool_registry
from agent.tools.process import CommandSandboxBackend
from agent.tools.registry import ToolRegistry


class DeterministicSandboxBackend(CommandSandboxBackend):
    """Run commands locally while exercising the production lifecycle contract."""

    def __init__(self) -> None:
        self.checked_workspaces: list[Path] = []

    def check_available(self, workspace_root: Path) -> None:
        self.checked_workspaces.append(workspace_root)

    def _build_command(
        self,
        *,
        workspace_root: Path,
        command: str,
        relative_cwd: str,
    ) -> list[str]:
        return ["/usr/bin/bash", "--noprofile", "--norc", "-c", command]


def create_test_tool_registry(workspace_root: Path) -> ToolRegistry:
    """Compose local tools without requiring host bubblewrap support."""

    return create_local_tool_registry(
        workspace_root,
        sandbox_backend=DeterministicSandboxBackend(),
    )
