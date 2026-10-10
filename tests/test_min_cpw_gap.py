"""A CPW gap under 100 um is refused: on RT/duroid 5880 the default 0.2 mm trace
needs an 86 um gap, and that board did not transmit (S21 -91 dB at 2 GHz)
while the 0.4 mm trace (111 um) did."""
import unittest

from microwave_package import paths
from microwave_package.design import constraints, equations as eq

DUROID = "Rogers RT/duroid 5880 (tm)"


def values(trace):
    base = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    return {**base, "cpw_trace_width": trace, "route_width": 0.8, "no_chip": 1.0, "pedestal_depth": 0.0}


class TestMinimumGap(unittest.TestCase):
    def test_the_default_trace_on_5880_is_refused_with_the_gap_in_the_message(self):
        reason = constraints.check_layout(values(0.2), DUROID)
        self.assertIn("86 um", reason)
        self.assertIn("widen cpw_trace_width", reason)

    def test_a_wider_trace_on_5880_is_accepted(self):
        for trace in (0.4, 0.5):
            self.assertIsNone(constraints.check_layout(values(trace), DUROID), trace)

    def test_the_default_substrate_is_unaffected(self):
        self.assertIsNone(constraints.check_layout(values(0.2)))


if __name__ == "__main__":
    unittest.main()
