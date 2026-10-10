"""Every PCB material the new-run form offers must have a permittivity, or the
CPW gap cannot be solved and the run fails before anything is built (it did,
for 5880, 6010 and FR4)."""
import unittest

from microwave_package.design import cpw
from microwave_package.hfss import model_builder


class TestPcbPermittivity(unittest.TestCase):
    def test_every_offered_pcb_material_has_one(self):
        for material in model_builder.MATERIAL_OPTIONS["mat_pcb"]:
            self.assertGreater(cpw.permittivity_of(material), 1.0, material)

    def test_the_50_ohm_gap_can_be_solved_on_each_for_the_default_trace(self):
        for material in model_builder.MATERIAL_OPTIONS["mat_pcb"]:
            gap = cpw.solve_gap_for_z0(
                0.2, 0.381, cpw.permittivity_of(material), conductor_thickness=0.035)
            self.assertTrue(0.01 < gap < 1.0, (material, gap))

    def test_the_lower_the_permittivity_the_narrower_the_gap(self):
        gaps = {m: cpw.solve_gap_for_z0(0.2, 0.381, cpw.permittivity_of(m), conductor_thickness=0.035)
                for m in ("Rogers RT/duroid 5880 (tm)", "FR4_epoxy", "Rogers TMM 10i (tm)")}
        self.assertLess(gaps["Rogers RT/duroid 5880 (tm)"], gaps["FR4_epoxy"])
        self.assertLess(gaps["FR4_epoxy"], gaps["Rogers TMM 10i (tm)"])

if __name__ == "__main__":
    unittest.main()
