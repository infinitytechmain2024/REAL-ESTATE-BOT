"""Policy-enforced Agent Reach adapter boundary.

This package intentionally does not install or invoke Agent Reach's upstream
CLI.  That CLI can execute local tools and manage browser instances, which is
outside this service's read-only safety boundary.
"""

from .runner import ControlledAgentReach

__all__ = ["ControlledAgentReach"]
