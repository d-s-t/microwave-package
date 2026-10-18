"""Tests for the box cavity's rounded corners (constraints.check_cavity_corners).

What is pinned: the radius is declared and read from the equations file, it
must be above 0 and less than half the cavity's smaller side, and the PCB's
own corner has to fit inside the cavity's rounded corner. The fit bound is
checked against the outlines themselves, not only against the formula.

Run: python -m unittest tests.test_cavity_corner
"""
import math
import unittest

from microwave_package import paths
from microwave_package.design import constraints, equations as eq, parameter_meta


def defaults(**changes):
    values = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    values.update(changes)
    return values


def fits_inside_cavity(R, g, r, X=25.0, Y=12.5, n=2000):
    """Whether a PCB outline with half-sides X - g, Y - g and corner radius r
    lies inside the cavity outline with half-sides X, Y and corner radius R.
    Samples the PCB's corner arcs and straight edges, and tests each point."""
    def inside(x, y):
        if abs(x) > X + 1e-12 or abs(y) > Y + 1e-12:
            return False
        cx, cy = X - R, Y - R
        if x > cx + 1e-12 and y > cy + 1e-12:
            return (x - cx) ** 2 + (y - cy) ** 2 <= R * R + 1e-9
        return True

    hx, hy = X - g, Y - g
    cx, cy = hx - r, hy - r
    pts = []
    for k in range(n + 1):
        t = math.pi / 2 * k / n
        if r > 0:
            for sx, sy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
                pts.append((sx * (cx + r * math.cos(t)), sy * (cy + r * math.sin(t))))
        s = -1 + 2 * k / n
        pts += [(hx, s * cy), (-hx, s * cy), (s * cx, hy), (s * cx, -hy)]
    return all(inside(x, y) for x, y in pts)


class TestDeclaration(unittest.TestCase):
    def test_the_default_file_carries_the_radius_at_one_millimetre(self):
        self.assertEqual(defaults()["cavity_corner_radius"], 1.0)

    def test_the_radius_is_labelled_and_in_the_form(self):
        self.assertIn("cavity_corner_radius", parameter_meta.LABELS)
        names = [n for group in parameter_meta.GROUPS for n in group["names"]]
        self.assertIn("cavity_corner_radius", names)

    def test_the_default_combination_passes(self):
        self.assertIsNone(constraints.check_cavity_corners(defaults()))


class TestRadiusRange(unittest.TestCase):
    def test_a_zero_radius_is_refused(self):
        # A sketch fillet cannot be zero, and SolidWorks would keep another radius.
        reason = constraints.check_cavity_corners(defaults(cavity_corner_radius=0.0))
        self.assertIn("must be above 0", reason)

    def test_a_negative_radius_is_refused(self):
        reason = constraints.check_cavity_corners(defaults(cavity_corner_radius=-0.5))
        self.assertIn("must be above 0", reason)

    def test_a_half_millimetre_radius_is_allowed(self):
        # The spec says above 0, not 1 mm; the 1 mm end mill is a reason to
        # choose 1, not a limit the check enforces.
        self.assertIsNone(constraints.check_cavity_corners(defaults(cavity_corner_radius=0.5)))

    def test_a_radius_of_half_the_smaller_side_is_refused(self):
        # Smaller side is pcb_side 25 mm, so the limit is 12.5 mm.
        reason = constraints.check_cavity_corners(defaults(cavity_corner_radius=12.5))
        self.assertIn("half the cavity's smaller side", reason)

    def test_just_under_half_the_smaller_side_is_allowed(self):
        self.assertIsNone(constraints.check_cavity_corners(
            defaults(cavity_corner_radius=12.4, pcb_corner_radius=12.4)))

    def test_the_smaller_side_is_the_one_that_counts(self):
        # pcb_length 50, pcb_side 25: a radius of 13 is over half the short side.
        reason = constraints.check_cavity_corners(defaults(cavity_corner_radius=13.0))
        self.assertIn("smaller side", reason)


class TestPcbCornerFit(unittest.TestCase):
    def test_a_sharp_pcb_corner_is_refused_for_a_rounded_cavity(self):
        reason = constraints.check_cavity_corners(defaults(pcb_corner_radius=0.0))
        self.assertIn("poke into the cavity", reason)

    def test_a_pcb_corner_just_under_the_cavity_radius_is_refused(self):
        # With no wall gap the PCB corner must equal the cavity corner. 0.9 mm is the
        # value the cavity_clearance-based rule would allow, and it does not fit.
        reason = constraints.check_cavity_corners(defaults(pcb_corner_radius=0.9))
        self.assertIsNotNone(reason)
        self.assertIn("1 mm", reason)

    def test_a_pcb_corner_equal_to_the_cavity_radius_fits(self):
        self.assertIsNone(constraints.check_cavity_corners(defaults(pcb_corner_radius=1.0)))

    def test_a_larger_pcb_corner_fits(self):
        self.assertIsNone(constraints.check_cavity_corners(defaults(pcb_corner_radius=1.5)))

    def test_without_a_pcb_corner_parameter_the_cavity_radius_stands_in(self):
        # Stage 2 adds pcb_corner_radius with this same default, so until then
        # the PCB is taken to match the cavity.
        self.assertNotIn("pcb_corner_radius", defaults())
        self.assertIsNone(constraints.check_cavity_corners(defaults()))

    def test_cavity_clearance_does_not_relax_the_pcb_corner(self):
        # cavity_clearance is the die's play in the PCB opening, not the PCB's
        # gap to the wall, so a larger clearance must not admit a smaller corner.
        reason = constraints.check_cavity_corners(
            defaults(pcb_corner_radius=0.9, cavity_clearance=0.3))
        self.assertIsNotNone(reason)

    def test_the_bound_is_r_minus_2_plus_root2_times_the_gap(self):
        self.assertAlmostEqual(constraints.pcb_corner_floor(1.0, 0.0), 1.0)
        self.assertAlmostEqual(constraints.pcb_corner_floor(1.0, 0.1), 1.0 - 0.1 * (2 + math.sqrt(2)))
        self.assertEqual(constraints.pcb_corner_floor(0.5, 1.0), 0.0)

    def test_a_wall_gap_lowers_the_bound_for_a_future_board(self):
        self.assertIsNone(constraints.check_cavity_corners(
            defaults(pcb_corner_radius=0.66), wall_gap=0.1))
        reason = constraints.check_cavity_corners(
            defaults(pcb_corner_radius=0.65), wall_gap=0.1)
        self.assertIn("poke into the cavity", reason)

    def test_the_bound_matches_the_outlines(self):
        # The formula is only worth trusting if the outlines agree with it:
        # the fit fails just below the bound and holds at it, at several radii and gaps.
        for R in (0.5, 1.0, 1.5):
            for g in (0.0, 0.05, 0.1):
                floor = constraints.pcb_corner_floor(R, g)
                self.assertTrue(fits_inside_cavity(R, g, floor, n=400), (R, g))
                self.assertFalse(fits_inside_cavity(R, g, floor - 0.005, n=400), (R, g))


class TestWhatIsChecked(unittest.TestCase):
    def test_a_bare_board_still_has_its_cavity(self):
        # The box is the same with or without a die, so the rule holds for no_chip too.
        reason = constraints.check_cavity_corners(defaults(no_chip=1.0, cavity_corner_radius=0.0))
        self.assertIn("must be above 0", reason)

    def test_the_layout_check_refuses_a_zero_radius(self):
        reason = constraints.check_layout(defaults(cavity_corner_radius=0.0))
        self.assertIsNotNone(reason)
        self.assertIn("must be above 0", reason)

    def test_the_layout_check_refuses_a_bare_board_with_a_zero_radius(self):
        # pedestal_depth 0: a bare board's recess is refused by its own check first.
        reason = constraints.check_layout(
            defaults(no_chip=1.0, pedestal_depth=0.0, cavity_corner_radius=0.0))
        self.assertIn("must be above 0", reason)

    def test_the_layout_check_refuses_a_sharp_pcb_corner(self):
        reason = constraints.check_layout(defaults(pcb_corner_radius=0.0))
        self.assertIn("poke into the cavity", reason)


if __name__ == "__main__":
    unittest.main()
