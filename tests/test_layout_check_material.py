"""The layout check solves the CPW gap, so it has to use the run's PCB
material: with TMM 10i hard-wired it refused every 0.4 / 0.5 mm trace on
5880 ("no 50 ohm geometry ... eps_r=9.8") although the generator, which does
use the material, could draw them."""
import unittest
from unittest import mock

from microwave_package import paths
from microwave_package.design import altium_generation, constraints, equations as eq

TMM, DUROID = "Rogers TMM 10i (tm)", "Rogers RT/duroid 5880 (tm)"


def values(trace):
    base = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    return {**base, "cpw_trace_width": trace, "no_chip": 1.0, "pedestal_depth": 0.0}


class TestLayoutCheckUsesTheMaterial(unittest.TestCase):
    def test_a_wide_trace_is_refused_on_tmm_and_buildable_on_5880(self):
        # under the default lid a 0.5 mm trace cannot reach 50 ohm on TMM 10i at any gap
        self.assertIn("50 ohm", constraints.check_layout(values(0.5), TMM))
        self.assertIsNone(constraints.check_layout(values(0.5), DUROID))

    def test_without_a_material_it_is_the_old_default(self):
        self.assertEqual(constraints.check_layout(values(0.5)), constraints.check_layout(values(0.5), TMM))
        self.assertIsNone(constraints.check_layout(values(0.2)))

    def test_the_generator_asks_with_the_runs_material(self):
        seen = []

        def spy(vals, mat_pcb=None, mat_chip=None):
            seen.append(mat_pcb)
            return "stop here"

        with mock.patch.object(constraints, "check_layout", spy):
            ok, why = altium_generation.generate_for_combo(
                "nowhere", values(0.5), materials={"mat_pcb": DUROID})
        self.assertFalse(ok)
        self.assertEqual(seen, [DUROID])


if __name__ == "__main__":
    unittest.main()
