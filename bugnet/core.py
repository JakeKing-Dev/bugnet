"""BugNet: an audit of a system's own account of itself.

Monitoring asks whether a system is healthy. BugNet asks whether its records
are true, by comparing them against a source the system does not write, and
stopping the component at fault when they are not.

The five rules it is built on, learned the expensive way:

1. Check against an outside truth. A check that compares a system's numbers
   with the same system's numbers passes while both are wrong.
2. A warning nobody acts on is not a control. A STOP blocks the component at
   fault. Winding down is always still permitted.
3. Fallbacks quietly become the main path. Count how often an outcome came
   from the fallback resolver instead of the authority.
4. Errors that are caught and hidden are faults too.
5. Every unit of work must end in a recorded outcome. A record that drops
   events is a record that lies.

Stdlib only, and deterministic: the same inputs give the same faults every
time. `step()` never raises, and reports its own failures.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

from .protocols import Authority, Ledger

log = logging.getLogger("bugnet")

__all__ = ["BugNet", "Config", "Fault", "Level", "Unit", "AUTHORITY", "FALLBACK"]

AUTHORITY = "authority"
FALLBACK = "fallback"


class Level(str, Enum):
    SQUEAK = "SQUEAK"   # logged repeatedly while it lasts; nothing is blocked
    STOP = "STOP"       # the component at fault is blocked until it clears


@dataclass
class Unit:
    """One unit of work that must end in a recorded outcome."""
    id: str
    component: str
    opened_at: float
    closes_at: Optional[float] = None    # when an outcome becomes due
    resolved_at: Optional[float] = None
    outcome: Any = None
    source: Optional[str] = None         # AUTHORITY, FALLBACK, or your own label
    value: float = 0.0                   # amount this unit contributed, for reconciliation
    reconciled: bool = False             # already counted in a reconciliation window
    written_off: bool = False            # acknowledged as never going to resolve

    @property
    def resolved(self) -> bool:
        return self.resolved_at is not None

    def due_since(self, now: float) -> float:
        """Seconds since an outcome was due. Negative means not due yet."""
        return now - (self.closes_at if self.closes_at is not None else self.opened_at)


@dataclass
class Fault:
    id: str
    level: Level
    component: str
    message: str
    first_seen: float
    last_seen: float
    live: bool          # live faults clear themselves; past-event faults need an ack
    count: int = 1


@dataclass
class Config:
    # completeness
    resolve_grace: float = 300.0        # squeak once an outcome is this overdue
    resolve_hard: float = 1800.0        # stop once it is this overdue
    # authority coverage
    authority_lag: float = 900.0        # ignore settlements younger than this
    authority_window: float = 86400.0   # how far back to ask on each pass
    # fallback share
    fallback_min_sample: int = 20
    fallback_max_share: float = 0.2
    # hidden errors
    error_window: float = 600.0
    error_stop_count: int = 3
    # reconciliation
    recon_min_interval: float = 3000.0  # minimum gap between snapshots
    recon_tolerance_abs: float = 0.15
    recon_tolerance_frac: float = 0.02
    # housekeeping
    ack_path: str = "BUGNET_OK"
    retain_resolved: float = 86400.0    # drop resolved units older than this
    max_seen: int = 20000
    # cadence
    every_fast: float = 60.0
    every_authority: float = 600.0
    every_recon: float = 120.0


class BugNet:
    """Watch a system's account of its own work.

    Tell it when work opens and when an outcome is recorded; give it an
    `Authority` and optionally a `Ledger`; call `step()` from your main loop and
    `blocks(component)` before doing anything consequential.
    """

    def __init__(
        self,
        authority: Optional[Authority] = None,
        ledger: Optional[Ledger] = None,
        config: Optional[Config] = None,
        now_fn: Optional[Callable[[], float]] = None,
        idle_fn: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.authority = authority
        self.ledger = ledger
        self.cfg = config or Config()
        self._now: Callable[[], float] = now_fn or time.time
        # Reconciliation is only meaningful while nothing is in flight. By
        # default "idle" means no unresolved units; override if your system has
        # a better notion of quiet.
        self._idle: Callable[[], bool] = idle_fn or self._default_idle

        self.units: Dict[str, Unit] = {}
        self.faults: Dict[str, Fault] = {}
        self.errors: Deque[Tuple[float, str, str]] = deque(maxlen=500)
        self.seen: Deque[str] = deque(maxlen=self.cfg.max_seen)
        self._seen_set: set = set()

        self.started = self._now()
        self.authority_since = self.started
        self.snapshot: Optional[Tuple[float, float]] = None   # (ts, total)
        self.recon_passes = 0
        self.own_errors = 0
        self.checks = 0

        self._resolutions: Dict[str, Dict[str, int]] = {}     # component -> source -> count
        self._said: Dict[str, float] = {}
        self._next: Dict[str, float] = {"fast": 0.0, "authority": 0.0, "recon": 0.0}

    # ---------------------------------------------------------------- recording

    def opened(self, unit_id: str, component: str, *, closes_at: Optional[float] = None,
               value: float = 0.0) -> Unit:
        """Record that a unit of work has started and owes an outcome."""
        u = Unit(id=unit_id, component=component, opened_at=self._now(),
                 closes_at=closes_at, value=value)
        self.units[unit_id] = u
        self._remember(unit_id)
        return u

    def resolved(self, unit_id: str, outcome: Any, *, source: str = AUTHORITY,
                 value: Optional[float] = None) -> None:
        """Record the outcome of a unit, and where the outcome came from.

        `source` matters: pass FALLBACK when the outcome was inferred rather
        than obtained from the authority. A fallback that becomes the main path
        is a fault, and you cannot see that happening without this.
        """
        u = self.units.get(unit_id)
        if u is None:
            u = self.opened(unit_id, component="unknown")
        u.resolved_at = self._now()
        u.outcome = outcome
        u.source = source
        if value is not None:
            u.value = value
        self._resolutions.setdefault(u.component, {}).setdefault(source, 0)
        self._resolutions[u.component][source] += 1

    def note_error(self, component: str, exc: BaseException) -> None:
        """An error the system caught and kept quiet about."""
        self.errors.append((self._now(), component, f"{type(exc).__name__}: {exc}"[:200]))

    def _remember(self, unit_id: str) -> None:
        if unit_id in self._seen_set:
            return
        if len(self.seen) == self.seen.maxlen:
            self._seen_set.discard(self.seen[0])
        self.seen.append(unit_id)
        self._seen_set.add(unit_id)

    def _default_idle(self) -> bool:
        return not any(not u.resolved for u in self.units.values())

    # ------------------------------------------------------------------ faults

    def raise_(self, fault_id: str, level: Level, component: str, message: str,
               *, live: bool = True) -> None:
        now = self._now()
        f = self.faults.get(fault_id)
        if f is None:
            f = Fault(fault_id, level, component, message, now, now, live)
            self.faults[fault_id] = f
            self._announce(fault_id, f, first=True)
        else:
            if level is Level.STOP and f.level is not Level.STOP:
                f.level = Level.STOP
                self._announce(fault_id, f, first=True)
            f.message, f.last_seen = message, now
            f.count += 1

    def clear(self, fault_id: str, why: str = "the check passes again") -> None:
        f = self.faults.pop(fault_id, None)
        if f is not None:
            log.warning("BUGNET CLEAR [%s]: %s (%s)", f.component, f.message, why)

    def _announce(self, fault_id: str, f: Fault, first: bool = False) -> None:
        now = self._now()
        every = 300.0 if f.level is Level.STOP else 600.0
        if not first and now - self._said.get(fault_id, -1e18) < every:
            return
        self._said[fault_id] = now
        what = f"{f.component} blocked" if f.level is Level.STOP else "watching"
        tail = "" if f.live else f"; acknowledge with: touch {self.cfg.ack_path}"
        log.warning("BUGNET %s [%s]: %s (%s%s)", f.level.value, f.component, f.message,
                    what, tail)

    def blocks(self, component: str) -> Optional[str]:
        """Why this component must not start new work, or None.

        Winding down existing work is always allowed — a component frozen
        holding resources is usually worse than one stopping in an orderly way.
        """
        for f in self.faults.values():
            if f.level is Level.STOP and f.component in (component, "all"):
                return f"BugNet: {f.message}"
        return None

    def acknowledge(self) -> int:
        """Clear faults about things that already happened. Returns how many.

        Acknowledging also writes off work that was never going to resolve.
        Without that, the same fault is re-raised on the next check and the
        acknowledgement means nothing — which is how alert buttons become
        decorative.
        """
        now = self._now()
        gone = [fid for fid, f in self.faults.items() if not f.live]
        for fid in gone:
            self.clear(fid, "acknowledged")
        for u in self.units.values():
            if not u.resolved and u.due_since(now) >= self.cfg.resolve_hard:
                u.written_off = True
        self.snapshot = None     # reconciliation starts fresh after a human looked
        return len(gone)

    # ------------------------------------------------------------------- checks

    def check_completeness(self, now: float) -> None:
        """Rule 5: every unit of work must end in a recorded outcome."""
        waiting: List[Unit] = []
        lost: List[Unit] = []
        for u in self.units.values():
            if u.resolved or u.written_off:
                continue
            overdue = u.due_since(now)
            if overdue >= self.cfg.resolve_hard:
                lost.append(u)
            elif overdue >= self.cfg.resolve_grace:
                waiting.append(u)

        by_component: Dict[str, List[Unit]] = {}
        for u in lost:
            by_component.setdefault(u.component, []).append(u)
        for component, us in by_component.items():
            self.raise_(f"unresolved:{component}", Level.STOP, component,
                        f"{len(us)} unit(s) of work passed their deadline with no recorded "
                        f"outcome (e.g. {us[0].id}); they are missing from the records, not "
                        "merely late", live=False)

        if waiting:
            self.raise_("waiting", Level.SQUEAK, waiting[0].component,
                        f"{len(waiting)} unit(s) overdue for an outcome "
                        f"(e.g. {waiting[0].id})")
        else:
            self.clear("waiting")

    def check_authority(self, now: float) -> None:
        """Rule 1: work the authority knows about that we never recorded.

        This is the check that sees what is missing from your own books, which
        no check that reads your books can do.
        """
        if self.authority is None:
            return
        try:
            rows = list(self.authority.settled_since(
                max(self.authority_since, now - self.cfg.authority_window)))
        except Exception as e:          # an authority that errors is not a fault in us
            self.note_error("bugnet.authority", e)
            return

        unaccounted: List[Tuple[str, Any]] = []
        newest = self.authority_since
        for unit_id, outcome, settled_at in rows:
            if settled_at < self.started or now - settled_at < self.cfg.authority_lag:
                continue                 # too old to be ours, or too fresh to judge
            newest = max(newest, settled_at)
            if unit_id not in self._seen_set:
                unaccounted.append((unit_id, outcome))
        self.authority_since = max(self.authority_since, newest - self.cfg.authority_lag)

        if unaccounted:
            unit_id, outcome = unaccounted[0]
            self.raise_(f"unaccounted:{int(now)}", Level.STOP, "all",
                        f"the authority settled {len(unaccounted)} unit(s) of work that "
                        f"nothing in this system recorded, e.g. {unit_id} (outcome "
                        f"{outcome!r})", live=False)

    def check_reconciliation(self, now: float) -> None:
        """Rule 1 again, in aggregate: does the outside total match our records?"""
        if self.ledger is None:
            return
        try:
            total = float(self.ledger.total())
        except Exception as e:
            self.note_error("bugnet.ledger", e)
            return
        if not self._idle():
            return                       # mid-flight work would look like a gap
        if self.snapshot is None:
            # anything already resolved belongs to the period before we started
            # watching, so it must not be counted against the first window
            for u in self.units.values():
                if u.resolved:
                    u.reconciled = True
            self.snapshot = (now, total)
            return
        since, before = self.snapshot
        if now - since < self.cfg.recon_min_interval:
            return

        # Counting by flag rather than by timestamp range: a unit resolved
        # exactly on a window boundary would otherwise be counted twice or not
        # at all, and "not at all" is the bug this library exists to catch.
        fresh = [u for u in self.units.values()
                 if u.resolved and not u.reconciled and (u.resolved_at or 0) <= now]
        recorded = sum(u.value for u in fresh)
        for u in fresh:
            u.reconciled = True
        moved = total - before
        gap = moved - recorded
        tolerance = max(self.cfg.recon_tolerance_abs,
                        abs(total) * self.cfg.recon_tolerance_frac)
        if abs(gap) > tolerance:
            self.raise_(f"reconciliation:{int(now)}", Level.STOP, "all",
                        f"the books are off by {gap:+.2f}: the outside total moved "
                        f"{moved:+.2f} over {(now - since) / 3600:.1f}h, recorded outcomes "
                        f"explain {recorded:+.2f}", live=False)
        else:
            self.recon_passes += 1
        self.snapshot = (now, total)

    def check_fallback(self, now: float) -> None:
        """Rule 3: a fallback that became the main path."""
        for component, by_source in self._resolutions.items():
            total = sum(by_source.values())
            fallback = by_source.get(FALLBACK, 0)
            fid = f"fallback:{component}"
            if total >= self.cfg.fallback_min_sample and \
                    fallback > self.cfg.fallback_max_share * total:
                self.raise_(fid, Level.SQUEAK, component,
                            f"{fallback} of {total} outcomes in {component} came from the "
                            "fallback, not the authority")
            else:
                self.clear(fid)

    def check_errors(self, now: float) -> None:
        """Rule 4: errors that were caught and kept quiet."""
        recent = [e for e in self.errors if now - e[0] <= self.cfg.error_window]
        by_component: Dict[str, List[Tuple[float, str, str]]] = {}
        for e in recent:
            by_component.setdefault(e[1], []).append(e)
        for component, es in by_component.items():
            level = Level.STOP if len(es) >= self.cfg.error_stop_count else Level.SQUEAK
            self.raise_(f"errors:{component}", level, component,
                        f"{len(es)} hidden error(s) in {component} within "
                        f"{int(self.cfg.error_window)}s; last: {es[-1][2]}")
        for fid in [k for k in self.faults if k.startswith("errors:")]:
            if fid.split(":", 1)[1] not in by_component:
                self.clear(fid, "no new errors in the window")

    # -------------------------------------------------------------------- loop

    def step(self, now: Optional[float] = None) -> None:
        """Run the checks that are due. Never raises."""
        now = self._now() if now is None else now
        try:
            self._check_ack()
            if now >= self._next["fast"]:
                self._next["fast"] = now + self.cfg.every_fast
                self.checks += 1
                self.check_completeness(now)
                self.check_fallback(now)
                self.check_errors(now)
                self._prune(now)
            if now >= self._next["authority"]:
                self._next["authority"] = now + self.cfg.every_authority
                self.check_authority(now)
            if now >= self._next["recon"]:
                self._next["recon"] = now + self.cfg.every_recon
                self.check_reconciliation(now)
            for fid, f in list(self.faults.items()):
                self._announce(fid, f)
        except Exception as e:           # BugNet must not take the system down...
            self.own_errors += 1         # ...nor go quiet about its own failure
            if now - self._said.get("__own__", -1e18) >= 600:
                self._said["__own__"] = now
                log.warning("BUGNET SQUEAK [bugnet]: BugNet's own check failed (%s); the "
                            "other checks carry on", e)

    def _check_ack(self) -> None:
        path = self.cfg.ack_path
        try:
            if not os.path.exists(path):
                return
            os.remove(path)
        except OSError:
            return
        n = self.acknowledge()
        log.warning("BUGNET: acknowledgement seen; %d past-event fault(s) cleared", n)

    def _prune(self, now: float) -> None:
        cutoff = now - self.cfg.retain_resolved
        for uid in [u.id for u in self.units.values()
                    if u.resolved and (u.resolved_at or 0) < cutoff]:
            self.units.pop(uid, None)

    # ----------------------------------------------------------------- reporting

    def status(self) -> str:
        if not self.faults:
            return f"bugnet: all clear ({self.checks} checks)"
        stops = [f for f in self.faults.values() if f.level is Level.STOP]
        worst = (stops or list(self.faults.values()))[0]
        return (f"bugnet: {len(stops)} STOP, {len(self.faults) - len(stops)} squeak; "
                f"{worst.component}: {worst.message[:90]}")

    def report(self) -> List[str]:
        out = ["BugNet:"]
        if not self.faults:
            out.append("  all clear")
        for f in sorted(self.faults.values(), key=lambda x: x.first_seen):
            when = time.strftime("%m-%d %H:%M", time.localtime(f.first_seen))
            out.append(f"  {f.level.value} [{f.component}] since {when}: {f.message}")
        unresolved = sum(1 for u in self.units.values() if not u.resolved)
        out.append(f"  units tracked: {len(self.units)} ({unresolved} unresolved)")
        for component, by_source in sorted(self._resolutions.items()):
            parts = ", ".join(f"{k} {v}" for k, v in sorted(by_source.items()))
            out.append(f"  {component} outcomes from: {parts}")
        out.append(f"  reconciliations passed: {self.recon_passes}")
        return out
