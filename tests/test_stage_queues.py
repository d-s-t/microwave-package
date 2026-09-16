"""Tests for the three stage queues that drain at the same time.

The scheduling is what is tested, not the programs: none of Altium,
SolidWorks or AEDT is started here. What has to be right is that two
lanes really do work at once, that a combination only reaches the third
when BOTH of its halves have landed, that a combination whose half
failed is dropped rather than half-built, and that a program that cannot
be used at all takes the run down on the caller's thread instead of
being retried once per combination.

Run: python -m unittest discover -s tests
"""
import logging
import threading
import unittest

from microwave_package.workflow import stages


class FakeRun:
    """Just enough of Run for the per-combination bookkeeping."""

    def __init__(self, *combo_ids, **fields):
        self.state = {"combos": {cid: dict(fields) for cid in combo_ids}}
        self._lock = threading.Lock()

    def update_combo(self, combo_id, **fields):
        with self._lock:
            self.state["combos"][combo_id].update(fields)


def drain(lane, combo_ids):
    """Run one lane over `combo_ids` and wait for it to finish."""
    lane.start()
    for combo_id in combo_ids:
        lane.submit(combo_id)
    lane.seal()
    lane.thread.join(timeout=10)


class TestOneLane(unittest.TestCase):
    def setUp(self):
        # A lane logs the traceback of a program that could not be used,
        # which is the point of it -- but not something the test output
        # needs to carry.
        logging.disable(logging.ERROR)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_combinations_are_worked_in_the_order_they_were_given(self):
        """Each program drives one shared document, so its own queue is
        strictly sequential however many lanes there are."""
        seen = []
        lane = stages._Lane("test", lambda combo_id: seen.append(combo_id) or True)
        drain(lane, ["c1", "c2", "c3"])
        self.assertEqual(seen, ["c1", "c2", "c3"])

    def test_it_closes_when_it_runs_out_of_work(self):
        closed = threading.Event()
        lane = stages._Lane("test", lambda _c: True, close=closed.set)
        drain(lane, ["c1"])
        self.assertTrue(closed.is_set())

    def test_a_program_that_cannot_be_used_stops_the_lane(self):
        """An exception is the program failing, not a combination: it is
        kept for the coordinator rather than repeated per combination."""
        tried = []

        def work(combo_id):
            tried.append(combo_id)
            raise RuntimeError("SolidWorks is not installed")

        lane = stages._Lane("test", work)
        drain(lane, ["c1", "c2", "c3"])
        self.assertEqual(tried, ["c1"])
        self.assertIsInstance(lane.error, RuntimeError)

    def test_it_still_closes_after_a_program_fails(self):
        closed = threading.Event()
        lane = stages._Lane("test", lambda _c: 1 / 0, close=closed.set)
        drain(lane, ["c1"])
        self.assertTrue(closed.is_set())

    def test_a_stop_is_acted_on_between_combinations(self):
        """No program can be interrupted mid-call, so the one in flight
        finishes and nothing after it is started."""
        stop_event = threading.Event()
        seen = []

        def work(combo_id):
            seen.append(combo_id)
            stop_event.set()
            return True

        lane = stages._Lane("test", work, stop_event=stop_event)
        drain(lane, ["c1", "c2", "c3"])
        self.assertEqual(seen, ["c1"])
        self.assertIsNone(lane.error)


class TestComOnALanesThread(unittest.TestCase):
    """Windows requires COM to be initialised on every thread that uses
    it, and a lane is its own thread -- two of the three programs are
    driven over COM from one. Without that the first SolidWorks call
    fails with "CoInitialize has not been called", which reads like
    SolidWorks not being installed rather than like a threading mistake.
    """

    def test_a_lane_can_drive_com(self):
        try:
            import win32com.client
        except ImportError:
            self.skipTest("pywin32 is Windows-only; nothing drives COM without it")

        outcome = []

        def work(_combo_id):
            try:
                # Any COM object will do -- this one ships with Windows.
                # What is being tested is that COM answers at all here.
                win32com.client.Dispatch("Scripting.Dictionary")
                outcome.append(None)
            except Exception as exc:
                outcome.append(exc)
            return True

        drain(stages._Lane("test", work), ["c1"])
        self.assertEqual(outcome, [None], f"COM was not usable on the lane's thread: {outcome}")


class TestTwoLanesAtOnce(unittest.TestCase):
    def test_they_really_do_run_at_the_same_time(self):
        """The whole point. Both lanes are made to wait for the other to
        have started, which can only happen if they overlap -- a
        sequential arrangement would deadlock and time out here.
        """
        both_started = threading.Barrier(2, timeout=10)

        def work(_combo_id):
            both_started.wait()
            return True

        first = stages._Lane("first", work)
        second = stages._Lane("second", work)
        for lane in (first, second):
            lane.start()
            lane.submit("c1")
            lane.seal()
        for lane in (first, second):
            lane.thread.join(timeout=10)
            self.assertFalse(lane.thread.is_alive())
            self.assertIsNone(lane.error)


class TestTheGate(unittest.TestCase):
    """A model needs both the copper and the solid, and the two arrive in
    whatever order their programs finish them."""

    def setUp(self):
        self.released = []
        self.gate = stages._Gate(2, self.released.append)

    def test_one_half_is_not_enough(self):
        self.gate.report("c1", True)
        self.assertEqual(self.released, [])

    def test_both_halves_release_it_once(self):
        self.gate.report("c1", True)
        self.gate.report("c1", True)
        self.assertEqual(self.released, ["c1"])

    def test_the_order_the_halves_land_in_does_not_matter(self):
        self.gate.report("c1", True)
        self.gate.report("c2", True)
        self.gate.report("c2", True)
        self.gate.report("c1", True)
        self.assertEqual(self.released, ["c2", "c1"])

    def test_a_failed_half_drops_the_combination(self):
        """Nothing to build a model from -- and the stage that failed has
        already recorded why."""
        self.gate.report("c1", False)
        self.gate.report("c1", True)
        self.assertEqual(self.released, [])

    def test_one_dependency_releases_immediately(self):
        """A run with Altium off has only SolidWorks in front of AEDT."""
        released = []
        gate = stages._Gate(1, released.append)
        gate.report("c1", True)
        self.assertEqual(released, ["c1"])


class TestClearingStaleProgress(unittest.TestCase):
    """A run killed mid-combination left that combination marked as being
    worked on. Nothing is working on it now."""

    def test_in_flight_markers_are_cleared(self):
        run = FakeRun("c1", altium_status="running", step_status="running",
                      model_status="building", post_status="running")
        stages.clear_stale_progress(run)
        for field in stages.BUSY_FIELDS:
            self.assertIsNone(run.state["combos"]["c1"][field], field)

    def test_finished_work_is_left_alone(self):
        run = FakeRun("c1", altium_status="done", step_status="done",
                      model_status="built", post_status="failed")
        stages.clear_stale_progress(run)
        combo = run.state["combos"]["c1"]
        self.assertEqual(combo["altium_status"], "done")
        self.assertEqual(combo["step_status"], "done")
        self.assertEqual(combo["model_status"], "built")
        self.assertEqual(combo["post_status"], "failed")


class TestTheLanesTogether(unittest.TestCase):
    """Two upstream lanes feeding a third through the gate -- the shape
    `run_local_stages` builds, without any of the programs."""

    def build(self, layout_fails=()):
        self.modelled = []
        self.abort = threading.Event()
        aedt = stages._Lane("AEDT", lambda c: self.modelled.append(c) or True,
                            abort=self.abort)
        gate = stages._Gate(2, aedt.submit)
        upstream = [
            stages._Lane("Altium", lambda c: c not in layout_fails,
                         on_finished=gate.report, abort=self.abort),
            stages._Lane("SolidWorks", lambda _c: True,
                         on_finished=gate.report, abort=self.abort),
        ]
        return upstream, aedt

    def run_all(self, upstream, aedt, combo_ids):
        for lane in (*upstream, aedt):
            lane.start()
        for combo_id in combo_ids:
            for lane in upstream:
                lane.submit(combo_id)
        for lane in upstream:
            lane.seal()
            lane.thread.join(timeout=10)
        aedt.seal()
        aedt.thread.join(timeout=10)

    def test_every_combination_reaches_the_model_stage(self):
        upstream, aedt = self.build()
        self.run_all(upstream, aedt, ["c1", "c2", "c3"])
        self.assertEqual(sorted(self.modelled), ["c1", "c2", "c3"])

    def test_a_combination_whose_layout_failed_is_never_modelled(self):
        upstream, aedt = self.build(layout_fails={"c2"})
        self.run_all(upstream, aedt, ["c1", "c2", "c3"])
        self.assertEqual(sorted(self.modelled), ["c1", "c3"])


class TestSealingAnIdleLane(unittest.TestCase):
    def test_a_lane_given_nothing_finishes(self):
        """It waits on its queue, so something has to tell it no more is
        coming -- otherwise a run with nothing to do never ends."""
        lane = stages._Lane("test", lambda _c: True).start()
        lane.seal()
        lane.thread.join(timeout=10)
        self.assertFalse(lane.thread.is_alive())

    def test_sealing_is_safe_to_repeat(self):
        lane = stages._Lane("test", lambda _c: True).start()
        lane.seal()
        lane.seal()
        lane.thread.join(timeout=10)
        self.assertFalse(lane.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
