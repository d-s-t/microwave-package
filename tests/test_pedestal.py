"""Tests for the pedestal recess (design/pedestal.py): its two shapes, their
separate sizes, the limits, the square's corner radius, the die-fall check and
the rim the die rests on.

What is pinned: the square's side (pedestal_size, 6.2) and the circle's
diameter (pedestal_diameter, 6.25) are declared, labelled and read from the
equations file; the square must be smaller than the 6.25 mm chip and the
circle no bigger; the square's radius must be above 0 and below half its side;
the die's corners must stay over the rim at the largest play, per shape; the
rim under a 6.25 mm die is 1.4809 mm2 for the square and 8.3829 mm2 for the
circle; a legacy chip_leg does not refuse anything; and a bare board or a
recess-free combination is left alone; a bare board's recess is checked like a die board's.

Run: python -m unittest tests.test_pedestal
"""
import unittest

from microwave_package import paths
from microwave_package.design import constraints, equations as eq, parameter_meta, pedestal


def defaults(**changes):
    values = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    values.update(changes)
    return values


class TestDeclaration(unittest.TestCase):
    def test_the_default_file_carries_the_two_sizes_shape_and_radius(self):
        values = defaults()
        self.assertEqual(values["pedestal_size"], 6.2)
        self.assertEqual(values["pedestal_diameter"], 6.25)
        self.assertEqual(values["pedestal_shape"], 0)
        self.assertEqual(values["pedestal_corner_radius"], 1.0)

    def test_the_new_parameters_are_labelled_and_in_the_form(self):
        names = [n for group in parameter_meta.GROUPS for n in group["names"]]
        for name in ("pedestal_size", "pedestal_diameter", "pedestal_shape", "pedestal_corner_radius"):
            self.assertIn(name, parameter_meta.LABELS)
            self.assertIn(name, names)

    def test_the_shape_is_an_integer_not_a_boolean(self):
        self.assertIn("pedestal_shape", parameter_meta.INTEGER_PARAMETERS)
        self.assertNotIn("pedestal_shape", parameter_meta.BOOLEAN_PARAMETERS)

    def test_the_defaults_pass(self):
        self.assertIsNone(pedestal.check(defaults()))

    def test_each_shape_reads_its_own_size(self):
        values = defaults(pedestal_size=6.0, pedestal_diameter=5.5)
        self.assertEqual(pedestal.recess_size(values), 6.0)           # square
        self.assertEqual(pedestal.recess_size({**values, "pedestal_shape": 1}), 5.5)


class TestShapeAndSize(unittest.TestCase):
    def test_a_shape_other_than_0_or_1_is_refused(self):
        self.assertIn("0 (square) or 1 (circle)", pedestal.check(defaults(pedestal_shape=2)))

    def test_the_circle_at_the_chip_size_passes(self):
        self.assertIsNone(pedestal.check(defaults(pedestal_shape=1)))

    def test_a_square_at_the_chip_size_is_refused(self):
        # The square must be smaller than the die: at 6.25 it would leave no rim.
        reason = pedestal.check(defaults(pedestal_size=6.25))
        self.assertIn("not smaller than the 6.25 mm chip", reason)
        self.assertIn("reduce pedestal_size", reason)

    def test_a_square_bigger_than_the_chip_is_refused(self):
        self.assertIn("not smaller than", pedestal.check(defaults(pedestal_size=6.3)))

    def test_a_circle_bigger_than_the_chip_is_refused(self):
        reason = pedestal.check(defaults(pedestal_shape=1, pedestal_diameter=6.3))
        self.assertIn("bigger than the 6.25 mm chip", reason)
        self.assertIn("reduce pedestal_diameter", reason)

    def test_the_square_size_does_not_limit_the_circle_and_back(self):
        # pedestal_size is the square only: a bad square does not refuse the circle, nor the reverse.
        self.assertIsNone(pedestal.check(defaults(pedestal_shape=1, pedestal_size=6.3)))
        self.assertIsNone(pedestal.check(defaults(pedestal_shape=0, pedestal_diameter=6.3)))

    def test_a_smaller_square_passes(self):
        self.assertIsNone(pedestal.check(defaults(pedestal_size=6.0)))

    def test_a_zero_or_negative_square_is_refused(self):
        self.assertIn("must be positive", pedestal.check(defaults(pedestal_size=0.0)))
        self.assertIn("must be positive", pedestal.check(defaults(pedestal_size=-1.0)))

    def test_a_zero_circle_is_refused(self):
        self.assertIn("must be positive", pedestal.check(defaults(pedestal_shape=1, pedestal_diameter=0.0)))


class TestRimSupport(unittest.TestCase):
    def test_the_square_leaves_1_4809_mm2_under_a_6_25_die(self):
        # 6.25^2 - (6.2^2 - (4 - pi) * 1^2) = 1.4809 mm2 (the checked numbers, 2026-10-20)
        self.assertAlmostEqual(pedestal.rim_support_area(defaults()), 1.4809, places=4)

    def test_the_circle_leaves_8_3829_mm2_under_a_6_25_die(self):
        self.assertAlmostEqual(pedestal.rim_support_area(defaults(pedestal_shape=1)), 8.3829, places=4)

    def test_the_old_6_25_square_left_0_8584_mm2(self):
        self.assertAlmostEqual(pedestal.rim_support_area(defaults(pedestal_size=6.25)), 0.8584, places=4)

    def test_the_die_cannot_fall_in_with_the_square(self):
        # The square is 0.025 mm inside the die on each side, so the die rests on the rim.
        self.assertGreater(pedestal.size(defaults()), 0)
        self.assertLess(pedestal.size(defaults()), pedestal.CHIP_SIZE)
        self.assertAlmostEqual((pedestal.CHIP_SIZE - pedestal.size(defaults())) / 2.0, 0.025)


class TestSquareRadius(unittest.TestCase):
    def test_a_zero_radius_is_refused(self):
        # A sketch fillet cannot be zero, and SolidWorks would keep its last radius.
        self.assertIn("must be above 0", pedestal.check(defaults(pedestal_corner_radius=0.0)))

    def test_a_negative_radius_is_refused(self):
        self.assertIn("must be above 0", pedestal.check(defaults(pedestal_corner_radius=-0.1)))

    def test_a_radius_of_half_the_side_is_refused(self):
        # At half the side the straight walls of the square vanish.
        reason = pedestal.check(defaults(pedestal_corner_radius=3.1))
        self.assertIn("less than half pedestal_size", reason)

    def test_a_radius_just_under_half_the_side_passes_the_size_rule(self):
        self.assertIsNone(pedestal.check(defaults(pedestal_corner_radius=3.0)))

    def test_the_radius_is_not_limited_by_the_legs_any_more(self):
        # chip_leg no longer drives the pocket, so a radius past the old leg limit is fine.
        self.assertIsNone(pedestal.check(defaults(pedestal_corner_radius=1.5)))

    def test_the_circle_ignores_the_radius(self):
        self.assertIsNone(pedestal.check(defaults(pedestal_shape=1, pedestal_corner_radius=0.0)))


class TestDieFall(unittest.TestCase):
    def test_the_square_default_holds_the_die_at_the_clearance(self):
        self.assertIsNone(pedestal.check(defaults(cavity_clearance=0.1)))

    def test_a_square_with_a_big_play_lets_the_corner_drop(self):
        # At 0.5 mm play the die's corner comes in past the 1 mm fillet of the 6.2 mm square.
        # (At 0.3 mm the 6.2 mm square still holds: its straight walls sit 0.025 mm
        # further out than the old 6.25 mm square's, so the play allowed is a little more.)
        reason = pedestal.check(defaults(cavity_clearance=0.5))
        self.assertIn("would drop past", reason)

    def test_the_circle_holds_the_die_at_the_clearance(self):
        self.assertIsNone(pedestal.check(defaults(pedestal_shape=1, cavity_clearance=0.3)))

    def test_a_circle_with_a_huge_play_lets_the_corner_fall_in(self):
        reason = pedestal.check(defaults(pedestal_shape=1, cavity_clearance=2.0))
        self.assertIn("fall inside", reason)

    def test_zero_play_is_a_tight_fit_not_a_mistake(self):
        self.assertIsNone(pedestal.check(defaults(cavity_clearance=0.0)))
        self.assertIsNone(pedestal.check(defaults(pedestal_shape=1, cavity_clearance=0.0)))


class TestLegacyLegIgnored(unittest.TestCase):
    def test_a_zero_or_tiny_chip_leg_refuses_nothing(self):
        # chip_leg is an unused legacy parameter now: the old leg rules are gone.
        self.assertIsNone(pedestal.check(defaults(chip_leg=0.0)))
        self.assertIsNone(pedestal.check(defaults(chip_leg=0.2, cavity_clearance=0.1)))


class TestWhatIsNotChecked(unittest.TestCase):
    def test_a_bare_board_recess_is_checked_like_a_die_boards(self):
        # the recess under a bare board's solid copper must still be a buildable shape
        reason = pedestal.check(defaults(no_chip=1.0, pedestal_size=9.0, pedestal_corner_radius=0.0))
        self.assertIn("pedestal_size", reason)
        self.assertIsNone(pedestal.check(defaults(no_chip=1.0, pedestal_size=6.2,
                                                  pedestal_corner_radius=1.0)))

    def test_no_pedestal_depth_means_no_recess(self):
        self.assertIsNone(pedestal.check(defaults(pedestal_depth=0.0, pedestal_size=9.0)))

    def test_the_layout_check_refuses_an_oversized_radius(self):
        reason = constraints.check_layout(defaults(pedestal_corner_radius=3.2))
        self.assertIsNotNone(reason)
        self.assertIn("less than half pedestal_size", reason)


if __name__ == "__main__":
    unittest.main()
