"""The relief circles at the SMP window corners in the lid (design/constraints.py:
check_smp_relief, smp_relief_centres, smp_relief_area, smp_relief_route_gap;
base_parts/add_smp_relief.py cuts them).

What is pinned:
- the defaults pass the layout check: each circle is centred smp_relief_inset = 0.6 mm
  inside the two walls of its window corner, so it bites 0.4 mm into the lid;
- the eight centres are (+-14.5, +-1.95) and (+-18.4, +-1.95); at inset 0 they are the
  old window corners (+-13.9, +-2.55) and (+-19.0, +-2.55);
- the lid material removed per corner is A(0.6) = 0.872693 mm2, so the lid volume changes
  by 8 x 1.5 x (3 pi/4 - A(0.6)) = 17.802 mm3 against the old dog-bone, and inset 0 gives
  back the 28.2743 mm3 the SolidWorks cut removed on 2026-10-20;
- the connector housing (5.0 x 5.0 mm in plan, corners cut back 0.42 mm, the +x one
  from the AEDT model) clears the relief arcs by 0.39 mm at inset 0.6, and by 0.07 mm at
  inset 0.85, which is below the 0.1 mm margin;
- a relief that leaves less than SMP_RELIEF_MIN_CLEARANCE of lid to the route channel's
  end, the outside edge or a countersink is refused, with the parameter named; an inset
  whose circle does not cover the window corner is refused.

Run: python -m unittest tests.test_smp_relief
"""
import math
import unittest

from microwave_package import paths
from microwave_package.design import constraints, equations as eq, parameter_meta

# The +x connector housing in the lid's frame, mm (the -x one is the mirror image).
# AEDT model numbers: 5.0 x 5.0 mm in plan, corners cut back 0.42 mm at 45 degrees.
HOUSING_VERTICES = [(14.42, 2.50), (18.58, 2.50), (19.00, 2.08), (19.00, -2.08),
                    (18.58, -2.50), (14.42, -2.50), (14.00, -2.08), (14.00, 2.08)]


def defaults(**changes):
    values = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    values.update(changes)
    return values


def corner_clearance(values):
    """Least lid between a housing corner and the relief arcs, mm, on the +x side (the
    -x side is the mirror image). A corner is inside its circle, so the clearance is the
    radius less the distance to the nearest centre."""
    radius = values["smp_relief_radius"]
    centres = [c for c in constraints.smp_relief_centres(values) if c[0] > 0]
    return min(radius - min(math.hypot(x - cx, z - cz) for cx, cz in centres) for x, z in HOUSING_VERTICES)


class TestDeclaration(unittest.TestCase):
    def test_the_default_radius_is_one_millimetre_and_labelled(self):
        self.assertEqual(defaults()["smp_relief_radius"], 1.0)
        self.assertIn("smp_relief_radius", parameter_meta.LABELS)

    def test_the_default_inset_is_point_six_and_labelled(self):
        self.assertEqual(defaults()["smp_relief_inset"], 0.6)
        self.assertIn("smp_relief_inset", parameter_meta.LABELS)


class TestCentres(unittest.TestCase):
    def assertCentres(self, got, want):
        self.assertEqual(len(got), 8)
        for c in want:
            self.assertTrue(any(abs(c[0] - g[0]) < 1e-9 and abs(c[1] - g[1]) < 1e-9 for g in got), c)

    def test_the_default_centres_are_0p6_inside_the_walls(self):
        want = [(sx * x, sz * 1.95) for x in (14.5, 18.4) for sx in (1, -1) for sz in (1, -1)]
        self.assertCentres(constraints.smp_relief_centres(defaults()), want)

    def test_inset_zero_is_the_old_window_corners(self):
        want = [(sx * x, sz * 2.55) for x in (13.9, 19.0) for sx in (1, -1) for sz in (1, -1)]
        self.assertCentres(constraints.smp_relief_centres(defaults(smp_relief_inset=0.0)), want)


class TestArea(unittest.TestCase):
    def test_inset_zero_is_three_pi_over_four(self):
        self.assertAlmostEqual(constraints.smp_relief_area(1.0, 0.0), 3 * math.pi / 4, places=7)

    def test_the_default_inset_removes_0p872693_per_corner(self):
        self.assertAlmostEqual(constraints.smp_relief_area(1.0, 0.6), 0.872693, places=6)

    def test_eight_corners_three_pi_over_four_at_the_plate_thickness(self):
        # Checked in SolidWorks, 2026-10-20: -28.2743 mm3 (8 x 3 pi/4 x 1^2 x 1.5).
        values = defaults(smp_relief_inset=0.0)
        removed = 8 * constraints.smp_relief_area(values["smp_relief_radius"], 0.0) * values["lid_width"]
        self.assertAlmostEqual(removed, 28.2743, places=4)

    def test_the_default_inset_changes_the_volume_by_17p802_against_the_old_dog_bone(self):
        values = defaults()
        change = 8 * values["lid_width"] * (3 * math.pi / 4 - constraints.smp_relief_area(
            values["smp_relief_radius"], values["smp_relief_inset"]))
        self.assertAlmostEqual(change, 17.802013, places=5)


class TestCheck(unittest.TestCase):
    def test_the_defaults_pass(self):
        self.assertIsNone(constraints.check_smp_relief(defaults()))

    def test_the_defaults_pass_the_layout_check(self):
        self.assertIsNone(constraints.check_layout(defaults()))

    def test_a_zero_radius_is_refused(self):
        self.assertIn("must be above 0", constraints.check_smp_relief(defaults(smp_relief_radius=0.0)))

    def test_an_inset_that_does_not_cover_the_window_corner_is_refused(self):
        # 0.8 sqrt 2 = 1.13 > 1: the circle would miss the window's sharp corner.
        reason = constraints.check_smp_relief(defaults(smp_relief_inset=0.8))
        self.assertIsNotNone(reason)
        self.assertIn("does not cover the window corner", reason)

    def test_a_channel_that_comes_too_near_the_relief_is_refused(self):
        # A 2.0 mm channel (half 1.0) leaves 0.12 mm between its end and the relief.
        reason = constraints.check_smp_relief(defaults(route_width=2.0))
        self.assertIsNotNone(reason)
        self.assertIn("route channel's end", reason)

    def test_a_relief_too_near_the_outside_edge_is_refused(self):
        # The outer window wall is port_space = 0.8 mm from the edge and the relief centre
        # is 0.6 mm inside it, so the circle reaches to 0.4 mm from the edge.
        reason = constraints.check_smp_relief(defaults(port_space=0.8))
        self.assertIsNotNone(reason)
        self.assertIn("outside edge", reason)


class TestFit(unittest.TestCase):
    def test_the_housing_clears_the_reliefs_by_0p39_at_the_default_inset(self):
        self.assertAlmostEqual(corner_clearance(defaults()), 0.386, places=3)

    def test_an_inset_of_0p85_leaves_only_0p07_at_the_housing_corners(self):
        # Below the 0.1 mm margin: the number behind the choice of 0.6.
        self.assertAlmostEqual(corner_clearance(defaults(smp_relief_inset=0.85)), 0.069, places=3)

    def test_the_route_gap_at_the_default_inset_is_0p477(self):
        values = defaults()
        gap = min(constraints.smp_relief_route_gap(values, c) for c in constraints.smp_relief_centres(values))
        self.assertAlmostEqual(gap, 0.4771, places=3)
        self.assertGreaterEqual(gap, constraints.SMP_RELIEF_MIN_CLEARANCE)


if __name__ == "__main__":
    unittest.main()
