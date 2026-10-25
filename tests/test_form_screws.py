"""The new-run form's yes/no boxes: bare board and corner screws.

The equation file builds the screws by default now, so a form whose
"Corner screws" box is unticked has to say so -- left silent it gave a
run with screws that was labelled as one without -- and the box has to
open ticked when nothing says otherwise, as the equation file would build.

Run: python -m unittest discover -s tests
"""
import logging
import re
import tempfile
import unittest
from unittest import mock

from microwave_package import paths, webapp
from microwave_package.workflow.run_manager import Run


class TestFormBooleans(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)
        self.client = webapp.app.test_client()

    def start(self, **extra):
        # A bare board posted without a pedestal gets 0 (flat floor); a typed one would win.
        pedestal = "0" if "no_chip" in extra else "0.5"
        data = {"range_pedestal_depth": pedestal, **extra}
        with mock.patch.object(webapp, "_start_pipeline_thread") as started:
            self.client.post("/runs/new", data=data)
        return started.call_args[0][0]

    def test_an_unticked_screws_box_pins_them_off(self):
        run = self.start()
        self.assertEqual(run.parameter_defaults["screws_on"], 0.0)

    def test_a_ticked_one_pins_them_on(self):
        run = self.start(screws_on="1")
        self.assertEqual(run.parameter_defaults["screws_on"], 1.0)

    def test_a_ticked_bare_board_is_pinned_and_an_unticked_one_is_left_to_the_file(self):
        self.assertEqual(self.start(no_chip="1").parameter_defaults["no_chip"], 1.0)
        self.assertNotIn("no_chip", self.start().parameter_defaults)

    def test_a_bare_board_with_a_typed_recess_is_started_with_it(self):
        # the recess sits under the bare board's solid copper: it is accepted, not refused
        data ={"range_pedestal_depth": "0.5", "no_chip": "1"}
        with mock.patch.object(webapp, "_start_pipeline_thread") as started:
            self.client.post("/runs/new", data=data)
        run = started.call_args[0][0]
        self.assertEqual(run.parameter_defaults["pedestal_depth"], 0.5)
        self.assertEqual(run.parameter_defaults["no_chip"], 1.0)

    def test_a_fresh_form_opens_with_the_screws_ticked_as_the_equation_file_has_them(self):
        page = self.client.get("/").get_data(as_text=True)
        box = re.search(r'<input type="checkbox" name="screws_on"[^>]*>', page, re.S).group(0)
        self.assertIn("checked", box)

    def test_the_form_opens_on_what_the_run_it_starts_from_chose(self):
        run = self.start()                                   # screws off, no bare board
        page = self.client.get(f"/?from_run={run.run_id}").get_data(as_text=True)
        box = re.search(r'<input type="checkbox" name="screws_on"[^>]*>', page, re.S).group(0)
        self.assertNotIn("checked", box)
        bare = self.start(no_chip="1", screws_on="1")
        page = self.client.get(f"/?from_run={bare.run_id}").get_data(as_text=True)
        self.assertIn("checked", re.search(r'<input type="checkbox" name="no_chip"[^>]*>', page, re.S).group(0))
        self.assertIn("checked", re.search(r'<input type="checkbox" name="screws_on"[^>]*>', page, re.S).group(0))


if __name__ == "__main__":
    unittest.main()
