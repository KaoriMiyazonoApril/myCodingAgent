"""Compatibility facade for the split Context history policies.

Use :mod:`agent.runtime.history.units`, ``selection``, ``pruning`` or
``compaction`` when a focused dependency is preferable.  This module keeps
the original import path stable for existing Runtime integrations.
"""

from .history import *  # noqa: F401,F403
from .history import __all__
