"""Tests for the pedestal recess (design/pedestal.py): its two shapes, the size
limit, the square's corner radius, and the die-fall check.

What is pinned: the shape, size and radius are declared and read from the
equations file and the form; the size cannot exceed the 6.25 mm chip; the
square's radius must be above 0 and below half its side; the die's corners
must stay over the rim at the largest play, per shape; a legacy chip_leg does
not refuse anything; and a bare board or a recess-free combination is left
alone.

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
    def test_the_default_file_carries_the_size_shape_and_radius(self):
        values = defaults()
        self.assertEqual(values["pedestal_size"], 6.25)
        self.assertEqual(values["pedestal_shape"], 0)
        self.assertEqual(values["pedestal_corner_radius"], 1.0)

    def test_the_new_parameters_are_labelled_and_in_the_form(self):
        names = [n for group in parameter_meta.GROUPS for n in group["names"]]
        for name in ("pedestal_size", "pedestal_shape", "pedestal_corner_radius"):
            self.assertIn(name, parameter_meta.LABELS)
            self.assertIn(name, names)

    def test_the_shape_is_an_integer_not_a_boolean(self):
        self.assertIn("pedestal_shape", parameter_meta.INTEGER_PARAMETERS)
        self.assertNotIn("pedestal_shape", parameter_meta.BOOLEAN_PARAMETERS)

    def test_the_defaults_pass(self):
        self.assertIsNone(pedestal.check(defaults()))


class TestShapeAndSize(unittest.TestCase):
    def test_a_shape_other_than_0_or_1_is_refused(self):
        self.assertIn("0 (square) or 1 (circle)", pedestal.check(defaults(pedestal_shape=2)))

    def test_the_circle_at_the_chip_size_passes(self):
        self.assertIsNone(pedestal.check(defaults(pedestal_shape=1)))

    def test_a_size_bigger_than_the_chip_is_refused(self):
        # A recess bigger than the 6.25 mm die would let it fall in.
        reason = pedestal.check(defaults(pedestal_size=6.3))
        self.assertIn("bigger than the 6.25 mm chip", reason)
        self.assertIn("reduce pedestal_size", reason)

    def test_a_size_bigger_than_the_chip_is_refused_for_the_circle_too(self):
        self.assertIn("bigger than", pedestal.check(defaults(pedestal_shape=1, pedestal_size=6.3)))

    def test_a_smaller_size_passes(self):
        self.assertIsNone(pedestal.check(defaults(pedestal_size=6.0)))

    def test_a_zero_or_negative_size_is_refused(self):
        self.assertIn("must be positive", pedestal.check(defaults(pedestal_size=0.0)))
        self.assertIn("must be positive", pedestal.check(defaults(pedestal_size=-1.0)))


class TestSquareRadius(unittest.TestCase):
    def test_a_zero_radius_is_refused(self):
        # A sketch fillet cannot be zero, and SolidWorks would keep its last radius.
        self.assertIn("must be above 0", pedestal.check(defaults(pedestal_corner_radius=0.0)))

    def test_a_negative_radius_is_refused(self):
        self.assertIn("must be above 0", pedestal.check(defaults(pedestal_corner_radius=-0.1)))

    def test_a_radius_of_half_the_side_is_refused(self):
        # At half the side the straight walls of the square vanish.
        reason = pedestal.check(defaults(pedestal_corner_radius=3.125))
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
        # The die's corner at 3.025 mm from centre stays outside the 1 mm fillet.
        self.assertIsNone(pedestal.check(defaults(cavity_clearance=0.1)))

    def test_a_square_with_a_big_play_lets_the_corner_drop(self):
        # At 0.3 mm play the die's corner comes in past the fillet.
        reason = pedestal.check(defaults(cavity_clearance=0.3))
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
    def test_a_bare_board_has_no_recess_to_check(self):
        self.assertIsNone(pedestal.check(defaults(no_chip=1.0, pedestal_size=9.0,
                                                  pedestal_corner_radius=0.0)))

    def test_no_pedestal_depth_means_no_recess(self):
        self.assertIsNone(pedestal.check(defaults(pedestal_depth=0.0, pedestal_size=9.0)))

    def test_the_layout_check_refuses_an_oversized_radius(self):
        reason = constraints.check_layout(defaults(pedestal_corner_radius=3.2))
        self.assertIsNotNone(reason)
        self.assertIn("less than half pedestal_size", reason)


if __name__ == "__main__":
    unittest.main()
