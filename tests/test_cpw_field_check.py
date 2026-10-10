"""The closed-form CPW impedance against a 2-D field solve of the real cross-section.

`cpw.py` is conformal mapping plus two fitted terms (the sidewalls of thick
copper, and a ceiling for the lid). Everything it claims is checked here against
`base_parts/cpw_fd_check.py`, which solves the cross-section directly -- copper as
a solid, the substrate, the lid with its channel walls. The tolerance is 3 %: the
fit is 0.9 % rms over a wide sweep and 5.7 % at worst, for the shallow covers
(0.1 mm and under) that no 50 ohm line uses.

Needs numpy and scipy, which the project does not depend on; skipped without them.
"""
import importlib.util
import os
import unittest

try:
    import numpy  # noqa: F401
    import scipy  # noqa: F401
    HAVE_SOLVER = True
except ImportError:
    HAVE_SOLVER = False

from microwave_package.design import cpw

CHECK = os.path.join(os.path.dirname(__file__), "..", "base_parts", "cpw_fd_check.py")
H, T = 0.381, 0.035


def field_solver():
    spec = importlib.util.spec_from_file_location("cpw_fd_check", CHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(HAVE_SOLVER, "needs numpy and scipy")
class TestAgainstTheFieldSolve(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fd = field_solver()

    def z_field(self, eps_r, trace, gap, channel, relief):
        return self.fd.characteristic_impedance(trace, gap, H, eps_r, T, channel, relief)[0]

    def test_the_solver_itself_matches_the_closed_form_for_a_thin_open_line(self):
        """The thin-metal limit, where conformal mapping is exact: 0.5 um of metal, 0.2 mm trace,
        0.15 mm gaps on TMM 10i, nothing over it (the field solve to within 1 %)."""
        z, eps_eff = self.fd.characteristic_impedance(0.2, 0.15, H, 9.8, 0.0005, 8.0, 8.0, half_width=8.0)
        self.assertAlmostEqual(z, cpw.z0_gcpw(0.2, 0.15, H, 9.8), delta=0.01 * z)
        self.assertAlmostEqual(z, cpw.z0_gcpw(0.2, 0.15, H, 9.8, 0.0005), delta=0.01 * z)

    def test_the_covered_candidate_board(self):
        """5880, 0.4 mm trace, 1.0 mm channel 200 um deep: at the solved 94 um gap, and at the
        old 111 um gap."""
        for gap in (0.0943, 0.111):
            field = self.z_field(2.2, 0.4, gap, 1.0, 0.2)
            formula = cpw.z0_gcpw(0.4, gap, H, 2.2, T, cover_height=0.2)
            self.assertAlmostEqual(formula, field, delta=0.03 * field, msg=gap)

    def test_the_default_board(self):
        """TMM 10i, 0.2 mm trace, the default 1.2 mm / 300 um lid, at the solved gap."""
        gap = cpw.solve_gap_for_z0(0.2, H, 9.8, conductor_thickness=T, cover_height=0.3, channel_width=1.2)
        field = self.z_field(9.8, 0.2, gap, 1.2, 0.3)
        self.assertAlmostEqual(field, 50.0, delta=0.03 * 50.0)

    def test_other_boards_and_covers(self):
        for eps_r, trace, gap, channel, relief in (
            (2.2, 0.5, 0.127, 1.0, 0.3), (4.4, 0.3, 0.15, 1.2, 0.2), (10.2, 0.15, 0.3, 2.0, 0.5),
            (2.2, 0.4, 0.111, 3.0, 3.0), (9.8, 0.2, 0.271, 1.0, 0.2),
        ):
            field = self.z_field(eps_r, trace, gap, channel, relief)
            formula = cpw.z0_gcpw(trace, gap, H, eps_r, T, cover_height=relief)
            self.assertAlmostEqual(formula, field, delta=0.03 * field, msg=(eps_r, trace, gap))

    def test_the_old_open_top_solve_is_a_50_ohm_line_only_on_paper(self):
        """The 111 um gap the previous solve gave a 0.4 mm trace on 5880 is a 62 ohm line with
        its thick copper and nothing over it; the field solve says so as well."""
        field = self.z_field(2.2, 0.4, 0.111, 12.0, 12.0)
        self.assertTrue(58.0 < field < 66.0, field)
        self.assertAlmostEqual(cpw.z0_gcpw(0.4, 0.111, H, 2.2, T), field, delta=0.03 * field)

    def test_walls_that_stand_over_the_slots_are_refused_for_a_reason(self):
        """A channel narrower than the CPW drops the field solve's impedance by about a fifth
        at a depth where the plane model sees no difference."""
        clear = self.z_field(2.2, 0.4, 0.111, 1.0, 0.3)
        over_the_slots = self.z_field(2.2, 0.4, 0.111, 0.5, 0.3)
        self.assertLess(over_the_slots, 0.85 * clear)


if __name__ == "__main__":
    unittest.main()
