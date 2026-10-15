"""Tests for the pedestal recess's rounded corners (design/pedestal.py).

What is pinned: the radius is declared and read from the equations file,
the legs bound it, a recess that cannot hold the die's play is refused,
and a bare board or a recess-free combination is left alone.

Run: python -m unittest tests.test_pedestal
"""
import math
import unittest

from microwave_package import paths
from microwave_package.design import constraints, equations as eq, parameter_meta, pedestal


def defaults(**changes):
    values = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    values.update(changes)
    return values


class TestDeclaration(unittest.TestCase):
    def test_the_default_file_carries_the_radius_at_one_millimetre(self):
        self.assertEqual(defaults()["pedestal_corner_radius"], 1.0)

    def test_the_radius_is_labelled_and_in_the_form(self):
        self.assertIn("pedestal_corner_radius", parameter_meta.LABELS)
        names = [n for group in parameter_meta.GROUPS for n in group["names"]]
        self.assertIn("pedestal_corner_radius", names)


class TestLegBound(unittest.TestCase):
    def test_the_default_radius_fits_the_default_legs(self):
        self.assertLess(pedestal.TAN_HALF_TURN * 2.0, 0.8 * math.sqrt(2.0))
        self.assertIsNone(pedestal.check(defaults()))

    def test_the_limit_is_about_1_37_mm_for_the_default_leg(self):
        self.assertAlmostEqual(pedestal.leg_limit(0.8), 1.3657, places=3)

    def test_a_radius_past_the_legs_is_refused_saying_how_far(self):
        reason = pedestal.check(defaults(pedestal_corner_radius=1.5))
        self.assertIn("does not fit", reason)
        self.assertIn("1.366", reason)

    def test_square_corners_are_allowed(self):
        self.assertIsNone(pedestal.check(defaults(pedestal_corner_radius=0.0)))

    def test_a_negative_radius_is_refused(self):
        self.assertIn("negative", pedestal.check(defaults(pedestal_corner_radius=-0.1)))


class TestLegAndPlay(unittest.TestCase):
    def test_legs_must_hold_the_die_clear_of_its_play(self):
        # chip_leg 0.2 is not more than twice a 0.1 mm play.
        reason = pedestal.check(defaults(chip_leg=0.2, pedestal_corner_radius=0.0))
        self.assertIn("twice cavity_clearance", reason)

    def test_a_zero_leg_is_refused(self):
        self.assertIn("must be positive", pedestal.check(defaults(chip_leg=0.0)))

    def test_zero_play_is_a_tight_fit_not_a_mistake(self):
        self.assertIsNone(pedestal.check(defaults(cavity_clearance=0.0)))

    def test_a_radius_over_half_the_smaller_side_is_refused(self):
        # A 1.2 mm leg allows a radius up to ~2.0, but the 1.4 mm die
        # half-side caps it at 1.4 when chip_side is 2.8.
        reason = pedestal.check(defaults(chip_side=2.8, chip_length=6.3,
                                         chip_leg=1.2, pedestal_corner_radius=1.5))
        self.assertIn("smaller side", reason)


class TestWhatIsNotChecked(unittest.TestCase):
    def test_a_bare_board_has_no_recess_to_check(self):
        self.assertIsNone(pedestal.check(defaults(no_chip=1.0, chip_leg=0.0)))

    def test_no_pedestal_depth_means_no_recess(self):
        self.assertIsNone(pedestal.check(defaults(pedestal_depth=0.0, chip_leg=0.0)))

    def test_the_layout_check_refuses_an_oversized_radius(self):
        reason = constraints.check_layout(defaults(pedestal_corner_radius=1.5))
        self.assertIsNotNone(reason)
        self.assertIn("does not fit", reason)


if __name__ == "__main__":
    unittest.main()
