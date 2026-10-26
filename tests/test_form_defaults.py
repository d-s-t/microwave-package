"""The new-run form opens on the defaults; a run's settings load only on request.

Before this, the form carried over the latest run's settings, and an unticked
box is absent from a POST, so the old run's yes/no settings leaked into the
next run (issue #98).

Run: python -m unittest discover -s tests
"""
import logging
import re
import tempfile
import unittest
from unittest import mock

from microwave_package import paths, webapp


def _box(page, name):
    return re.search(rf'<input type="checkbox" name="{name}"[^>]*>', page, re.S).group(0)


class TestFormDefaults(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)
        self.client = webapp.app.test_client()

    def start(self, **extra):
        pedestal = "0" if "no_chip" in extra else "0.5"
        data = {"range_pedestal_depth": pedestal, **extra}
        with mock.patch.object(webapp, "_start_pipeline_thread") as started:
            self.client.post("/runs/new", data=data)
        return started.call_args[0][0]

    def test_a_previous_run_does_not_change_the_defaults_the_form_opens_on(self):
        # the previous run: bare board, corner screws off
        self.start(no_chip="1")
        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("checked", _box(page, "no_chip"))
        self.assertIn("checked", _box(page, "screws_on"))
        self.assertIn("Showing the defaults.", page)

    def test_loading_a_run_shows_its_values_and_says_where_they_came_from(self):
        run = self.start(no_chip="1")
        page = self.client.get(f"/?from_run={run.run_id}").get_data(as_text=True)
        self.assertIn("checked", _box(page, "no_chip"))
        self.assertIn(f"Loaded the settings from {run.run_id}.", page)
        self.assertIn('href="/"', page)  # the way back to the defaults

    def test_a_post_with_unticked_boxes_pins_the_defaults(self):
        self.start(no_chip="1", screws_on="1")  # an earlier run, both on
        run = self.start()                      # every box unticked
        self.assertEqual(run.parameter_defaults["no_chip"], 0.0)
        self.assertEqual(run.parameter_defaults["screws_on"], 0.0)
        self.assertFalse(run.analysis_settings["graphical"])


if __name__ == "__main__":
    unittest.main()
