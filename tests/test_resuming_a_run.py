"""Tests for when a run can be resumed from its page.

A run whose copy back failed -- the laptop's standby drops the network
in the middle of one -- used to count as complete, and a complete run's
page offers no Resume button, although Resume is exactly what copies the
files back. The same held for every failure but the worker's own.

Run: python -m unittest discover -s tests
"""
import logging
import tempfile
import unittest

from microwave_package import paths, webapp
from microwave_package.design.sweep import SweepParameter
from microwave_package.workflow.run_manager import Run

# Everything up to and including a finished analysis on the worker.
ANALYSED = {
    "altium_status": "done", "step_status": "done", "model_status": "built",
    "handoff_status": "done", "task_id": "t1", "queue_status": "done",
}


class ResumeTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)
        self.run = Run.create([SweepParameter(name="pedestal_depth", values=[0.0, 1.0])],
                              dict(webapp.pipeline.DEFAULT_ANALYSIS_SETTINGS))
        self.first, self.second = list(self.run.state["combos"])
        self.run.update_combo(self.first, **ANALYSED, fetch_status="done", post_status="done")

    def second_is(self, **fields):
        self.run.update_combo(self.second, **ANALYSED, **fields)


class TestIsComplete(ResumeTest):
    def test_a_failed_copy_back_is_not_the_end(self):
        self.second_is(fetch_status="failed", post_status="failed",
                       post_error="could not copy the analysed files off the queue")
        self.assertFalse(self.run.is_complete)

    def test_nor_are_results_that_failed_after_the_copy(self):
        self.second_is(fetch_status="done", post_status="failed", post_error="AEDT said no")
        self.assertFalse(self.run.is_complete)

    def test_nor_a_model_that_failed(self):
        """Resume rebuilds any model that is not built."""
        self.run.update_combo(self.second, altium_status="done", step_status="done",
                              model_status="failed")
        self.assertFalse(self.run.is_complete)

    def test_an_analysis_the_worker_failed_is(self):
        """The one failure Resume does not retry: the task stays failed
        on the queue."""
        self.run.update_combo(self.second, **dict(ANALYSED, queue_status="failed"))
        self.assertTrue(self.run.is_complete)

    def test_every_result_in_is_complete(self):
        self.second_is(fetch_status="done", post_status="done")
        self.assertTrue(self.run.is_complete)


class TestTheRunPageOffersResume(ResumeTest):
    def page(self):
        response = webapp.app.test_client().get(f"/runs/{self.run.run_id}")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_after_a_failed_copy_back(self):
        self.second_is(fetch_status="failed", post_status="failed")
        self.assertIn('id="resume-form"', self.page())

    def test_not_once_every_result_is_in(self):
        self.second_is(fetch_status="done", post_status="done")
        self.assertNotIn('id="resume-form"', self.page())


if __name__ == "__main__":
    unittest.main()
