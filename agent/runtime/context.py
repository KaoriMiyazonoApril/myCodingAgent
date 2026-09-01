"""Compatibility facade for the provider-independent Context API.

Context orchestration now lives in :mod:`agent.runtime.context_manager`;
instruction, runtime-environment, task-state, and shared value types each
have their own focused module.  Existing integrations importing from
``agent.runtime.context`` continue to receive the canonical classes.
"""

from .context_manager import *  # noqa: F401,F403
from .context_manager import __all__
