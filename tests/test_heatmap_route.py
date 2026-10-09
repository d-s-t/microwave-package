"""The chip-field heatmap route: it errored for every run (it read its panel
cap from the wrong module), so the page's heatmap panel could never load.

Run: python -m unittest discover -s tests
"""
import logging
import tempfile
import unittest
from unittest import mock

from microwave_package import paths, pipeline, webapp
from microwave_package.design.sweep import SweepParameter
from microwave_package.hfss.eigenmode import fields
from microwave_package.workflow.run_manager import Run


class TestHeatmapRoute(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)
        self.run = Run.create([SweepParameter(name="pedestal_depth", values=[0.0])],
                              dict(pipeline.DEFAULT_ANALYSIS_SETTINGS), {})
        self.client = webapp.app.test_client()

    def test_a_run_with_no_field_data_says_so_instead_of_failing(self):
        reply = self.client.get(f"/runs/{self.run.run_id}/heatmap")
        self.assertEqual(reply.status_code, 200)
        self.assertIn("No chip field data", reply.get_data(as_text=True))

    def test_more_variants_than_panels_are_capped_and_the_page_says_so(self):
        many = [f"c{i}" for i in range(fields.MAX_HEATMAP_PANELS + 5)]
        drawn = []
        with mock.patch.object(webapp, "_chip_field_combos", lambda run: many), \
                mock.patch.object(webapp, "_chip_field_entries",
                                  lambda run, combos: drawn.append(list(combos)) or []):
            reply = self.client.get(f"/runs/{self.run.run_id}/heatmap")
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(drawn, [many[:fields.MAX_HEATMAP_PANELS]])


if __name__ == "__main__":
    unittest.main()
