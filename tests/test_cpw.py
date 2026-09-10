"""Tests for the 50 ohm CPW solver.

Run: python -m unittest discover -s tests
"""
import math
import unittest

from microwave_package.design import cpw


class TestEllipticIntegral(unittest.TestCase):
    """K(k) underpins every impedance number, so it is pinned first."""

    def test_known_values(self):
        # Abramowitz & Stegun table values.
        self.assertAlmostEqual(cpw.complete_elliptic_k(0.0), math.pi / 2, places=12)
        self.assertAlmostEqual(cpw.complete_elliptic_k(0.5), 1.6857503548, places=9)
        self.assertAlmostEqual(cpw.complete_elliptic_k(0.9), 2.2805491384, places=9)

    def test_rejects_out_of_range(self):
        for bad in (-0.1, 1.0, 1.5):
            with self.assertRaises(ValueError):
                cpw.complete_elliptic_k(bad)


class TestZ0(unittest.TestCase):
    def test_thin_film_chip_geometry_is_50_ohm(self):
        """10 um trace / 6 um gap on silicon is a standard published 50 ohm
        thin-film CPW -- the strongest independent check available."""
        z0 = cpw.z0_gcpw(trace_width=0.010, gap=0.006, substrate_height=0.381, eps_r=11.9)
        self.assertAlmostEqual(z0, 50.0, delta=0.5)

    def test_impedance_rises_with_gap(self):
        """Monotonicity is what makes the bisection in solve_gap_for_z0 valid."""
        z = [cpw.z0_gcpw(0.2, g, 0.381, 9.8) for g in (0.05, 0.1, 0.2, 0.4, 0.8, 1.6)]
        self.assertEqual(z, sorted(z))

    def test_wide_trace_approaches_microstrip(self):
        """With the slots opened right up, the coplanar grounds stop
        mattering and the line is just microstrip over the backing plane."""
        z0 = cpw.z0_gcpw(0.5, 50.0, 0.381, 9.8)
        self.assertTrue(40.0 < z0 < 55.0, f"expected a microstrip-like value, got {z0}")

    def test_rejects_nonpositive_dimensions(self):
        for args in ((0.0, 0.1, 0.381, 9.8), (0.2, 0.0, 0.381, 9.8), (0.2, 0.1, -1.0, 9.8)):
            with self.assertRaises(ValueError):
                cpw.z0_gcpw(*args)


class TestSolveGap(unittest.TestCase):
    def test_solution_round_trips(self):
        """Whatever the solver returns must reproduce 50 ohm when fed back."""
        for eps_r, widths in ((9.8, (0.15, 0.2, 0.3)), (11.9, (0.05, 0.1, 0.2))):
            for w in widths:
                gap = cpw.solve_gap_for_z0(w, 0.381, eps_r)
                z0 = cpw.z0_gcpw(w, gap, 0.381, eps_r)
                self.assertAlmostEqual(z0, 50.0, places=6, msg=f"w={w}, eps_r={eps_r}")

    def test_denser_substrate_needs_a_wider_gap(self):
        """A higher permittivity means more capacitance per unit length and
        so a lower impedance for a given geometry -- the slots have to open
        further to climb back to 50 ohm."""
        rogers = cpw.solve_gap_for_z0(0.2, 0.381, 9.8)
        silicon = cpw.solve_gap_for_z0(0.2, 0.381, 11.9)
        self.assertGreater(silicon, rogers)

    def test_unreachable_target_is_refused_with_a_useful_message(self):
        """A trace that wide is microstrip-dominated and saturates below
        50 ohm -- the solver must say so rather than return a bad gap."""
        with self.assertRaises(ValueError) as ctx:
            cpw.solve_gap_for_z0(0.5, 0.381, 9.8)
        self.assertIn("unreachable", str(ctx.exception))
        self.assertIn("trace width", str(ctx.exception))

    def test_non_default_target_impedance(self):
        gap = cpw.solve_gap_for_z0(0.2, 0.381, 9.8, z0=75.0)
        self.assertAlmostEqual(cpw.z0_gcpw(0.2, gap, 0.381, 9.8), 75.0, places=6)


class TestConductorThickness(unittest.TestCase):
    """35 um of copper across a ~100 um gap is not a perturbation, so the
    finite-thickness correction has to be right."""

    def test_zero_thickness_is_the_ideal_case(self):
        ideal = cpw.z0_gcpw(0.2, 0.1, 0.381, 9.8)
        explicit = cpw.z0_gcpw(0.2, 0.1, 0.381, 9.8, conductor_thickness=0.0)
        self.assertEqual(ideal, explicit)

    def test_correction_matches_the_closed_form(self):
        # delta = (1.25t/pi) * (1 + ln(4 pi w / t)); w=0.2, t=0.035 mm.
        self.assertAlmostEqual(cpw.thickness_correction(0.2, 0.035), 0.0734459, places=6)
        self.assertEqual(cpw.thickness_correction(0.2, 0.0), 0.0)

    def test_thicker_metal_lowers_impedance(self):
        """Thick metal widens the trace and narrows the slot, raising the
        capacitance -- so the same drawn geometry measures lower."""
        thin = cpw.z0_gcpw(0.2, 0.3, 0.381, 9.8, conductor_thickness=0.0)
        thick = cpw.z0_gcpw(0.2, 0.3, 0.381, 9.8, conductor_thickness=0.035)
        self.assertLess(thick, thin)

    def test_thicker_metal_needs_a_wider_gap_for_50_ohm(self):
        ideal = cpw.solve_gap_for_z0(0.2, 0.381, 9.8, conductor_thickness=0.0)
        real = cpw.solve_gap_for_z0(0.2, 0.381, 9.8, conductor_thickness=0.035)
        self.assertGreater(real, ideal)

    def test_ignoring_thickness_would_badly_miss_50_ohm(self):
        """The regression this correction exists to prevent: build the
        zero-thickness gap in 1 oz copper and the line is nowhere near
        matched."""
        ideal_gap = cpw.solve_gap_for_z0(0.2, 0.381, 9.8, conductor_thickness=0.0)
        actual = cpw.z0_gcpw(0.2, ideal_gap, 0.381, 9.8, conductor_thickness=0.035)
        self.assertLess(actual, 40.0)

    def test_solution_round_trips_with_thickness(self):
        gap = cpw.solve_gap_for_z0(0.2, 0.381, 9.8, conductor_thickness=0.035)
        z0 = cpw.z0_gcpw(0.2, gap, 0.381, 9.8, conductor_thickness=0.035)
        self.assertAlmostEqual(z0, 50.0, places=6)

    def test_thin_film_chip_metal_barely_matters(self):
        """200 nm on the chip should move the gap by ~1 um, not more."""
        ideal = cpw.solve_gap_for_z0(0.1, 0.381, 11.9, conductor_thickness=0.0)
        real = cpw.solve_gap_for_z0(0.1, 0.381, 11.9, conductor_thickness=0.0002)
        self.assertLess(abs(real - ideal), 0.005)

    def test_metal_thicker_than_the_gap_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            cpw.z0_gcpw(0.2, 0.02, 0.381, 9.8, conductor_thickness=0.035)
        self.assertIn("closes the slot", str(ctx.exception))

    def test_negative_thickness_is_refused(self):
        with self.assertRaises(ValueError):
            cpw.z0_gcpw(0.2, 0.1, 0.381, 9.8, conductor_thickness=-0.01)


class TestPermittivityLookup(unittest.TestCase):
    def test_known_materials_match_the_hfss_names(self):
        self.assertEqual(cpw.permittivity_of("Rogers TMM 10i (tm)"), 9.8)
        self.assertEqual(cpw.permittivity_of("silicon"), 11.9)

    def test_unknown_material_names_itself_in_the_error(self):
        with self.assertRaises(KeyError) as ctx:
            cpw.permittivity_of("unobtainium")
        self.assertIn("unobtainium", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
