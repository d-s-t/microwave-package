"""Tests for how an imported solid's name picks its material.

This is worth testing precisely because getting it wrong is invisible.
The old if/elif chain would have handed `pcb_copper` the Rogers
permittivity, and nothing would have complained -- the model would just
have solved to the wrong frequencies.

HFSS is not involved: a tiny stand-in records what would have been
assigned, so the rules can be checked without AEDT.

Run: python -m unittest discover -s tests
"""
import unittest

from eigen_mode_analyze import model_builder


class FakeObject:
    def __init__(self, name):
        self.name = name
        self.material_name = None


class FakeHfss:
    """Records assignments instead of performing them."""

    def __init__(self, names):
        self._objects = [FakeObject(n) for n in names]
        self.perfect_e = []

    @property
    def modeler(self):
        return self

    @property
    def solid_objects(self):
        return self._objects

    def assign_perfect_e(self, assignment):
        self.perfect_e.append(assignment)


SETTINGS = {
    "mat_box": "aluminum",
    "mat_pcb": "Rogers TMM 10i (tm)",
    "mat_chip": "silicon",
    "mat_vacuum": "vacuum",
    "mat_conductor_pcb": "copper",
    "mat_conductor_chip": "niobium",
    "perfect_e_box": True,
    "perfect_e_conductors": True,
    "perfect_e_connector": True,
}


def assign(names, settings=None, log=lambda _msg: None):
    hfss = FakeHfss(names)
    objects = model_builder.assign_materials_and_boundaries(
        hfss, dict(settings or SETTINGS), log=log
    )
    return hfss, objects


class TestTheOriginalBodies(unittest.TestCase):
    """The bodies that existed before copper did must keep behaving."""

    def test_each_gets_its_own_material(self):
        hfss, _ = assign(["vacuum", "PCB", "chip", "box", "lid", "chip_lid"])
        got = {o.name: o.material_name for o in hfss.modeler.solid_objects}
        self.assertEqual(got["vacuum"], "vacuum")
        self.assertEqual(got["PCB"], "Rogers TMM 10i (tm)")
        self.assertEqual(got["chip"], "silicon")
        self.assertEqual(got["box"], "aluminum")
        self.assertEqual(got["lid"], "aluminum")
        self.assertEqual(got["chip_lid"], "aluminum")

    def test_chip_lid_is_metal_not_silicon(self):
        """`chip_lid` contains `chip`. The specific rule has to win, or
        the lid becomes silicon."""
        hfss, _ = assign(["chip_lid"])
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "aluminum")

    def test_perfect_e_on_the_enclosure(self):
        hfss, _ = assign(["box", "lid", "chip_lid", "PCB"])
        self.assertEqual(sorted(hfss.perfect_e), ["box", "chip_lid", "lid"])


class TestCopperDoesNotBecomeSubstrate(unittest.TestCase):
    """The regression this refactor exists to prevent."""

    def test_pcb_copper_is_not_given_the_pcb_dielectric(self):
        hfss, _ = assign(["pcb_copper_top"])
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "copper")

    def test_pcb_vias_are_conductor(self):
        hfss, _ = assign(["pcb_vias"])
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "copper")

    def test_pcb_substrate_is_still_the_dielectric(self):
        hfss, _ = assign(["pcb_substrate"])
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "Rogers TMM 10i (tm)")

    def test_chip_conductor_is_not_given_silicon(self):
        hfss, _ = assign(["chip_conductor"])
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "niobium")

    def test_chip_substrate_is_still_silicon(self):
        hfss, _ = assign(["chip_substrate"])
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "silicon")

    def test_a_full_copper_model_assigns_every_body_correctly(self):
        names = [
            "vacuum", "box", "lid", "chip_lid",
            "pcb_substrate", "pcb_copper_top", "pcb_copper_bottom", "pcb_vias",
            "chip_substrate", "chip_conductor", "wirebond_1", "connector_J1",
        ]
        hfss, _ = assign(names)
        got = {o.name: o.material_name for o in hfss.modeler.solid_objects}
        self.assertEqual(got["pcb_substrate"], "Rogers TMM 10i (tm)")
        self.assertEqual(got["pcb_copper_top"], "copper")
        self.assertEqual(got["pcb_copper_bottom"], "copper")
        self.assertEqual(got["pcb_vias"], "copper")
        self.assertEqual(got["chip_substrate"], "silicon")
        self.assertEqual(got["chip_conductor"], "niobium")
        self.assertEqual(got["wirebond_1"], "copper")
        self.assertEqual(got["connector_J1"], "aluminum")


class TestPerfectEToggles(unittest.TestCase):
    def test_conductors_get_perfect_e_by_default(self):
        hfss, _ = assign(["pcb_copper_top", "chip_conductor", "connector_J1"])
        self.assertEqual(
            sorted(hfss.perfect_e), ["chip_conductor", "connector_J1", "pcb_copper_top"]
        )

    def test_turning_the_conductor_toggle_off_leaves_the_materials(self):
        settings = dict(SETTINGS, perfect_e_conductors=False)
        hfss, _ = assign(["pcb_copper_top", "chip_conductor"], settings)
        self.assertEqual(hfss.perfect_e, [])
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "copper")
        self.assertEqual(hfss.modeler.solid_objects[1].material_name, "niobium")

    def test_substrates_never_get_perfect_e(self):
        hfss, _ = assign(["pcb_substrate", "chip_substrate", "vacuum"])
        self.assertEqual(hfss.perfect_e, [])


class TestUnmatchedBodies(unittest.TestCase):
    def test_an_unknown_body_is_warned_about_not_silently_defaulted(self):
        """A renamed or mis-exported body must be loud. Silence here
        shows up as a puzzling frequency shift days later."""
        messages = []
        hfss, _ = assign(["mystery_part"], log=messages.append)
        self.assertIsNone(hfss.modeler.solid_objects[0].material_name)
        self.assertTrue(any("mystery_part" in m for m in messages))
        self.assertTrue(any("WARNING" in m for m in messages))

    def test_no_warning_when_everything_matches(self):
        messages = []
        assign(["vacuum", "box", "pcb_substrate"], log=messages.append)
        self.assertEqual(messages, [])


class TestObjectsDict(unittest.TestCase):
    def test_keys_the_rest_of_the_pipeline_relies_on(self):
        """post_processing looks up 'vacuum' by name, so the key must
        survive the rename of the underlying body."""
        _, objects = assign(["vacuum", "pcb_substrate", "chip_substrate", "box"])
        self.assertEqual(objects["vacuum"].name, "vacuum")
        self.assertEqual(objects["pcb"].name, "pcb_substrate")
        self.assertEqual(objects["chip"].name, "chip_substrate")
        self.assertEqual(objects["box"].name, "box")


if __name__ == "__main__":
    unittest.main()
