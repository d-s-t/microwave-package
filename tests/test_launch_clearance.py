"""The ground keep-out round the SMP signal pad.

The pad (0.48 mm) is wider than the trace, and the ground edge follows the
trace, so without a keep-out the pad-to-ground clearance is the line's gap
minus half the difference -- 71 um for a 0.4 mm trace with a 111 um gap, 54
um with 94 um, both under the 100 um floor. The generator pulls the ground
back (`launch_clearance`), and the layout check refuses a value under
MIN_CPW_GAP.

Run: python -m unittest discover -s tests
"""
import re
import unittest
from unittest import mock

from microwave_package import paths
from microwave_package.design import altium_generation as ag, constraints, cpw, equations as eq, smp

PAD = 2.0 * smp.SIGNAL_HALF_W


def defaults():
    return eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))


def bare_clearance(trace, gap):
    """What the clearance would be with the ground edge at the gap edge."""
    return gap - (PAD - trace) / 2.0


class TestTheProblem(unittest.TestCase):
    def test_the_numbers_from_the_analysis(self):
        self.assertAlmostEqual(PAD, 0.48)
        self.assertAlmostEqual(bare_clearance(0.45, 0.129), 0.114)
        self.assertAlmostEqual(bare_clearance(0.4, 0.111), 0.071)
        self.assertAlmostEqual(bare_clearance(0.4, 0.094), 0.054)


class TestPadGroundClearance(unittest.TestCase):
    def test_the_default_is_the_documented_one_and_clears_the_floor(self):
        v = defaults()
        self.assertEqual(constraints.launch_clearance(v), constraints.DEFAULT_LAUNCH_CLEARANCE)
        self.assertGreaterEqual(constraints.DEFAULT_LAUNCH_CLEARANCE, constraints.MIN_CPW_GAP)

    def test_narrow_gaps_are_pulled_back_to_the_clearance(self):
        for trace, gap in ((0.4, 0.111), (0.4, 0.094), (0.45, 0.129)):
            v = dict(defaults(), cpw_trace_width=trace, launch_clearance=0.2)
            self.assertAlmostEqual(constraints.pad_ground_clearance(v, gap), 0.2)
            self.assertGreaterEqual(constraints.pad_ground_edge(v, gap), trace / 2 + gap)

    def test_a_wide_gap_is_left_alone(self):
        v = dict(defaults(), cpw_trace_width=0.2, launch_clearance=0.12)
        gap = 0.4
        self.assertAlmostEqual(constraints.pad_ground_edge(v, gap), 0.1 + gap)

    def test_the_default_value_travels_to_the_generator(self):
        params = ag.build_params(defaults())
        self.assertIn("launch_clearance", params)
        self.assertIn("smp_sig_half_w", params)
        self.assertAlmostEqual(params["smp_sig_half_w"], 0.24)
        self.assertAlmostEqual(params["smp_sig_half_l"], 0.7)


class TestTheCheck(unittest.TestCase):
    def test_the_shipped_defaults_pass(self):
        self.assertIsNone(constraints.check_layout(defaults()))

    def test_a_clearance_under_the_minimum_is_refused_with_the_numbers(self):
        v = dict(defaults(), cpw_trace_width=0.4, launch_clearance=0.05)
        reason = constraints.check_launch_clearance(v, 0.111)
        self.assertIsNotNone(reason)
        self.assertIn("launch_clearance", reason)
        self.assertIn("50 um", reason)
        self.assertIn("71 um", reason)  # what it would have been with no keep-out
        self.assertIn("100 um", reason)

    def test_exactly_the_minimum_passes(self):
        v = dict(defaults(), launch_clearance=constraints.MIN_CPW_GAP)
        self.assertIsNone(constraints.check_launch_clearance(v, 0.111))

    def test_check_layout_reports_it(self):
        # a 0.4 mm trace has no 50 ohm gap on the default board, so the
        # solve is pinned to the 111 um of the study this rule comes from
        v = dict(defaults(), cpw_trace_width=0.4, launch_clearance=0.05)
        with mock.patch.object(cpw, "solve_gap_for_z0", return_value=0.111):
            reason = constraints.check_layout(v)
            self.assertIsNotNone(reason)
            self.assertIn("launch_clearance", reason)
            ok = constraints.check_layout(dict(v, launch_clearance=0.2))
        self.assertIsNone(ok)

    def test_zero_clearance_is_refused_where_the_gap_is_narrow(self):
        v = dict(defaults(), cpw_trace_width=0.4, launch_clearance=0.0)
        self.assertIsNotNone(constraints.check_launch_clearance(v, 0.094))

    def test_a_wide_gap_needs_no_keep_out(self):
        """The ground is already far enough away; the parameter is moot."""
        v = dict(defaults(), launch_clearance=0.0)
        self.assertIsNone(constraints.check_launch_clearance(v, 0.4))


class TestAltiumScriptAgrees(unittest.TestCase):
    PAS = (paths.BASE_PARTS_DIR / "altium" / "GenerateFromParams.pas")

    def setUp(self):
        self.text = self.PAS.read_text(encoding="utf-8", errors="replace")

    def test_the_default_matches_python(self):
        m = re.search(r"ParamOf\(P, 'launch_clearance', ([0-9.]+)\)", self.text)
        self.assertIsNotNone(m)
        self.assertAlmostEqual(float(m.group(1)), constraints.DEFAULT_LAUNCH_CLEARANCE)

    def test_the_script_reads_what_python_sends(self):
        for name in ("smp_sig_half_w", "smp_sig_half_l", "smp_sig_v"):
            self.assertIn(f"'{name}'", self.text)

    def test_the_equations_file_carries_it(self):
        self.assertIn("launch_clearance", defaults())
        self.assertAlmostEqual(defaults()["launch_clearance"],
                               constraints.DEFAULT_LAUNCH_CLEARANCE)


if __name__ == "__main__":
    unittest.main()
