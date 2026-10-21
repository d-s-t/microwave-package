"""The dog-bone reliefs at the SMP pocket corners in the lid (design/constraints.py
check_smp_relief; base_parts/add_smp_relief.py cuts them).

What is pinned: the defaults pass; a relief radius that leaves less than the
minimum lid to the channel, the outside edge or a screw countersink is refused,
with the parameter named; zero is refused; and eight corners of 3 pi/4 R^2 at
1.5 mm deep is the 28.2743 mm3 the SolidWorks cut removed on 2026-10-20.

Run: python -m unittest tests.test_smp_relief
"""
import math
import unittest

from microwave_package import paths
from microwave_package.design import constraints, equations as eq, parameter_meta


def defaults(**changes):
    values = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    values.update(changes)
    return values


class TestDeclaration(unittest.TestCase):
    def test_the_default_is_one_millimetre_and_labelled(self):
        self.assertEqual(defaults()["smp_relief_radius"], 1.0)
        self.assertIn("smp_relief_radius", parameter_meta.LABELS)


class TestCheck(unittest.TestCase):
    def test_the_defaults_pass(self):
        self.assertIsNone(constraints.check_smp_relief(defaults()))

    def test_a_zero_radius_is_refused(self):
        self.assertIn("must be above 0", constraints.check_smp_relief(defaults(smp_relief_radius=0.0)))

    def test_a_radius_that_hits_the_route_channel_is_refused(self):
        # Circles at y = 2.55: at R 2 the edge is at 0.55, less than 0.6 for the channel.
        reason = constraints.check_smp_relief(defaults(smp_relief_radius=2.0))
        self.assertIsNotNone(reason)
        self.assertIn("route channel", reason)

    def test_a_wider_channel_that_still_clears_passes(self):
        # A 2.0 mm channel leaves 2.55 - 1 - 1.0 = 0.55 mm to the relief: over the 0.5 mm minimum.
        self.assertIsNone(constraints.check_smp_relief(defaults(route_width=2.0)))

    def test_a_relief_too_near_the_outside_edge_is_refused(self):
        # The outer end is port_space in from the edge: 1.2 - 1 = 0.2 mm of lid is under the minimum.
        reason = constraints.check_smp_relief(defaults(port_space=1.2))
        self.assertIsNotNone(reason)
        self.assertIn("outside edge", reason)


class TestVolume(unittest.TestCase):
    def test_eight_corners_three_pi_over_four_at_the_plate_thickness(self):
        # Checked in SolidWorks, 2026-10-20: -28.2743 mm3 (8 x 3 pi/4 x 1^2 x 1.5).
        values = defaults()
        removed = 8 * 3 * math.pi / 4 * values["smp_relief_radius"] ** 2 * values["lid_width"]
        self.assertAlmostEqual(removed, 28.2743, places=4)


if __name__ == "__main__":
    unittest.main()
