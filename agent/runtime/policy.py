"""Tool authorization decisions independent of any frontend."""

from __future__ import annotations

from enum import Enum
from typing import Protocol

from agent.core.messages import ToolCallBlock


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ToolPolicy(Protocol):
    def decide(self, call: ToolCallBlock) -> PolicyDecision:
        """Return one explicit decision for a model-requested tool call."""


class AllowAllPolicy:
    """Initial policy: allow valid calls without claiming risk classification."""

    def decide(self, call: ToolCallBlock) -> PolicyDecision:
        return PolicyDecision.ALLOW
