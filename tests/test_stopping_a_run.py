"""Tests for stopping a run from the web UI.

Stopping is cooperative -- the button sets an event and the pipeline acts
on it between combinations -- so what is testable here is the wiring:
the button is offered exactly while a run is going, pressing it asks the
run that is actually running to stop, and the page says so while the
programs finish what they are in the middle of.

Run: python -m unittest discover -s tests
"""
import logging
import tempfile
import threading
import unittest

from microwave_package import paths, webapp
from microwave_package.design.sweep import SweepParameter
from microwave_package.workflow.run_manager import Run


class StopControlTest(unittest.TestCase):
    def setUp(self):
        # The stop route says so in the log, which is the point of it but
        # not something the test output needs to carry.
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.client = webapp.app.test_client()
        # A real run folder, in a temporary runs root -- nothing here
        # should depend on what is in the developer's own runs/. Every
        # module resolves the runs root through `paths`, so pointing it
        # somewhere else is enough.
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)

        self.run = Run.create(
            [SweepParameter(name="pedestal_depth", values=[0.0, 1.0])],
            dict(webapp.pipeline.DEFAULT_ANALYSIS_SETTINGS),
        )
        self.addCleanup(webapp._active_threads.pop, self.run.run_id, None)
        self.addCleanup(webapp._stop_events.pop, self.run.run_id, None)

    def pretend_it_is_running(self) -> threading.Event:
        """A live thread and a stop event, which is all `_is_running` and
        the Stop button work on.

        The thread does NOT end when the stop event is set, deliberately:
        that gap -- asked to stop, still going -- is the state the page
        has to describe, and it is the real one. `finish()` is the
        program finally coming back.
        """
        self.alive = threading.Event()
        stop_event = threading.Event()
        thread = threading.Thread(target=self.alive.wait, daemon=True)
        thread.start()
        self.addCleanup(self.alive.set)
        webapp._active_threads[self.run.run_id] = thread
        webapp._stop_events[self.run.run_id] = stop_event
        return stop_event

    def finish(self) -> None:
        self.alive.set()
        webapp._active_threads[self.run.run_id].join(timeout=10)

    def page(self) -> str:
        response = self.client.get(f"/runs/{self.run.run_id}")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def status(self) -> dict:
        response = self.client.get(f"/runs/{self.run.run_id}/status.json")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    # -- the button ----------------------------------------------------
    def test_a_running_run_is_offered_a_stop(self):
        self.pretend_it_is_running()
        page = self.page()
        self.assertIn("Stop this run", page)
        self.assertIn(f"/runs/{self.run.run_id}/stop", page)

    def test_a_run_that_is_not_going_is_offered_a_resume_instead(self):
        page = self.page()
        self.assertIn("Resume this run", page)
        # Both forms are rendered; the one that does not apply is hidden,
        # so the poll can swap them without a reload.
        self.assertIn('id="stop-form"', page)

    # -- pressing it ---------------------------------------------------
    def test_pressing_stop_asks_the_running_pipeline_to_stop(self):
        stop_event = self.pretend_it_is_running()
        self.assertFalse(stop_event.is_set())
        response = self.client.post(f"/runs/{self.run.run_id}/stop")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(stop_event.is_set())

    def test_stopping_a_run_that_is_not_running_does_nothing(self):
        response = self.client.post(f"/runs/{self.run.run_id}/stop")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(webapp._is_stopping(self.run.run_id))

    def test_stopping_an_unknown_run_is_not_an_error(self):
        response = self.client.post("/runs/run_00000000_000000/stop")
        self.assertEqual(response.status_code, 302)

    # -- while it is stopping ------------------------------------------
    def test_the_page_says_it_is_stopping_until_the_thread_ends(self):
        """There is always a gap: no program can be interrupted mid-call,
        so the run stops as each of them next comes back."""
        self.pretend_it_is_running()
        self.client.post(f"/runs/{self.run.run_id}/stop")
        self.assertTrue(webapp._is_stopping(self.run.run_id))
        self.assertTrue(self.status()["is_stopping"])
        self.assertIn("stopping", self.page())

    def test_a_finished_stop_is_no_longer_stopping(self):
        self.pretend_it_is_running()
        self.client.post(f"/runs/{self.run.run_id}/stop")
        self.finish()
        self.assertFalse(webapp._is_stopping(self.run.run_id))
        self.assertFalse(self.status()["is_running"])


if __name__ == "__main__":
    unittest.main()
