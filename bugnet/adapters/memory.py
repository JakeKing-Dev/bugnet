"""In-memory Authority and Ledger.

Enough to try BugNet in two minutes, to write tests against, and to serve as a
worked example of how small a real adapter needs to be.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


class InMemoryAuthority:
    """An outside source of truth you control, for examples and tests."""

    def __init__(self, now_fn: Optional[Callable[[], float]] = None) -> None:
        self._now = now_fn or time.time
        self._settled: Dict[str, Tuple[Any, float]] = {}

    def settle(self, unit_id: str, outcome: Any, at: Optional[float] = None) -> None:
        """The outside world has decided what happened to this unit."""
        self._settled[unit_id] = (outcome, self._now() if at is None else at)

    # --- Authority -------------------------------------------------------
    def resolve(self, unit_id: str) -> Optional[Any]:
        row = self._settled.get(unit_id)
        return None if row is None else row[0]

    def settled_since(self, ts: float) -> Iterable[Tuple[str, Any, float]]:
        rows: List[Tuple[str, Any, float]] = [
            (uid, outcome, at) for uid, (outcome, at) in self._settled.items() if at >= ts
        ]
        rows.sort(key=lambda r: r[2])
        return rows


class InMemoryLedger:
    """An externally-held aggregate you control, for examples and tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._total = float(start)

    def move(self, delta: float) -> None:
        """The outside world's total changed by this much."""
        self._total += delta

    def set(self, value: float) -> None:
        self._total = float(value)

    # --- Ledger ----------------------------------------------------------
    def total(self) -> float:
        return self._total
