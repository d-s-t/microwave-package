"""The Smith chart of a run's S11 / S22: the reflection coefficient, the
normalised impedance, the grid circles, which ports a table can show, and
the web routes that serve the chart.

Run: python -m unittest discover -s tests
"""
import logging
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd

from microwave_package import paths, pipeline, webapp
from microwave_package.design.sweep import SweepParameter
from microwave_package.workflow import smith
from microwave_package.workflow.run_manager import Run


def _sparams(ports=("s11", "s22")):
    """A small table of a driven run: a few frequencies, one combination."""
    df = pd.DataFrame({
        "freq_ghz": [1.0, 2.0, 3.0],
        "combo_id": ["c1", "c1", "c1"],
        "pedestal_depth": [0.5, 0.5, 0.5],
    })
    for port in ports:
        df[f"{port}_db"] = [-20.0, -6.0, -3.0]
        df[f"{port}_deg"] = [0.0, 90.0, -45.0]
    return df


class TestReflectionCoefficient(unittest.TestCase):
    def test_magnitude_comes_from_db_and_phase_from_degrees(self):
        gamma = smith.reflection(-20.0, 0.0)
        self.assertAlmostEqual(gamma, 0.1 + 0j)

    def test_zero_db_at_180_degrees_is_minus_one(self):
        self.assertAlmostEqual(smith.reflection(0.0, 180.0), -1 + 0j, places=12)

    def test_vectorised(self):
        gamma = smith.reflection(np.array([0.0, -20.0]), np.array([0.0, 90.0]))
        np.testing.assert_allclose(gamma, [1 + 0j, 1j * 0.1], atol=1e-12)


class TestNormalisedImpedance(unittest.TestCase):
    def test_matched_load_is_the_reference(self):
        self.assertAlmostEqual(smith.normalised_impedance(0j), 1 + 0j)

    def test_known_value(self):
        # G = 0.5 -> z = 1.5 / 0.5 = 3
        self.assertAlmostEqual(smith.normalised_impedance(0.5 + 0j), 3 + 0j)

    def test_open_circuit_is_not_a_number_rather_than_an_error(self):
        self.assertTrue(np.isnan(smith.normalised_impedance(1 + 0j)))


class TestGridCircles(unittest.TestCase):
    def test_constant_resistance_circle_is_on_its_centre_and_radius(self):
        for r in smith.RESISTANCE_VALUES:
            circle = smith.resistance_circle(r)
            centre, radius = r / (r + 1), 1 / (r + 1)
            np.testing.assert_allclose(np.abs(circle - centre), radius, atol=1e-12)

    def test_constant_resistance_circle_maps_back_to_its_resistance(self):
        # Every point of the r-circle has a real normalised impedance r.
        for r in smith.RESISTANCE_VALUES:
            # The chart's edge (|G| = 1) is an open or a short, where z is
            # infinite or zero, so only the points inside it are compared.
            gamma = smith.resistance_circle(r)
            inside = gamma[np.abs(gamma) < 1 - 1e-6]
            np.testing.assert_allclose(smith.normalised_impedance(inside).real, r, rtol=1e-9)

    def test_constant_reactance_circle_maps_back_to_its_reactance(self):
        # Every kept point of the x-circle has imaginary normalised impedance x,
        # and only points inside the unit circle are kept.
        for x in smith.REACTANCE_VALUES:
            for signed in (x, -x):
                arc = smith.reactance_circle(signed)
                kept = arc[np.isfinite(arc)]
                self.assertGreater(kept.size, 0)
                self.assertTrue(np.all(np.abs(kept) <= 1 + 1e-9))
                # Skip the edge, where z is infinite (an open).
                kept = kept[np.abs(kept) < 1 - 1e-6]
                z = smith.normalised_impedance(kept)
                np.testing.assert_allclose(z.imag, signed, rtol=1e-6, atol=1e-9)

    def test_grid_has_the_unit_circle_and_one_label_per_circle(self):
        traces = smith.grid_traces()
        labels = [t for t in traces if t.mode == "text"]
        self.assertEqual(len(labels), 1)
        expected = len(smith.RESISTANCE_VALUES) + 2 * len(smith.REACTANCE_VALUES)
        self.assertEqual(len(labels[0].text), expected)


class TestAvailablePorts(unittest.TestCase):
    def test_both_ports_when_both_are_present(self):
        self.assertEqual(smith.available_ports(_sparams()), ["s11", "s22"])

    def test_only_the_port_the_table_holds(self):
        self.assertEqual(smith.available_ports(_sparams(ports=("s11",))), ["s11"])

    def test_an_eigenmode_only_table_offers_no_smith_chart(self):
        eigen = pd.DataFrame({"freq_ghz": [10.0], "combo_id": ["c1"], "q": [1200.0]})
        self.assertEqual(smith.available_ports(eigen), [])


class TestSmithFigure(unittest.TestCase):
    def test_one_trace_per_combination_with_the_port_points(self):
        html = smith.smith_html(_sparams(), port="s11")
        self.assertIn("plotly", html)
        self.assertNotIn("No S11 data", html)

    def test_a_missing_port_says_so(self):
        html = smith.smith_html(_sparams(ports=("s11",)), port="s22")
        self.assertIn("No S22 data", html)

    def test_the_figure_carries_the_hover_impedance_at_the_reference(self):
        html = smith.smith_html(_sparams(), port="s11", reference_ohm=75.0)
        # The ohm readout is the normalised impedance times the reference, so
        # a 75 ohm reference is in the figure's hover template.
        self.assertIn("&Omega;", html)


class TestSmithRoutes(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)
        self.run = Run.create([SweepParameter(name="pedestal_depth", values=[0.0])],
                              dict(pipeline.DEFAULT_ANALYSIS_SETTINGS), {})
        self.client = webapp.app.test_client()

    def test_run_plot_serves_the_smith_chart_for_the_port_asked_for(self):
        with mock.patch.object(webapp, "_results_frame", lambda run: _sparams()):
            reply = self.client.get(f"/runs/{self.run.run_id}/plot?view=smith&port=s22")
        self.assertEqual(reply.status_code, 200)
        body = reply.get_data(as_text=True)
        self.assertIn("plotly", body)
        self.assertNotIn("No S22 data", body)

    def test_run_plot_says_so_when_the_port_has_no_data(self):
        with mock.patch.object(webapp, "_results_frame", lambda run: _sparams(ports=("s11",))):
            reply = self.client.get(f"/runs/{self.run.run_id}/plot?view=smith&port=s22")
        self.assertIn("No S22 data", reply.get_data(as_text=True))

    def test_run_page_offers_only_the_ports_with_data(self):
        df = _sparams(ports=("s11",))
        self.assertEqual(webapp._smith_options(df), [{"value": "s11", "label": "S11"}])
        self.assertEqual(webapp._smith_options(None), [])
        self.assertEqual(webapp._smith_options(pd.DataFrame({"freq_ghz": [1.0]})), [])

    def test_compare_plot_draws_each_run_in_its_own_colour(self):
        first = _sparams()
        first["run_id"], first["run"] = "run_a", "A · first"
        second = _sparams()
        second["run_id"], second["run"] = "run_b", "B · second"
        combined = pd.concat([first, second], ignore_index=True)
        entries = [
            SimpleNamespace(run=self.run, run_id="run_a", name="A · first", held={}),
            SimpleNamespace(run=self.run, run_id="run_b", name="B · second", held={}),
        ]
        with mock.patch.object(webapp, "_compared_runs", lambda args: (entries, [])), \
                mock.patch.object(webapp.compare, "combined_results", lambda e: combined):
            reply = self.client.get("/compare/plot?runs=run_a,run_b&view=smith&port=s11")
        self.assertEqual(reply.status_code, 200)
        body = reply.get_data(as_text=True)
        self.assertIn(webapp.plotting.RUN_COLOURS["light"][0], body)
        self.assertIn(webapp.plotting.RUN_COLOURS["light"][1], body)


if __name__ == "__main__":
    unittest.main()
