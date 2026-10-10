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
    """35 um of copper across a ~100 um gap is not a perturbation: the metal's
    sidewalls face each other across the slot and add their own capacitance.
    It is the parallel-plate term t/gap, with a fitted fringe on top, and it
    belongs to the air side of the line only."""

    def test_zero_thickness_is_the_ideal_case(self):
        ideal = cpw.z0_gcpw(0.2, 0.1, 0.381, 9.8)
        explicit = cpw.z0_gcpw(0.2, 0.1, 0.381, 9.8, conductor_thickness=0.0)
        self.assertEqual(ideal, explicit)
        self.assertEqual(cpw.sidewall_ratio(0.1, 0.0), 0.0)

    def test_sidewall_term_is_the_parallel_plate_field_plus_a_fringe(self):
        # t/gap, plus 0.09 * (1 - exp(-t / 10 um)); 35 um across a 100 um slot.
        self.assertAlmostEqual(cpw.sidewall_ratio(0.1, 0.035),
                               0.35 + 0.09 * (1.0 - math.exp(-3.5)), places=9)
        # Thick metal on a narrow slot is nothing but two plates: the fringe is a
        # constant and t/gap grows without bound.
        self.assertAlmostEqual(cpw.sidewall_ratio(0.1, 1.0), 10.0 + 0.09, places=4)

    def test_the_fringe_dies_away_with_the_metal(self):
        """The chip's 200 nm film is drawn as a sheet: it has no edge to fringe at."""
        self.assertLess(cpw.sidewall_ratio(0.006, 0.0002) - 0.0002 / 0.006, 0.003)

    def test_thicker_metal_lowers_impedance(self):
        """The sidewalls add capacitance, so the same drawn geometry measures lower."""
        thin = cpw.z0_gcpw(0.2, 0.3, 0.381, 9.8, conductor_thickness=0.0)
        thick = cpw.z0_gcpw(0.2, 0.3, 0.381, 9.8, conductor_thickness=0.035)
        self.assertLess(thick, thin)

    def test_thicker_metal_needs_a_wider_gap_for_50_ohm(self):
        ideal = cpw.solve_gap_for_z0(0.2, 0.381, 9.8, conductor_thickness=0.0)
        real = cpw.solve_gap_for_z0(0.2, 0.381, 9.8, conductor_thickness=0.035)
        self.assertGreater(real, ideal * 0.9)
        self.assertLess(cpw.z0_gcpw(0.2, ideal, 0.381, 9.8, conductor_thickness=0.035),
                        cpw.z0_gcpw(0.2, ideal, 0.381, 9.8))

    def test_ignoring_thickness_would_miss_50_ohm(self):
        """The regression this term exists to prevent: build the zero-thickness
        gap in 1 oz copper and the line comes out 12 % low. (A 2-D field solve of
        the same cross-section, base_parts/cpw_fd_check.py, agrees; the formula
        this replaced said 40 ohm here and asked for a gap that was too wide.)"""
        ideal_gap = cpw.solve_gap_for_z0(0.2, 0.381, 9.8, conductor_thickness=0.0)
        actual = cpw.z0_gcpw(0.2, ideal_gap, 0.381, 9.8, conductor_thickness=0.035)
        self.assertTrue(42.0 < actual < 46.0, actual)

    def test_the_sidewall_field_is_in_the_air_so_it_does_not_scale_with_eps_r(self):
        """What the metal adds to the capacitance is the same on a 2.2 board and a 9.8
        one (in air-side units), so the impedance falls by a similar fraction on both.
        A shift of the trace edges, as this module used to make, falls 2-3 times as
        far on the low-permittivity board than the field solve says it does."""
        fall = {}
        for eps_r in (2.2, 9.8):
            fall[eps_r] = 1.0 - (cpw.z0_gcpw(0.4, 0.15, 0.381, eps_r, 0.035)
                                 / cpw.z0_gcpw(0.4, 0.15, 0.381, eps_r, 0.0))
        self.assertTrue(0.05 < fall[9.8] < 0.15 and 0.05 < fall[2.2] < 0.15, fall)
        self.assertLess(abs(fall[2.2] - fall[9.8]), 0.05)
        self.assertGreater(fall[2.2], fall[9.8])    # more of a 2.2 board's capacitance is in the air

    def test_solution_round_trips_with_thickness(self):
        gap = cpw.solve_gap_for_z0(0.2, 0.381, 9.8, conductor_thickness=0.035)
        z0 = cpw.z0_gcpw(0.2, gap, 0.381, 9.8, conductor_thickness=0.035)
        self.assertAlmostEqual(z0, 50.0, places=6)

    def test_thin_film_chip_metal_barely_matters(self):
        """200 nm on the chip should move the gap by ~1 um, not more."""
        ideal = cpw.solve_gap_for_z0(0.1, 0.381, 11.9, conductor_thickness=0.0)
        real = cpw.solve_gap_for_z0(0.1, 0.381, 11.9, conductor_thickness=0.0002)
        self.assertLess(abs(real - ideal), 0.005)

    def test_metal_thicker_than_the_gap_is_no_longer_refused(self):
        """The formula this replaced closed the slot when the metal was thicker
        than its correction allowed. The sidewall term has no such limit (it
        is the parallel-plate field, which only gets stronger): a 35 um film on
        a 20 um slot is simply a low impedance."""
        thin = cpw.z0_gcpw(0.2, 0.02, 0.381, 9.8, conductor_thickness=0.0)
        thick = cpw.z0_gcpw(0.2, 0.02, 0.381, 9.8, conductor_thickness=0.035)
        self.assertTrue(0.0 < thick < thin)

    def test_negative_thickness_is_refused(self):
        with self.assertRaises(ValueError):
            cpw.z0_gcpw(0.2, 0.1, 0.381, 9.8, conductor_thickness=-0.01)


class TestCover(unittest.TestCase):
    """The lid sits on the board's copper, so the line has a metal ceiling
    `cover_height` over the trace: the line that is actually built."""

    def test_no_cover_is_the_open_top_line(self):
        explicit = cpw.z0_gcpw(0.4, 0.111, 0.381, 2.2, 0.035, cover_height=None)
        self.assertEqual(cpw.z0_gcpw(0.4, 0.111, 0.381, 2.2, 0.035), explicit)

    def test_a_high_cover_reduces_to_the_open_line(self):
        for eps_r, trace, gap in ((2.2, 0.4, 0.111), (9.8, 0.2, 0.27), (4.4, 0.3, 0.15)):
            open_top = cpw.z0_gcpw(trace, gap, 0.381, eps_r, 0.035)
            very_high = cpw.z0_gcpw(trace, gap, 0.381, eps_r, 0.035, cover_height=1.0e5)
            self.assertAlmostEqual(very_high, open_top, places=4, msg=(eps_r, trace))

    def test_a_lower_cover_gives_a_lower_impedance(self):
        heights = (3.0, 1.0, 0.5, 0.3, 0.2, 0.1, 0.05)
        for eps_r in (2.2, 9.8):
            z = [cpw.z0_gcpw(0.3, 0.15, 0.381, eps_r, 0.035, cover_height=h) for h in heights]
            self.assertEqual(z, sorted(z, reverse=True), eps_r)
            self.assertLess(z[0], cpw.z0_gcpw(0.3, 0.15, 0.381, eps_r, 0.035))

    def test_the_cover_matters_more_on_a_low_permittivity_board(self):
        """On 5880 the air above the line is most of its capacitance, on TMM 10i a fifth:
        the same cover costs the 5880 line more of its impedance."""
        drop = {}
        for eps_r in (2.2, 9.8):
            open_top = cpw.z0_gcpw(0.3, 0.15, 0.381, eps_r, 0.035)
            covered = cpw.z0_gcpw(0.3, 0.15, 0.381, eps_r, 0.035, cover_height=0.2)
            drop[eps_r] = 1.0 - covered / open_top
        self.assertGreater(drop[2.2], drop[9.8])

    def test_a_cover_close_enough_to_short_the_line_gives_a_low_impedance_not_an_error(self):
        z = cpw.z0_gcpw(0.4, 0.111, 0.381, 2.2, 0.035, cover_height=1.0e-9)
        self.assertTrue(0.0 < z < 20.0, z)

    def test_nonpositive_cover_is_refused(self):
        for bad in (0.0, -0.1):
            with self.assertRaises(ValueError):
                cpw.z0_gcpw(0.4, 0.111, 0.381, 2.2, 0.035, cover_height=bad)

    def test_impedance_still_rises_with_the_gap_under_a_cover(self):
        """Monotonicity is what keeps the bisection valid."""
        z = [cpw.z0_gcpw(0.4, g, 0.381, 2.2, 0.035, cover_height=0.2) for g in (0.03, 0.06, 0.1, 0.2, 0.4, 0.8)]
        self.assertEqual(z, sorted(z))


class TestSolveUnderACover(unittest.TestCase):
    CANDIDATE = dict(trace_width=0.4, substrate_height=0.381, eps_r=2.2, conductor_thickness=0.035)

    def test_the_candidate_gap_is_sane(self):
        """RT/duroid 5880, 0.4 mm trace, a 1.0 mm channel 200 um deep -- the board the box study
        found by trial to be clean to 20 GHz. The old solve gave 111 um (open top, and a thickness
        formula that overcorrected); this line needs about 94 um."""
        gap = cpw.solve_gap_for_z0(**self.CANDIDATE, cover_height=0.2, channel_width=1.0)
        self.assertAlmostEqual(gap, 0.0943, delta=0.0005)
        self.assertAlmostEqual(
            cpw.z0_gcpw(0.4, gap, 0.381, 2.2, 0.035, cover_height=0.2), 50.0, places=6)

    def test_a_lower_cover_needs_a_wider_gap(self):
        gaps = [cpw.solve_gap_for_z0(**self.CANDIDATE, cover_height=h) for h in (1.0, 0.5, 0.3, 0.2, 0.15)]
        self.assertEqual(gaps, sorted(gaps))

    def test_a_high_cover_gives_the_open_gap(self):
        open_gap = cpw.solve_gap_for_z0(**self.CANDIDATE)
        self.assertAlmostEqual(cpw.solve_gap_for_z0(**self.CANDIDATE, cover_height=1.0e5), open_gap, places=6)
        # 57 um open: the old solve's 111 um came from the thickness formula, not from the lid
        self.assertAlmostEqual(open_gap, 0.0567, delta=0.001)

    def test_a_cover_too_close_cannot_reach_50_ohm(self):
        """The default 40 um relief over a 0.4 mm trace on 5880 is a 25 ohm line at any gap."""
        with self.assertRaises(cpw.ReliefChannelError) as ctx:
            cpw.solve_gap_for_z0(**self.CANDIDATE, cover_height=0.04, channel_width=0.5)
        self.assertEqual(ctx.exception.kind, "shallow")
        self.assertIn("unreachable", str(ctx.exception))
        # ... and it says how deep would do
        self.assertTrue(0.10 < ctx.exception.min_height < 0.14, ctx.exception.min_height)
        gap = cpw.solve_gap_for_z0(**self.CANDIDATE, cover_height=ctx.exception.min_height * 1.02)
        self.assertLess(gap, 0.6)

    def test_a_channel_that_does_not_clear_the_cpw_is_refused(self):
        """The 94 um gap makes the CPW 0.589 mm across; the walls need 0.1 mm outside each slot."""
        with self.assertRaises(cpw.ReliefChannelError) as ctx:
            cpw.solve_gap_for_z0(**self.CANDIDATE, cover_height=0.2, channel_width=0.7)
        err = ctx.exception
        self.assertEqual(err.kind, "narrow")
        self.assertAlmostEqual(err.gap, 0.0943, delta=0.0005)
        self.assertAlmostEqual(err.needed_width, 0.4 + 2 * err.gap + 0.2, places=9)

    def test_the_channel_width_limit_is_exactly_trace_plus_two_gaps_plus_two_clearances(self):
        gap = cpw.solve_gap_for_z0(**self.CANDIDATE, cover_height=0.2)
        needed = 0.4 + 2 * gap + 2 * cpw.wall_clearance(0.2)
        self.assertEqual(cpw.solve_gap_for_z0(**self.CANDIDATE, cover_height=0.2, channel_width=needed + 1e-6), gap)
        with self.assertRaises(cpw.ReliefChannelError):
            cpw.solve_gap_for_z0(**self.CANDIDATE, cover_height=0.2, channel_width=needed - 1e-4)

    def test_a_channel_needs_a_depth(self):
        with self.assertRaises(ValueError):
            cpw.solve_gap_for_z0(**self.CANDIDATE, channel_width=1.0)

    def test_walls_stand_a_tenth_of_a_millimetre_out_or_half_the_depth(self):
        self.assertEqual(cpw.wall_clearance(0.04), 0.1)
        self.assertEqual(cpw.wall_clearance(0.2), 0.1)
        self.assertEqual(cpw.wall_clearance(0.5), 0.25)

    def test_the_open_top_unreachable_error_is_still_a_plain_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            cpw.solve_gap_for_z0(0.5, 0.381, 9.8, conductor_thickness=0.0)
        self.assertNotIsInstance(ctx.exception, cpw.ReliefChannelError)

    def test_min_cover_height_is_the_edge_of_what_a_usable_slot_reaches(self):
        height = cpw.min_cover_height(0.4, 0.381, 2.2, conductor_thickness=0.035)
        self.assertGreaterEqual(cpw.z0_gcpw(0.4, 0.6, 0.381, 2.2, 0.035, height), 50.0)
        self.assertLess(cpw.z0_gcpw(0.4, 0.6, 0.381, 2.2, 0.035, height * 0.97), 50.0)


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
