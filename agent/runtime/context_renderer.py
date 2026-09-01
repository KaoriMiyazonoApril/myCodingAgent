"""Provider-independent rendering of a detached :class:`ContextPlan`."""

from __future__ import annotations

from copy import deepcopy

from agent.core.messages import Message, TextBlock

from .context_plan import ContextPlan
from .context_types import ContextSection


class ContextRenderer:
    """Render a ContextPlan into provider-independent model messages."""

    def render(self, plan: ContextPlan) -> list[Message]:
        def section_text(section: ContextSection) -> str:
            # TaskState/Skill projections already carry a self-describing
            # heading. Avoid rendering ``task_state: task_state:`` while
            # retaining headings for ordinary source sections.
            prefix = f"{section.name}:"
            return (
                section.content
                if section.content.startswith(prefix)
                else f"{prefix}\n{section.content}"
            )

        late_names = {section.name for section in plan.late_sections}
        baseline_sections = [
            section for section in plan.source_sections if section.name not in late_names
        ]
        stable = [section for section in baseline_sections if section.stable]
        dynamic = [section for section in baseline_sections if not section.stable]

        # Keep the base/project pair as the first block. The remaining epoch
        # sections retain their boundaries as an additional TextBlock.
        core_names = {"base_system_instructions", "project_instructions"}
        core = [section for section in stable if section.name in core_names]
        epoch_tail = [section for section in stable if section.name not in core_names]
        system_content: list[TextBlock] = []
        core_text = "\n\n".join(section.content for section in core if section.content)
        if core_text:
            system_content.append(TextBlock(text=core_text))
        tail_text = "\n\n".join(
            section_text(section)
            for section in (*epoch_tail, *dynamic)
            if section.content
        )
        if tail_text:
            separator = "\n\n" if system_content else ""
            system_content.append(TextBlock(text=separator + tail_text))
        messages = [Message(role="system", content=system_content)]
        history = [
            deepcopy(message)
            for message in plan.compacted_history
            if message.role != "system"
        ]
        messages.extend(history)
        if plan.current_input is not None and (
            not history or history[-1] != plan.current_input
        ):
            messages.append(deepcopy(plan.current_input))
        if plan.late_sections:
            late_text = "\n\n".join(
                section_text(section)
                for section in plan.late_sections
                if section.content
            )
            if late_text:
                messages.append(
                    Message(role="system", content=[TextBlock(text=late_text)])
                )
        return messages


__all__ = ["ContextRenderer"]
