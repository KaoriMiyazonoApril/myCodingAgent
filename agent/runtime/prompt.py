"""Provider-independent system prompt composition for coding-agent Turns."""

from __future__ import annotations


DEFAULT_SYSTEM_PROMPT = """You are a local coding agent.
- Use only workspace-relative paths with tools.
- Inspect relevant files before modifying them.
- Prefer file tools for source changes.
- Run suitable validation before completion.
- Handle tool errors honestly and never report an action that did not succeed.
- In the final answer, summarize the work and validation."""


class PromptBuilder:
    """Combine stable agent rules with optional Runtime instructions."""

    def build(self, additional_system_instructions: str | None = None) -> str:
        if not additional_system_instructions:
            return DEFAULT_SYSTEM_PROMPT
        return f"{DEFAULT_SYSTEM_PROMPT}\n\n{additional_system_instructions}"
