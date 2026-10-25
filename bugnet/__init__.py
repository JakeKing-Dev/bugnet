"""BugNet — an audit of a system's own account of itself.

Monitoring tells you a system is healthy. BugNet tells you whether its records
are true: it compares them against a source the system does not write, and
stops the component at fault when they disagree.

    from bugnet import BugNet, AUTHORITY, FALLBACK
    from bugnet.adapters.memory import InMemoryAuthority

    authority = InMemoryAuthority()
    net = BugNet(authority=authority)

    net.opened("job-1", "importer", closes_at=time.time())
    ...
    net.resolved("job-1", outcome="ok", source=AUTHORITY, value=1.0)
    net.step()

    if net.blocks("importer"):
        ...                      # do not start new work
"""

from .core import AUTHORITY, FALLBACK, BugNet, Config, Fault, Level, Unit
from .protocols import Authority, Ledger

__all__ = [
    "BugNet",
    "Config",
    "Fault",
    "Level",
    "Unit",
    "Authority",
    "Ledger",
    "AUTHORITY",
    "FALLBACK",
]

__version__ = "0.1.0"
