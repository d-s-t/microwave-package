"""Tests for the box's conductor model: perfect E, or finite conductivity.

The box and lid are aluminium, superconducting below ~1.2 K, so perfect E
is the default. With it off they get a finite-conductivity boundary of the
configured (cryogenic) value -- not the library aluminium's room-
temperature one, and without a thickness, which an Eigenmode design
refuses on that boundary.

Run: python -m unittest discover -s tests
"""
import unittest

from microwave_package.hfss import model_builder
from tests.test_material_rules import FakeHfss, SETTINGS


class RecordingHfss(FakeHfss):
    def __init__(self, names, fail=False):
        super().__init__(names)
        self.finite = []
        self.fail = fail

    def assign_finite_conductivity(self, assignment, **kwargs):
        if self.fail:
            raise RuntimeError("no such boundary")
        self.finite.append((assignment, kwargs))


NAMES = ["box", "lid_inside", "lid", "pcb_copper_top"]


def assign(settings, names=NAMES, fail=False):
    hfss = RecordingHfss(names, fail=fail)
    logs = []
    model_builder.assign_materials_and_boundaries(hfss, dict(settings), log=logs.append)
    return hfss, logs


class TestBoxConductorModel(unittest.TestCase):
    def test_perfect_e_stays_the_default_and_gets_no_finite_boundary(self):
        hfss, _ = assign(SETTINGS)
        self.assertEqual(sorted(hfss.perfect_e), sorted(["box", "lid_inside", "lid", "pcb_copper_top"]))
        self.assertEqual(hfss.finite, [])

    def test_the_default_setting_is_perfect_e(self):
        from microwave_package import pipeline
        self.assertTrue(pipeline.DEFAULT_ANALYSIS_SETTINGS["materials"]["perfect_e_box"])

    def test_without_perfect_e_the_box_gets_the_configured_conductivity(self):
        hfss, _ = assign({**SETTINGS, "perfect_e_box": False, "box_conductivity": 2.5e8})
        self.assertNotIn("box", hfss.perfect_e)
        self.assertEqual({name for name, _ in hfss.finite}, {"box", "lid_inside", "lid"})
        for _name, kwargs in hfss.finite:
            self.assertEqual(kwargs["conductivity"], 2.5e8)

    def test_no_thickness_is_asked_for(self):
        """An Eigenmode design refuses one on this boundary."""
        hfss, _ = assign({**SETTINGS, "perfect_e_box": False})
        for _name, kwargs in hfss.finite:
            self.assertFalse(kwargs.get("use_thickness", False))
            self.assertNotIn("thickness", kwargs)

    def test_a_run_with_no_recorded_conductivity_gets_the_default(self):
        hfss, _ = assign({**SETTINGS, "perfect_e_box": False})
        self.assertEqual({k["conductivity"] for _n, k in hfss.finite},
                         {model_builder.DEFAULT_BOX_CONDUCTIVITY})

    def test_nonsense_conductivity_falls_back_to_the_default(self):
        for bad in (0, -3, "abc"):
            hfss, _ = assign({**SETTINGS, "perfect_e_box": False, "box_conductivity": bad})
            self.assertEqual({k["conductivity"] for _n, k in hfss.finite},
                             {model_builder.DEFAULT_BOX_CONDUCTIVITY}, bad)

    def test_the_conductors_are_not_affected_by_the_box_setting(self):
        hfss, _ = assign({**SETTINGS, "perfect_e_box": False})
        self.assertIn("pcb_copper_top", hfss.perfect_e)
        self.assertNotIn("pcb_copper_top", [n for n, _ in hfss.finite])

    def test_a_boundary_hfss_refuses_is_reported_not_fatal(self):
        hfss, logs = assign({**SETTINGS, "perfect_e_box": False}, fail=True)
        self.assertTrue(any("finite conductivity" in m for m in logs))

    def test_the_setting_has_a_label_for_the_comparison_page(self):
        self.assertIn("box_conductivity", model_builder.MATERIAL_LABELS)


if __name__ == "__main__":
    unittest.main()
