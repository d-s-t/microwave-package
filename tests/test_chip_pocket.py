"""The lid's chip window (the 7.5 mm window in the plug): its corner radius is a
declared parameter, labelled and in the form, and the four corners add
(1 - pi/4) R^2 x depth each, with the depth the window cut's 0.4 mm.

Run: python -m unittest tests.test_chip_pocket
"""
import math
import unittest

from microwave_package import paths
from microwave_package.design import equations as eq, parameter_meta


class TestChipPocketRadius(unittest.TestCase):
    def test_the_default_is_one_millimetre(self):
        values = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
        self.assertEqual(values["chip_pocket_radius"], 1.0)

    def test_it_is_labelled_and_in_the_form(self):
        names = [n for group in parameter_meta.GROUPS for n in group["names"]]
        self.assertIn("chip_pocket_radius", parameter_meta.LABELS)
        self.assertIn("chip_pocket_radius", names)

    def test_four_corners_at_the_window_depth_add_the_expected_material(self):
        # Checked in SolidWorks, 2026-10-20: +0.3434 mm3 with the window cut 0.4 mm deep.
        values = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
        depth = values["bond_height"] * 2
        added = 4 * (1 - math.pi / 4) * values["chip_pocket_radius"] ** 2 * depth
        self.assertAlmostEqual(added, 0.3434, places=4)


if __name__ == "__main__":
    unittest.main()
