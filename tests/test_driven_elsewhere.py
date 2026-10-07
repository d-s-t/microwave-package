"""Tests for a run that another process is driving.

The web app knew only the runs its own threads were driving. A run
resumed from the command line looked idle there, and its Resume button
would have had two processes driving one run, both waiting on the same
tasks and reading the same results back. So the process driving a run
says so in the run's folder, and everything else believes it only while
that process is alive.

Run: python -m unittest discover -s tests
"""
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import unittest

import psutil

from microwave_package import paths, webapp
from microwave_package.design.sweep import SweepParameter
from microwave_package.workflow.run_manager import Run


class RunFolderTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)
        self.run = Run.create(
            [SweepParameter(name="pedestal_depth", values=[0.0, 1.0])],
            dict(webapp.pipeline.DEFAULT_ANALYSIS_SETTINGS),
        )

    def other_process(self) -> psutil.Process:
        """Another live process on this machine, standing in for the
        command line driving the run."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return psutil.Process(child.pid)

    def claimed_by(self, process: psutil.Process, **overrides) -> None:
        claim = {"pid": process.pid, "started": process.create_time(),
                 "host": socket.gethostname(), **overrides}
        with open(self.run.driver_path, "w") as f:
            json.dump(claim, f)


class WhoIsDrivingTest(RunFolderTest):
    def test_a_run_nobody_has_claimed_is_not_driven(self):
        self.assertIsNone(self.run.driven_elsewhere())

    def test_this_process_is_not_elsewhere(self):
        self.run.claim_driver()
        self.assertIsNone(self.run.driven_elsewhere())

    def test_another_live_process_is(self):
        other = self.other_process()
        self.claimed_by(other)
        self.assertEqual(self.run.driven_elsewhere(), other.pid)

    def test_a_process_that_has_ended_is_not(self):
        """A process killed outright cannot take its claim back."""
        other = self.other_process()
        self.claimed_by(other)
        other.kill()
        other.wait()
        self.assertIsNone(self.run.driven_elsewhere())

    def test_a_later_process_given_the_same_pid_is_not(self):
        other = self.other_process()
        self.claimed_by(other, started=other.create_time() - 3600)
        self.assertIsNone(self.run.driven_elsewhere())

    def test_a_claim_from_another_machine_is_not_counted(self):
        other = self.other_process()
        self.claimed_by(other, host="some-other-machine")
        self.assertIsNone(self.run.driven_elsewhere())

    def test_an_unreadable_claim_is_not_counted(self):
        with open(self.run.driver_path, "w") as f:
            f.write("{not json")
        self.assertIsNone(self.run.driven_elsewhere())

    def test_releasing_takes_back_our_own_claim(self):
        self.run.claim_driver()
        self.run.release_driver()
        self.assertFalse(os.path.exists(self.run.driver_path))

    def test_releasing_leaves_another_process_claim_alone(self):
        other = self.other_process()
        self.claimed_by(other)
        self.run.release_driver()
        self.assertEqual(self.run.driven_elsewhere(), other.pid)


class WebAppTest(RunFolderTest):
    def setUp(self):
        super().setUp()
        self.client = webapp.app.test_client()
        self.other = self.other_process()
        self.claimed_by(self.other)
        self.addCleanup(webapp._active_threads.pop, self.run.run_id, None)

    def test_the_status_feed_says_it_is_running_elsewhere(self):
        data = self.client.get(f"/runs/{self.run.run_id}/status.json").get_json()
        self.assertTrue(data["is_running"])
        self.assertEqual(data["driven_elsewhere"], self.other.pid)

    def test_the_page_offers_neither_resume_nor_stop(self):
        page = self.client.get(f"/runs/{self.run.run_id}").get_data(as_text=True)
        self.assertIn("running elsewhere", page)
        self.assertRegex(page, r'id="resume-form"\s+hidden')
        self.assertRegex(page, r'id="stop-form"\s+hidden')
        self.assertNotRegex(page, r'id="elsewhere-note"\s+hidden')

    def test_resume_is_refused_and_starts_nothing(self):
        response = self.client.post(f"/runs/{self.run.run_id}/resume")
        self.assertEqual(response.status_code, 302)
        self.assertIn("another+process", response.headers["Location"])
        self.assertNotIn(self.run.run_id, webapp._active_threads)

    def test_delete_is_refused(self):
        self.client.post(f"/runs/{self.run.run_id}/delete",
                         data={"confirm_run_id": self.run.run_id})
        self.assertTrue(os.path.isdir(self.run.run_dir))

    def test_once_that_process_ends_it_is_idle_again(self):
        self.other.kill()
        self.other.wait()
        data = self.client.get(f"/runs/{self.run.run_id}/status.json").get_json()
        self.assertFalse(data["is_running"])
        self.assertIsNone(data["driven_elsewhere"])


if __name__ == "__main__":
    unittest.main()
