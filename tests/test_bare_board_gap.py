"""A bare board has no chip, so its layout must not need a chip CPW gap; the die's
line has a trace of its own, so a trace the board carries does not have to be one the
die can; and a gap that cannot be solved must fail that combination, not end the sweep."""
import unittest

from microwave_package import paths
from microwave_package.design import altium_generation, cpw, equations as eq

DUROID = "Rogers RT/duroid 5880 (tm)"


def values(trace, no_chip, chip_trace=None, **extra):
    base = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    v = {**base, "cpw_trace_width": trace, "no_chip": float(no_chip), "pedestal_depth": 0.0, **extra}
    if chip_trace is not None:
        v["chip_trace_width"] = chip_trace
    return v


def silicon_gap(trace, base):
    return cpw.solve_chip_gap_for_z0(trace, base["chip_width"], cpw.permittivity_of("silicon"),
                                     conductor_thickness=base["chip_metal_thickness"])


class TestBareBoardGap(unittest.TestCase):
    def test_a_trace_the_board_carries_but_the_chip_cannot_is_fine_on_a_bare_board(self):
        params = altium_generation.build_params(values(0.5, True, chip_trace=0.5), mat_pcb=DUROID)
        self.assertEqual(params["chip_cpw_gap"], params["pcb_cpw_gap"])

    def test_the_die_has_a_trace_of_its_own_so_a_board_trace_it_cannot_carry_is_fine(self):
        """The 5880 board's 0.4-0.5 mm trace is no CPW on a 0.381 mm silicon die."""
        v = values(0.5, False)
        params = altium_generation.build_params(v, mat_pcb=DUROID)
        self.assertEqual(params["chip_trace_width"], 0.2)
        self.assertAlmostEqual(params["chip_cpw_gap"], silicon_gap(0.2, v), places=9)

    def test_a_die_trace_that_is_no_cpw_on_silicon_fails_with_a_chip(self):
        with self.assertRaisesRegex(ValueError, "unreachable"):
            altium_generation.build_params(values(0.5, False, chip_trace=0.5), mat_pcb=DUROID)

    def test_a_gap_that_cannot_be_solved_fails_the_combination_not_the_run(self):
        ok, why = altium_generation.generate_for_combo(
            "nowhere", values(0.5, False, chip_trace=0.5), materials={"mat_pcb": DUROID})
        self.assertFalse(ok)
        self.assertIn("layout cannot be built", why)

    def test_values_with_no_die_trace_use_the_boards_as_runs_from_before_did(self):
        v = {k: x for k, x in values(0.2, False).items() if k != "chip_trace_width"}
        params = altium_generation.build_params(v)
        self.assertEqual(params["chip_trace_width"], 0.2)
        self.assertAlmostEqual(params["chip_cpw_gap"], silicon_gap(0.2, v), places=9)


class TestTheChipBoardRunsOfTheBoxStudy(unittest.TestCase):
    """run_20261010_093710 and run_20261010_102243: RT/duroid 5880, cpw_trace_width 0.4, with
    the chip. The die's gap was solved on silicon for the BOARD's 0.4 mm trace and came back
    as 29 mm, so copper.txt had the die's grounds at |y| from 3 to 29.5 mm -- no ground beside
    the line, and a metal sheet reaching 26 mm past the die into the PCB, lid and walls."""

    CANDIDATE = dict(route_width=1.0, route_clearence=0.2)

    def test_the_die_gets_a_real_cpw(self):
        v = values(0.4, False, **self.CANDIDATE)
        params = altium_generation.build_params(v, mat_pcb=DUROID)
        self.assertLess(params["chip_cpw_gap"], 0.5)
        # 121 um is HFSS's 50 ohm gap for this die (PRE_MANUFACTURING S3); the formula gives 119.
        self.assertAlmostEqual(params["chip_cpw_gap"], 0.121, delta=0.006)
        self.assertAlmostEqual(params["pcb_cpw_gap"], 0.0943, delta=0.0005)

    def test_the_old_behaviour_was_a_29_mm_gap(self):
        gap = cpw.solve_gap_for_z0(0.4, 0.381, 11.9, conductor_thickness=0.0002, gap_bounds=(1e-4, 100.0))
        self.assertTrue(28.0 < gap < 30.0, gap)

    def test_asking_for_the_boards_trace_on_the_die_is_refused_not_solved_to_29_mm(self):
        from unittest import mock
        from microwave_package.design import constraints

        v = values(0.4, False, chip_trace=0.4, **self.CANDIDATE)
        with mock.patch.object(constraints, "MIN_CPW_GAP", 0.05):
            reason = constraints.check_layout(v, DUROID)
        self.assertIn("no 50 ohm CPW for a 0.4 mm trace on the 0.381 mm die", reason)
        self.assertIn("chip_trace_width", reason)


if __name__ == "__main__":
    unittest.main()


class BarePedestalSkipTest(unittest.TestCase):
    def test_bare_board_pedestal_is_skipped_at_creation(self):
        from microwave_package.workflow import run_manager as rm
        self.assertIn("set pedestal_depth to 0", rm._skip_reason({"no_chip": 1.0, "pedestal_depth": 2.0}, True))
        self.assertIsNone(rm.constraints.check_bare_pedestal({"no_chip": 1.0, "pedestal_depth": 0.0}))


class BarePedestalWebWarningTest(unittest.TestCase):
    def test_start_run_warns_for_bare_board_pedestal(self):
        from microwave_package import webapp
        from microwave_package.design.sweep import SweepParameter
        msg = webapp._bare_pedestal_message(
            [SweepParameter(name="pedestal_depth", values=[0.0, 2.0])], {"no_chip": 1.0})
        self.assertIn("Not started", msg)
        self.assertIsNone(webapp._bare_pedestal_message(
            [SweepParameter(name="pedestal_depth", values=[0.0, 2.0])], {}))
        self.assertIsNone(webapp._bare_pedestal_message([], {"no_chip": 1.0, "pedestal_depth": 0.0}))
