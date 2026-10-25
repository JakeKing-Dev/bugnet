"""Injected-fault benchmark.

Claims about a fault detector should be measured, not asserted. This harness
runs a simulated system, injects known faults at known rates, and reports what
BugNet caught and how long it took.

An honest note on what these numbers mean: BugNet's checks are deterministic,
not statistical. Given a fault larger than the configured threshold, detection
is certain — the interesting questions are *how long* it takes, *how small* a
fault it can see, and *how often it is wrong on a clean system*. Those are the
numbers below. A detector that reports a probabilistic detection rate for a
deterministic check is measuring its own randomness, not its sensitivity.

    python bench/benchmark.py            # table to stdout
    python bench/benchmark.py --check    # non-zero exit if a guarantee regressed
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass
from typing import List, Optional

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from bugnet import AUTHORITY, FALLBACK, BugNet, Config, Level     # noqa: E402
from bugnet.adapters.memory import InMemoryAuthority, InMemoryLedger  # noqa: E402

TICK = 30.0          # simulated seconds per loop iteration
UNIT_LIFETIME = 300.0


@dataclass
class Result:
    scenario: str
    injected: int
    detected: bool
    seconds_to_detect: Optional[float]
    stops: int


class Sim:
    """A system that does units of work, with faults injected on demand."""

    def __init__(self, cfg: Optional[Config] = None, seed: int = 1,
                 with_ledger: bool = False) -> None:
        self.t = 100_000.0
        self.rng = random.Random(seed)
        self.authority = InMemoryAuthority(now_fn=lambda: self.t)
        self.ledger = InMemoryLedger(start=1000.0) if with_ledger else None
        self.net = BugNet(authority=self.authority, ledger=self.ledger,
                          config=cfg or Config(), now_fn=lambda: self.t)
        self.n = 0
        self.first_fault_at: Optional[float] = None
        self.detected_at: Optional[float] = None
        # instance-level on purpose: a class-level mutable default leaks state
        # between runs and makes every result quietly order-dependent
        self._pending: List = []

    def tick(self) -> None:
        self.t += TICK
        self.net.step(self.t)
        if self.detected_at is None and self.stops():
            self.detected_at = self.t

    def do_unit(self, *, drop_outcome: bool = False, hide_from_system: bool = False,
                fallback: bool = False, value: float = 1.0) -> str:
        """One unit of work, with optional injected faults."""
        self.n += 1
        uid = f"unit-{self.n}"
        settle_at = self.t + UNIT_LIFETIME

        if hide_from_system:
            # the authority knows about work this system never recorded at all
            self.authority.settle(uid, "done", at=settle_at)
            if self.ledger:
                self.ledger.move(value)
            self._mark_fault()
            return uid

        self.net.opened(uid, "worker", closes_at=settle_at, value=value)
        self.authority.settle(uid, "done", at=settle_at)
        if drop_outcome:
            # the work happened and settled; the system never wrote it down
            if self.ledger:
                self.ledger.move(value)
            self._mark_fault()
            return uid

        self._pending.append((settle_at, uid, value, fallback))
        return uid

    def settle_due(self) -> None:
        still: List = []
        for settle_at, uid, value, fallback in self._pending:
            if self.t >= settle_at:
                self.net.resolved(uid, "done",
                                  source=FALLBACK if fallback else AUTHORITY, value=value)
                if self.ledger:
                    self.ledger.move(value)
            else:
                still.append((settle_at, uid, value, fallback))
        self._pending = still

    def _mark_fault(self) -> None:
        if self.first_fault_at is None:
            self.first_fault_at = self.t

    def stops(self) -> int:
        return sum(1 for f in self.net.faults.values() if f.level is Level.STOP)

    def run_until_stop(self, ticks: int) -> Optional[float]:
        for _ in range(ticks):
            self.settle_due()
            self.tick()
            if self.detected_at is not None:
                return self.detected_at
        return self.detected_at

    def lag(self) -> Optional[float]:
        """Simulated seconds from the first injected fault to the first STOP."""
        if self.detected_at is None or self.first_fault_at is None:
            return None
        return self.detected_at - self.first_fault_at


def scenario_dropped_outcomes(rate: float, total: int = 200, seed: int = 1) -> Result:
    """Work completes and settles, but the system never records the outcome."""
    sim = Sim(seed=seed)
    injected = 0
    for _ in range(total):
        drop = sim.rng.random() < rate
        injected += drop
        sim.do_unit(drop_outcome=drop)
        sim.settle_due()
        sim.tick()
    sim.run_until_stop(ticks=200)
    return Result(f"dropped outcomes @ {rate:.0%}", injected,
                  sim.detected_at is not None, sim.lag(), sim.stops())


def scenario_unrecorded_work(count: int = 1, seed: int = 2) -> Result:
    """The authority settled work the system has no record of whatsoever."""
    sim = Sim(seed=seed)
    for _ in range(50):
        sim.do_unit()
        sim.settle_due()
        sim.tick()
    for _ in range(count):
        sim.do_unit(hide_from_system=True)
    sim.run_until_stop(ticks=400)
    return Result(f"unrecorded work x{count}", count,
                  sim.detected_at is not None, sim.lag(), sim.stops())


def scenario_ledger_drift(amount: float, start: float = 1000.0, seed: int = 3) -> Result:
    """Money or rows move with nothing in the records to explain it."""
    cfg = Config(recon_min_interval=600.0)
    sim = Sim(cfg=cfg, seed=seed, with_ledger=True)
    sim.ledger.set(start)
    for _ in range(40):
        # value=0 so the ledger stays at `start`: this scenario measures the
        # tolerance, and a drifting total would move the threshold underneath it
        sim.do_unit(value=0.0)
        sim.settle_due()
        sim.tick()
    for _ in range(40):          # drain, so the system is idle
        sim.settle_due()
        sim.tick()
    sim.ledger.move(amount)      # unexplained movement
    sim._mark_fault()
    sim.run_until_stop(ticks=200)
    return Result(f"ledger drift {amount:+.2f} on {start:,.0f}", 1,
                  sim.detected_at is not None, sim.lag(), sim.stops())


def smallest_detected_drift(start: float, seed: int = 4) -> float:
    """The smallest unexplained movement BugNet will see on a ledger this size.

    This is the honest limit of the reconciliation check: tolerance is
    max(recon_tolerance_abs, recon_tolerance_frac x total), so the blind spot
    grows with the ledger. Measuring it beats pretending it isn't there.
    """
    lo, hi = 0.0, max(1.0, start)
    for _ in range(24):                       # bisect to a tight bound
        mid = (lo + hi) / 2
        if scenario_ledger_drift(mid, start=start, seed=seed).detected:
            hi = mid
        else:
            lo = mid
    return hi


def scenario_clean(seed: int, with_ledger: bool = True) -> Result:
    """No faults at all. Anything BugNet reports here is a false positive."""
    sim = Sim(seed=seed, with_ledger=with_ledger)
    for _ in range(300):
        sim.do_unit(fallback=sim.rng.random() < 0.05)   # a little fallback is normal
        sim.settle_due()
        sim.tick()
    for _ in range(200):
        sim.settle_due()
        sim.tick()
    return Result(f"clean run (seed {seed})", 0, sim.stops() > 0, None, sim.stops())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if a guarantee regressed")
    args = ap.parse_args()

    detection = [
        scenario_dropped_outcomes(0.01),
        scenario_dropped_outcomes(0.05),
        scenario_dropped_outcomes(0.20),
        scenario_unrecorded_work(1),
        scenario_unrecorded_work(5),
        scenario_ledger_drift(+25.00),
        scenario_ledger_drift(-25.00),
        scenario_ledger_drift(+0.50, start=10.0),
    ]
    clean = [scenario_clean(seed) for seed in range(1, 11)]

    print("## Detection\n")
    print("| scenario | faults injected | detected | time to detect |")
    print("|---|---:|:---:|---:|")
    for r in detection:
        when = "-" if r.seconds_to_detect is None else f"{r.seconds_to_detect / 60:.0f} min"
        print(f"| {r.scenario} | {r.injected} | {'yes' if r.detected else 'NO'} | {when} |")

    print("\n## Reconciliation sensitivity\n")
    print("Tolerance is max(0.15, 2% of total), so the blind spot grows with the ledger.\n")
    print("| ledger total | smallest drift detected |")
    print("|---:|---:|")
    for start in (10.0, 100.0, 1000.0, 10000.0):
        print(f"| {start:,.0f} | {smallest_detected_drift(start):,.2f} |")

    false_positives = sum(1 for r in clean if r.stops > 0)
    print("\n## False positives\n")
    print(f"{len(clean)} clean runs, including 5% fallback resolutions and a live ledger: "
          f"**{false_positives} STOP(s) raised**.\n")

    missed = [r for r in detection if not r.detected]
    if missed:
        print("MISSED:", ", ".join(r.scenario for r in missed))

    if args.check:
        if missed:
            print("FAIL: a fault went undetected", file=sys.stderr)
            return 1
        if false_positives:
            print("FAIL: a clean run raised a STOP", file=sys.stderr)
            return 1
        print("\nOK: every injected fault detected, no false positives.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
