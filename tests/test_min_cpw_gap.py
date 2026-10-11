"""A CPW gap under 100 um is refused: on RT/duroid 5880 a 0.2 mm trace needs a gap
well under that, and a board with an 86 um gap did not transmit (S21 -91 dB at 2 GHz)
while one with 111 um did. The gap is the one for the line under the lid."""
import unittest

from microwave_package import paths
from microwave_package.design import constraints, cpw, equations as eq

DUROID = "Rogers RT/duroid 5880 (tm)"


def values(trace):
    base = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    return {**base, "cpw_trace_width": trace, "no_chip": 1.0, "pedestal_depth": 0.0}


def gap_um(trace):
    v = values(trace)
    return 1000.0 * cpw.solve_gap_for_z0(
        trace, v["pcb_width"], cpw.permittivity_of(DUROID), conductor_thickness=v["copper_thickness"],
        **constraints.lid_cover(v))


class TestMinimumGap(unittest.TestCase):
    def test_a_narrow_trace_on_5880_is_refused_with_the_gap_in_the_message(self):
        reason = constraints.check_layout(values(0.3), DUROID)
        self.assertIn(f"{gap_um(0.3):.0f} um", reason)
        self.assertIn("widen cpw_trace_width", reason)
        self.assertLess(gap_um(0.3), 1000.0 * constraints.MIN_CPW_GAP)

    def test_the_default_trace_on_5880_is_refused(self):
        self.assertIn("widen cpw_trace_width", constraints.check_layout(values(0.2), DUROID))

    def test_a_wider_trace_on_5880_is_accepted(self):
        self.assertIsNone(constraints.check_layout(values(0.5), DUROID))
        self.assertGreater(gap_um(0.5), 1000.0 * constraints.MIN_CPW_GAP)

    def test_the_default_substrate_is_unaffected(self):
        self.assertIsNone(constraints.check_layout(values(0.2)))


if __name__ == "__main__":
    unittest.main()
