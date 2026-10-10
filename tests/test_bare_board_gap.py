"""A bare board has no chip, so its layout must not need a chip CPW gap; and a
gap that cannot be solved must fail that combination, not end the sweep."""
import unittest

from microwave_package import paths
from microwave_package.design import altium_generation, equations as eq

DUROID = "Rogers RT/duroid 5880 (tm)"


def values(trace, no_chip):
    base = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    return {**base, "cpw_trace_width": trace, "no_chip": float(no_chip), "pedestal_depth": 0.0}


class TestBareBoardGap(unittest.TestCase):
    def test_a_trace_the_board_carries_but_the_chip_cannot_is_fine_on_a_bare_board(self):
        params = altium_generation.build_params(values(0.5, True), mat_pcb=DUROID)
        self.assertEqual(params["chip_cpw_gap"], params["pcb_cpw_gap"])

    def test_the_same_trace_still_fails_with_a_chip(self):
        with self.assertRaisesRegex(ValueError, "unreachable"):
            altium_generation.build_params(values(0.5, False), mat_pcb=DUROID)

    def test_a_gap_that_cannot_be_solved_fails_the_combination_not_the_run(self):
        ok, why = altium_generation.generate_for_combo(
            "nowhere", values(0.5, False), materials={"mat_pcb": DUROID})
        self.assertFalse(ok)
        self.assertIn("layout cannot be built", why)


if __name__ == "__main__":
    unittest.main()
