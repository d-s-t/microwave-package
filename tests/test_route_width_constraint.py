"""A lid relief channel no wider than the trace shorts the trace to the box
(the lid's walls land on the trace edges): a model that solves and reads as no
transmission at all. The layout check refuses it -- and, since the line is solved
under the lid, one that is wider than the trace but does not clear the whole CPW."""
import unittest

from microwave_package import paths
from microwave_package.design import constraints, equations as eq

DUROID = "Rogers RT/duroid 5880 (tm)"


def values(trace, route_width=None):
    base = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    v = {**base, "cpw_trace_width": trace, "no_chip": 1.0, "pedestal_depth": 0.0}
    if route_width is not None:
        v["route_width"] = route_width
    return v


class TestRouteWidth(unittest.TestCase):
    def test_a_channel_as_wide_as_the_trace_is_refused(self):
        reason = constraints.check_layout(values(0.5, 0.5), DUROID)
        self.assertIn("shorts it", reason)
        self.assertIn("route_width", reason)

    def test_a_channel_too_little_wider_is_refused(self):
        self.assertIsNotNone(constraints.check_layout(values(0.5, 0.55), DUROID))

    def test_a_channel_with_the_margin_passes_this_rule_and_meets_the_next(self):
        """0.1 mm over the trace is enough not to short it, not enough to hold its gaps."""
        reason = constraints.check_layout(values(0.5, 0.6), DUROID)
        self.assertNotIn("shorts it", reason)
        self.assertIn("raise route_width to at least", reason)
        self.assertIsNone(constraints.check_layout(values(0.5, 1.2), DUROID))

    def test_the_shipped_defaults_are_unaffected(self):
        self.assertIsNone(constraints.check_layout(values(0.2)))

    def test_the_message_says_what_to_change(self):
        reason = constraints.check_layout(values(0.5, 0.5), DUROID)
        self.assertIn("at least 0.6", reason)


if __name__ == "__main__":
    unittest.main()
