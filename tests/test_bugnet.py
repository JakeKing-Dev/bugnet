"""Tests for BugNet.

One test per claim the README makes. Where a check has a threshold, there is a
test just below it and a test just above it, because a detector that fires on
everything is as useless as one that fires on nothing.
"""

import os
import tempfile
import unittest

from bugnet import AUTHORITY, FALLBACK, BugNet, Config, Level
from bugnet.adapters.memory import InMemoryAuthority, InMemoryLedger


class Clock:
    def __init__(self, t=100_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt
        return self.t


def net(clock, **kw):
    cfg = kw.pop("config", Config())
    return BugNet(now_fn=clock, config=cfg, **kw)


# --------------------------------------------------------------- completeness
class TestCompleteness(unittest.TestCase):
    """Rule 5: every unit of work must end in a recorded outcome."""

    def test_a_resolved_unit_raises_nothing(self):
        c = Clock()
        bn = net(c)
        bn.opened("u1", "worker", closes_at=c.t)
        bn.resolved("u1", "done")
        bn.step(c.advance(5000))
        self.assertEqual(bn.faults, {})
        self.assertIsNone(bn.blocks("worker"))

    def test_an_overdue_unit_squeaks_before_it_stops(self):
        c = Clock()
        bn = net(c)
        bn.opened("u1", "worker", closes_at=c.t)
        bn.step(c.advance(400))                 # past grace, before hard
        self.assertIn("waiting", bn.faults)
        self.assertIs(bn.faults["waiting"].level, Level.SQUEAK)
        self.assertIsNone(bn.blocks("worker"))  # a squeak never blocks

    def test_a_unit_that_never_resolves_stops_its_component(self):
        c = Clock()
        bn = net(c)
        bn.opened("u1", "worker", closes_at=c.t)
        bn.step(c.advance(2000))
        self.assertTrue(bn.blocks("worker"))
        self.assertIn("missing from the records", bn.blocks("worker"))

    def test_one_component_stopping_does_not_block_another(self):
        c = Clock()
        bn = net(c)
        bn.opened("u1", "worker", closes_at=c.t)
        bn.step(c.advance(2000))
        self.assertTrue(bn.blocks("worker"))
        self.assertIsNone(bn.blocks("other"))

    def test_a_late_outcome_still_counts_and_the_squeak_clears(self):
        c = Clock()
        bn = net(c)
        bn.opened("u1", "worker", closes_at=c.t)
        bn.step(c.advance(400))
        self.assertIn("waiting", bn.faults)
        bn.resolved("u1", "done")
        bn.step(c.advance(100))
        self.assertNotIn("waiting", bn.faults)


# ------------------------------------------------------------------ authority
class TestAuthority(unittest.TestCase):
    """Rule 1: the check that sees what is missing from your own books."""

    def test_work_the_system_never_recorded_is_caught(self):
        c = Clock()
        auth = InMemoryAuthority(now_fn=c)
        bn = net(c, authority=auth)
        auth.settle("ghost", "done", at=c.t + 10)
        bn.step(c.advance(2000))
        self.assertTrue(bn.blocks("anything"))
        self.assertIn("nothing in this system recorded", bn.blocks("anything"))

    def test_work_the_system_did_record_is_not_flagged(self):
        c = Clock()
        auth = InMemoryAuthority(now_fn=c)
        bn = net(c, authority=auth)
        bn.opened("u1", "worker", closes_at=c.t + 10)
        auth.settle("u1", "done", at=c.t + 10)
        bn.resolved("u1", "done")
        bn.step(c.advance(2000))
        self.assertEqual(bn.faults, {})

    def test_very_recent_settlements_are_not_judged_yet(self):
        """Records are written moments after settlement; do not cry too early."""
        c = Clock()
        auth = InMemoryAuthority(now_fn=c)
        bn = net(c, authority=auth)
        c.advance(100)
        auth.settle("fresh", "done", at=c.t)
        bn.step(c.advance(60))
        self.assertEqual(bn.faults, {})

    def test_an_authority_that_errors_is_not_treated_as_our_fault(self):
        class Broken:
            def resolve(self, unit_id): return None
            def settled_since(self, ts): raise RuntimeError("upstream down")

        c = Clock()
        bn = net(c, authority=Broken())
        bn.step(c.advance(2000))
        self.assertEqual([f for f in bn.faults.values() if f.level is Level.STOP], [])
        self.assertTrue(bn.errors)


# ------------------------------------------------------------- reconciliation
class TestReconciliation(unittest.TestCase):
    def _run(self, drift, start=10.0):
        c = Clock()
        led = InMemoryLedger(start=start)
        bn = net(c, ledger=led, config=Config(recon_min_interval=600.0))
        bn.step(c.advance(60))              # first snapshot
        led.move(drift)
        bn.step(c.advance(1200))
        return bn

    def test_an_explained_move_passes(self):
        c = Clock()
        led = InMemoryLedger(start=10.0)
        bn = net(c, ledger=led, config=Config(recon_min_interval=600.0))
        bn.step(c.advance(60))
        bn.opened("u1", "worker", closes_at=c.t, value=5.0)
        bn.resolved("u1", "done", value=5.0)
        led.move(5.0)
        bn.step(c.advance(1200))
        self.assertEqual(bn.faults, {})
        self.assertEqual(bn.recon_passes, 1)

    def test_an_unexplained_move_stops_everything(self):
        bn = self._run(drift=5.0)
        self.assertTrue(bn.blocks("worker"))
        self.assertIn("the books are off by", bn.blocks("worker"))

    def test_a_move_inside_tolerance_is_ignored(self):
        bn = self._run(drift=0.05)          # under max(0.15, 2% of 10)
        self.assertEqual(bn.faults, {})

    def test_reconciliation_waits_while_work_is_in_flight(self):
        """A unit mid-flight looks exactly like a gap; do not guess."""
        c = Clock()
        led = InMemoryLedger(start=10.0)
        bn = net(c, ledger=led)
        bn.opened("u1", "worker", closes_at=c.t + 10_000)
        bn.step(c.advance(300))
        self.assertIsNone(bn.snapshot)


# ------------------------------------------------------------------- fallback
class TestFallback(unittest.TestCase):
    """Rule 3: a fallback that quietly became the main path."""

    def test_heavy_fallback_use_squeaks(self):
        c = Clock()
        bn = net(c)
        for i in range(20):
            bn.opened(f"u{i}", "worker", closes_at=c.t)
            bn.resolved(f"u{i}", "done", source=FALLBACK if i < 10 else AUTHORITY)
        bn.step(c.advance(100))
        self.assertIn("fallback:worker", bn.faults)
        self.assertIs(bn.faults["fallback:worker"].level, Level.SQUEAK)
        self.assertIsNone(bn.blocks("worker"))

    def test_occasional_fallback_use_is_fine(self):
        c = Clock()
        bn = net(c)
        for i in range(40):
            bn.opened(f"u{i}", "worker", closes_at=c.t)
            bn.resolved(f"u{i}", "done", source=FALLBACK if i < 2 else AUTHORITY)
        bn.step(c.advance(100))
        self.assertNotIn("fallback:worker", bn.faults)

    def test_a_small_sample_is_not_enough_to_judge(self):
        c = Clock()
        bn = net(c)
        for i in range(4):
            bn.opened(f"u{i}", "worker", closes_at=c.t)
            bn.resolved(f"u{i}", "done", source=FALLBACK)
        bn.step(c.advance(100))
        self.assertNotIn("fallback:worker", bn.faults)


# --------------------------------------------------------------------- errors
class TestHiddenErrors(unittest.TestCase):
    """Rule 4: errors that were caught and kept quiet are faults too."""

    def test_repeated_hidden_errors_stop_the_component(self):
        c = Clock()
        bn = net(c)
        for _ in range(3):
            bn.note_error("worker", KeyError("x"))
        bn.step(c.advance(60))
        self.assertTrue(bn.blocks("worker"))

    def test_a_single_hidden_error_only_squeaks(self):
        c = Clock()
        bn = net(c)
        bn.note_error("worker", ValueError("x"))
        bn.step(c.advance(60))
        self.assertIn("errors:worker", bn.faults)
        self.assertIsNone(bn.blocks("worker"))

    def test_the_block_lifts_once_the_errors_stop(self):
        c = Clock()
        bn = net(c)
        for _ in range(3):
            bn.note_error("worker", KeyError("x"))
        bn.step(c.advance(60))
        self.assertTrue(bn.blocks("worker"))
        bn.step(c.advance(1200))            # quiet for longer than the window
        self.assertIsNone(bn.blocks("worker"))


# ------------------------------------------------------- acknowledgement, self
class TestAcknowledgement(unittest.TestCase):
    def test_faults_about_past_events_need_a_human(self):
        c = Clock()
        bn = net(c)
        bn.opened("u1", "worker", closes_at=c.t)
        bn.step(c.advance(2000))
        self.assertTrue(bn.blocks("worker"))
        bn.resolved("u1", "done")           # fixing it afterwards is not enough
        bn.step(c.advance(100))
        self.assertTrue(bn.blocks("worker"))
        self.assertEqual(bn.acknowledge(), 1)
        self.assertIsNone(bn.blocks("worker"))

    def test_the_ack_file_clears_past_faults_and_is_consumed(self):
        c = Clock()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "BUGNET_OK")
            bn = net(c, config=Config(ack_path=path))
            bn.opened("u1", "worker", closes_at=c.t)
            bn.step(c.advance(2000))
            self.assertTrue(bn.blocks("worker"))
            open(path, "w").close()
            bn.step(c.advance(60))
            self.assertIsNone(bn.blocks("worker"))
            self.assertFalse(os.path.exists(path))


class TestSelfFailure(unittest.TestCase):
    def test_bugnet_never_takes_the_system_down_and_says_when_it_breaks(self):
        c = Clock()
        bn = net(c)
        bn.check_completeness = lambda now: 1 / 0
        bn.step(c.advance(60))              # must not raise
        self.assertEqual(bn.own_errors, 1)
        self.assertTrue(bn.status().startswith("bugnet:"))

    def test_report_and_status_work_when_all_is_well(self):
        c = Clock()
        bn = net(c)
        bn.opened("u1", "worker", closes_at=c.t)
        bn.resolved("u1", "done", source=AUTHORITY)
        bn.step(c.advance(60))
        self.assertIn("all clear", bn.status())
        self.assertTrue(any("authority 1" in line for line in bn.report()))


class TestHousekeeping(unittest.TestCase):
    def test_resolved_units_are_eventually_dropped(self):
        c = Clock()
        bn = net(c, config=Config(retain_resolved=100.0))
        bn.opened("u1", "worker", closes_at=c.t)
        bn.resolved("u1", "done")
        bn.step(c.advance(5000))
        self.assertNotIn("u1", bn.units)

    def test_a_dropped_unit_is_still_remembered_as_accounted_for(self):
        """Pruning must not resurrect old work as 'never recorded'."""
        c = Clock()
        auth = InMemoryAuthority(now_fn=c)
        bn = net(c, authority=auth, config=Config(retain_resolved=100.0))
        bn.opened("u1", "worker", closes_at=c.t)
        auth.settle("u1", "done", at=c.t)
        bn.resolved("u1", "done")
        bn.step(c.advance(5000))
        self.assertNotIn("u1", bn.units)
        bn.step(c.advance(600))
        self.assertEqual([f for f in bn.faults.values() if f.level is Level.STOP], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
