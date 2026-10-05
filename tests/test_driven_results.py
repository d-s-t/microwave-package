"""Tests for reading a driven run's results back, and plotting them.

The two things a driven read has to get right that an eigenmode read does
not: picking the sweep's solution rather than the adaptive one, and
converting the frequency axis out of whatever unit AEDT reported it in.
Both are silent when wrong -- the first gives a single point that looks
like a curve of length one, the second a plot that is off by nine orders
of magnitude and otherwise perfect.

Run: python -m unittest discover -s tests
"""
import unittest

import pandas as pd

from microwave_package.hfss import registry
from microwave_package.hfss.driven import results as driven_results
from microwave_package.workflow import plotting


class FakeSetup:
    def __init__(self, name="Setup_1"):
        self.name = name


class FakeHfss:
    def __init__(self, sweeps):
        self.existing_analysis_sweeps = list(sweeps)


class FakeSolutionData:
    def __init__(self, values, unit, primary_sweep="Freq"):
        self.primary_sweep = primary_sweep
        self.primary_sweep_values = list(values)
        self.units_sweeps = {primary_sweep: unit}


class TestPickingTheSweepSolution(unittest.TestCase):
    def test_the_sweep_is_chosen_over_the_adaptive_solution(self):
        """A driven setup has both. LastAdaptive holds one frequency, so
        S-parameters read off it are a point, not a curve -- and nothing
        about the result says so."""
        hfss = FakeHfss(["Setup_1 : LastAdaptive", "Setup_1 : Sweep_1"])
        self.assertEqual(
            driven_results._sweep_solution_name(hfss, FakeSetup()), "Setup_1 : Sweep_1"
        )

    def test_order_does_not_decide_it(self):
        hfss = FakeHfss(["Setup_1 : Sweep_1", "Setup_1 : LastAdaptive"])
        self.assertEqual(
            driven_results._sweep_solution_name(hfss, FakeSetup()), "Setup_1 : Sweep_1"
        )

    def test_another_setups_sweep_is_not_borrowed(self):
        hfss = FakeHfss(["Setup_2 : Sweep_1", "Setup_1 : LastAdaptive"])
        with self.assertRaises(RuntimeError):
            driven_results._sweep_solution_name(hfss, FakeSetup("Setup_1"))

    def test_an_adaptive_only_setup_says_the_sweep_did_not_solve(self):
        """The worker reports a task successful on the adaptive solution
        alone, so this is where a failed sweep actually surfaces."""
        hfss = FakeHfss(["Setup_1 : LastAdaptive"])
        with self.assertRaises(RuntimeError) as caught:
            driven_results._sweep_solution_name(hfss, FakeSetup())
        self.assertIn("sweep", str(caught.exception).lower())


class TestNamingTheSParameters(unittest.TestCase):
    """AEDT names an S-parameter after its ports, so a project whose ports
    are called port_1/port_2 has S(port_2,port_1), not S(2,1). Asking
    only for the numbered names found nothing, and the read failed with
    every S-parameter sitting right there in the "available" list."""

    WANTED = {"s11", "s21", "s22", "s12"}

    def test_ports_named_by_word_and_number(self):
        available = {"S(port_1,port_1)", "S(port_1,port_2)",
                     "S(port_2,port_1)", "S(port_2,port_2)"}
        wanted = dict((key, expr) for expr, key in driven_results._wanted_expressions(available))
        self.assertEqual(set(wanted), self.WANTED)
        self.assertEqual(wanted["s21"], "S(port_2,port_1)", "row is the receiving port")
        self.assertEqual(wanted["s12"], "S(port_1,port_2)")

    def test_ports_named_by_number_alone(self):
        available = {"S(1,1)", "S(1,2)", "S(2,1)", "S(2,2)"}
        wanted = dict((key, expr) for expr, key in driven_results._wanted_expressions(available))
        self.assertEqual(wanted["s21"], "S(2,1)")

    def test_nothing_to_check_against_asks_for_the_numbered_names(self):
        self.assertEqual(driven_results._wanted_expressions(set()),
                         list(driven_results.S_PARAMETERS))

    def test_a_one_port_project_gets_only_its_reflection(self):
        wanted = driven_results._wanted_expressions({"S(port_1,port_1)"})
        self.assertEqual(wanted, [("S(port_1,port_1)", "s11")])


class TestTheFrequencyAxis(unittest.TestCase):
    def test_ghz_passes_through(self):
        data = FakeSolutionData([1.0, 2.0, 3.0], "GHz")
        self.assertEqual(driven_results._frequencies_ghz(data), [1.0, 2.0, 3.0])

    def test_hz_is_converted(self):
        data = FakeSolutionData([1e9, 5e9], "Hz")
        self.assertEqual(driven_results._frequencies_ghz(data), [1.0, 5.0])

    def test_mhz_is_converted(self):
        data = FakeSolutionData([1000.0, 15000.0], "MHz")
        self.assertEqual(driven_results._frequencies_ghz(data), [1.0, 15.0])

    def test_a_missing_unit_is_assumed_to_be_ghz(self):
        """Which is what this pipeline's own sweeps are built in."""
        self.assertEqual(driven_results._frequencies_ghz(FakeSolutionData([2.0], None)), [2.0])

    def test_an_unknown_unit_is_refused_rather_than_guessed(self):
        with self.assertRaises(RuntimeError):
            driven_results._frequencies_ghz(FakeSolutionData([2.0], "furlongs"))


class TestPlottingDrivenResults(unittest.TestCase):
    def _driven_frame(self):
        rows = []
        for combo in ("a", "b"):
            for step in range(5):
                rows.append({
                    "freq_ghz": 1.0 + step,
                    "s11_db": -20.0 + step,
                    "s21_db": -1.0 - step,
                    "combo_id": combo,
                    # Recorded per row by post-processing, which is how a
                    # table on its own -- a saved CSV, say -- still says
                    # which solver's axes it wants.
                    "solution_type": "DrivenModal",
                })
        return pd.DataFrame(rows)

    def _eigen_frame(self):
        return pd.DataFrame([
            {"freq_ghz": 5.1, "q": 900, "mode": 1, "combo_id": "a", "pedestal_depth": 0.0},
            {"freq_ghz": 6.2, "q": 800, "mode": 2, "combo_id": "a", "pedestal_depth": 0.0},
        ])

    def test_s_parameters_are_recognised_from_the_table(self):
        self.assertTrue(plotting.has_sparameters(self._driven_frame()))
        self.assertFalse(plotting.has_sparameters(self._eigen_frame()))

    def test_the_solver_decides_and_plotting_just_asks_it(self):
        """default_axes reads the solution type off the table and hands
        the swept columns to that solver, so the two never disagree."""
        df = self._driven_frame()
        self.assertEqual(
            plotting.default_axes(df, ["pedestal_depth"]),
            registry.get("DrivenModal").default_axes(df, ["pedestal_depth"]),
        )

    def test_a_driven_run_opens_on_frequency_against_s11(self):
        axes = plotting.default_axes(self._driven_frame(), ["pedestal_depth"])
        self.assertEqual(axes["x"], "freq_ghz")
        self.assertEqual(axes["y"], "s11_db")
        self.assertEqual(axes["color"], "combo_id")

    def test_a_table_that_does_not_say_reads_as_an_eigenmode_one(self):
        """Which is what every table written before driven runs existed
        is, so the fallback is not a guess."""
        df = self._driven_frame().drop(columns=["solution_type"])
        self.assertEqual(plotting.solution_type_of(df), None)
        self.assertEqual(
            plotting.default_axes(df, []),
            registry.get(None).default_axes(df, []),
        )

    def test_the_caller_can_say_instead_of_the_table(self):
        """The web UI and the plot writer both have the run, so they pass
        its solution type rather than relying on the table."""
        df = self._driven_frame().drop(columns=["solution_type"])
        axes = plotting.default_axes(df, [], "DrivenModal")
        self.assertEqual(axes["x"], "freq_ghz")
        self.assertEqual(axes["y"], "s11_db")

    def test_an_eigenmode_run_keeps_the_axes_it_had(self):
        axes = plotting.default_axes(self._eigen_frame(), ["pedestal_depth"])
        self.assertEqual(axes["y"], "freq_ghz")
        self.assertNotEqual(axes["x"], "freq_ghz")

    def test_s_parameters_are_drawn_as_curves(self):
        """Several hundred points per combination as markers is
        unreadable, and the shape between them is the result."""
        df = self._driven_frame()
        html = plotting.figure_html(df, ["pedestal_depth"], **plotting.default_axes(df, ["pedestal_depth"]))
        self.assertIn('"mode":"lines"', html)

    def test_eigenmode_points_stay_points(self):
        """Each is its own measurement; joining them would invent a trend."""
        df = self._eigen_frame()
        html = plotting.figure_html(df, ["pedestal_depth"], **plotting.default_axes(df, ["pedestal_depth"]))
        self.assertNotIn('"mode":"lines"', html)

    def test_s_parameters_against_a_swept_parameter_stay_points(self):
        """Only frequency makes a curve -- one point per combination
        against a swept dimension is a scatter like any other."""
        df = self._driven_frame()
        df["pedestal_depth"] = 0.0
        html = plotting.figure_html(df, ["pedestal_depth"], x="pedestal_depth", y="s11_db")
        self.assertNotIn('"mode":"lines"', html)

    def test_the_s_parameter_columns_are_offered_as_axes(self):
        options = [o["value"] for o in plotting.axis_options(self._driven_frame(), [])]
        self.assertIn("s11_db", options)
        self.assertIn("s21_db", options)


if __name__ == "__main__":
    unittest.main()
