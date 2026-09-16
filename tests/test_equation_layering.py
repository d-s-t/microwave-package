"""Tests for laying a run's values over the equations file.

This is what lets Altium and SolidWorks work at the same time: each one
works out the combination it is on from the file's original text, in
memory, instead of reading the shared file on disk -- which the other
one is rewriting for a different combination.

Run: python -m unittest discover -s tests
"""
import unittest

from microwave_package.design import equations as eq

FILE = '\n'.join([
    '"wall_width"= 2.0',
    '"pedestal_depth"= 1.5   \' how deep the pedestal sits',
    '"box_width"= "wall_width" + "pedestal_depth"',
])


class TestWithValues(unittest.TestCase):
    def test_a_later_layer_wins(self):
        """The run's pinned values go over the file, and the
        combination's own values over those."""
        content = eq.with_values(FILE, {"wall_width": 3.0}, {"wall_width": 4.0})
        self.assertEqual(eq.get_numeric_parameters(content)["wall_width"], 4.0)

    def test_every_layer_is_applied(self):
        content = eq.with_values(FILE, {"wall_width": 3.0}, {"pedestal_depth": 0.5})
        values = eq.get_numeric_parameters(content)
        self.assertEqual(values["wall_width"], 3.0)
        self.assertEqual(values["pedestal_depth"], 0.5)

    def test_the_original_is_not_touched(self):
        """Two stages work from the same original string at the same
        time, so this has to be a copy every time."""
        eq.with_values(FILE, {"wall_width": 9.0})
        self.assertEqual(eq.get_numeric_parameters(FILE)["wall_width"], 2.0)

    def test_no_layers_means_no_change(self):
        self.assertEqual(eq.with_values(FILE), FILE)

    def test_an_empty_or_missing_layer_is_skipped(self):
        self.assertEqual(eq.with_values(FILE, {}, None), FILE)

    def test_comments_and_formulas_survive(self):
        content = eq.with_values(FILE, {"pedestal_depth": 0.25})
        self.assertIn("' how deep the pedestal sits", content)
        self.assertIn('"box_width"= "wall_width" + "pedestal_depth"', content)

    def test_a_formula_cannot_be_set(self):
        """Derived values are SolidWorks' to recompute, not ours to
        overwrite -- and finding that out per combination is better than
        building geometry from a number nothing agrees with."""
        with self.assertRaises(ValueError):
            eq.with_values(FILE, {"box_width": 7.0})

    def test_an_unknown_parameter_says_so(self):
        with self.assertRaises(KeyError):
            eq.with_values(FILE, {"not_a_parameter": 1.0})


if __name__ == "__main__":
    unittest.main()
