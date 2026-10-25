"""The two things BugNet needs from your system.

Both are deliberately tiny. BugNet's whole premise is that a system cannot
certify itself, so everything it trusts has to come from behind one of these
interfaces — implemented against something the system under watch does not
write.

If you find yourself implementing `Authority` by reading the same records
BugNet is checking, stop: that is the failure this library exists to catch.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Protocol, Tuple, runtime_checkable


@runtime_checkable
class Authority(Protocol):
    """The outside source of truth about what actually happened.

    An exchange's settlement feed, a payment processor's API, the warehouse a
    pipeline writes into, the filesystem a batch job was supposed to produce
    files on. Anything whose answer does not depend on the component being
    checked.
    """

    def resolve(self, unit_id: str) -> Optional[Any]:
        """The real outcome of one unit of work, or None if not known yet.

        Returning None means "not settled yet", not "nothing happened".
        """

    def settled_since(self, ts: float) -> Iterable[Tuple[str, Any, float]]:
        """Everything the authority has settled since `ts`.

        Yields `(unit_id, outcome, settled_at)`. This is what catches work the
        system never recorded at all — the units that are missing from your own
        books and therefore invisible to any check that reads them.
        """


@runtime_checkable
class Ledger(Protocol):
    """An externally-held aggregate to reconcile recorded outcomes against.

    An account balance, a row count, a total byte size, a invoice sum. BugNet
    snapshots it while the system is idle and checks that the change between
    snapshots is explained by the outcomes that were recorded in between.
    """

    def total(self) -> float:
        """The aggregate as the outside world currently reports it."""
