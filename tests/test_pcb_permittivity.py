"""Every PCB material the new-run form offers must have a permittivity, or the
CPW gap cannot be solved and the run fails before anything is built (it did,
for 5880, 6010 and FR4)."""
import glob
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

    def test_the_cte_matched_boards_are_offered_with_the_library_permittivity(self):
        self.assertIn("Rogers RO3003 (tm)", model_builder.MATERIAL_OPTIONS["mat_pcb"])
        self.assertIn("Rogers RO4003 (tm)", model_builder.MATERIAL_OPTIONS["mat_pcb"])
        self.assertEqual(cpw.permittivity_of("Rogers RO3003 (tm)"), 3.0)
        self.assertEqual(cpw.permittivity_of("Rogers RO4003 (tm)"), 3.55)

    def test_the_gap_orders_with_permittivity_across_the_rogers_boards(self):
        order = ("Rogers RT/duroid 5880 (tm)", "Rogers RO3003 (tm)",
                 "Rogers RO4003 (tm)", "Rogers TMM 10i (tm)")
        gaps = [cpw.solve_gap_for_z0(0.4, 0.381, cpw.permittivity_of(m), conductor_thickness=0.035)
                for m in order]
        self.assertEqual(gaps, sorted(gaps), dict(zip(order, gaps)))

    def test_every_offered_pcb_material_is_in_the_installed_aedt_library(self):
        """A name the library lacks is not an error to HFSS: the body is quietly
        given the last-resort dielectric (TMM 10i) instead. Checked against the
        library file when AEDT is installed here, skipped when it is not."""
        libraries = glob.glob(r"C:\Program Files\ANSYS Inc\v*\AnsysEM\syslib\Materials.amat")
        if not libraries:
            self.skipTest("no AEDT material library on this machine")
        with open(sorted(libraries)[-1], encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        for material in model_builder.MATERIAL_OPTIONS["mat_pcb"]:
            if material in model_builder.MISSING_FROM_LIBRARY:
                continue
            self.assertIn(f"$begin '{material}'", text, material)


if __name__ == "__main__":
    unittest.main()
