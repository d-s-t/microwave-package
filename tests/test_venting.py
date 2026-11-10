"""Tests for the vent grooves (vents_on = 1): the plan, the layout check and
the HFSS builder against a modeller that records what it is asked to cut.
Nothing opens Altium, SolidWorks or AEDT.

Run: python -m unittest discover -s tests
"""
import logging
import unittest

from microwave_package import paths
from microwave_package.design import (
    constraints, copper_geometry as cg, equations as eq, parameter_meta, venting,
)
from microwave_package.hfss import vents as vent_builder


def setUpModule():
    logging.disable(logging.INFO)


def tearDownModule():
    logging.disable(logging.NOTSET)


def defaults():
    return eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))


def values(**overrides):
    # The vents are off by default (they are cut after the box is made); these tests are about the feature, so they ask for it.
    return {**defaults(), "vents_on": 1, **overrides}


class TestThePlan(unittest.TestCase):
    def test_off_by_default_and_on_when_told(self):
        # The vents are cut after the box is made (2026-11-08), so a run has none unless it asks.
        self.assertEqual(defaults()["vents_on"], 0)
        self.assertEqual(venting.plan(dict(defaults())), [])
        self.assertEqual(venting.plan(values(vents_on=0)), [])
        self.assertTrue(venting.plan(values()))

    def test_it_is_a_flag_with_two_dimensions_in_the_form(self):
        self.assertIn("vents_on", parameter_meta.BOOLEAN_PARAMETERS)
        names = [n for g in parameter_meta.grouped(list(defaults())) for n in g["names"]]
        self.assertIn("vent_width", names)
        self.assertIn("vent_depth", names)
        self.assertNotIn("vents_on", names)

    def test_pedestal_and_one_per_screw(self):
        names = [v.name for v in venting.plan(values())]
        self.assertEqual(names, ["pedestal", "screw_1", "screw_2", "screw_3", "screw_4"])

    def test_no_screws_no_screw_vents(self):
        self.assertEqual([v.name for v in venting.plan(values(screws_on=0))], ["pedestal"])

    def test_no_recess_no_pedestal_vent(self):
        names = [v.name for v in venting.plan(values(pedestal_depth=0))]
        self.assertNotIn("pedestal", names)

    def test_a_bare_board_has_no_pedestal_vent_but_keeps_its_screw_vents(self):
        # no die and no opening in the bottom copper, so a recess there is not a
        # pocket this vent serves
        for extra in (dict(no_chip=1), dict(no_chip=1, pedestal_depth=0.5), dict(no_chip=1, pedestal_depth=0)):
            names = [v.name for v in venting.plan(values(**extra))]
            self.assertEqual(names, ["screw_1", "screw_2", "screw_3", "screw_4"], extra)

    def test_every_vent_reaches_the_side_face_past_the_wall(self):
        v = values()
        face = v["pcb_side"] / 2.0 + v["wall_width"]
        for vent in venting.plan(v):
            self.assertGreaterEqual(max(abs(vent.start[1]), abs(vent.end[1])), face)

    def test_screw_vents_go_to_the_nearest_side(self):
        screws = [x for x in venting.plan(values()) if x.name.startswith("screw")]
        for vent in screws:
            self.assertEqual(vent.start[1] > 0, vent.end[1] > 0)

    def test_the_pedestal_vent_runs_across_the_recess_and_off_the_line(self):
        v = values()
        ped = venting.plan(v)[0]
        u_min, v_min, u_max, v_max = ped.rectangle()
        self.assertLess(v_min, -v["chip_side"] / 2)
        self.assertGreater(v_max, v["chip_side"] / 2)
        # it crosses the CPW's line only under the die, below the ground plane's
        # opening: it must not be near the launch
        self.assertLess(abs(u_max), 1.0)

    def test_vents_clear_the_screws_of_each_other_and_the_pedestal_vent(self):
        rects = {x.name: x.rectangle() for x in venting.plan(values())}
        names = list(rects)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                ra, rb = rects[a], rects[b]
                overlap = ra[0] < rb[2] and rb[0] < ra[2] and ra[1] < rb[3] and rb[1] < ra[3]
                self.assertFalse(overlap, (a, b))


class TestCutoff(unittest.TestCase):
    def test_the_default_is_far_below_cutoff(self):
        v = values()
        self.assertGreater(venting.cutoff_ghz(v["vent_width"], v["vent_depth"]),
                           venting.CUTOFF_MARGIN * venting.BAND_TOP_GHZ)

    def test_known_value(self):
        self.assertAlmostEqual(venting.cutoff_ghz(0.5, 0.3), 299.792458, places=3)


class TestTheCheck(unittest.TestCase):
    def test_default_passes(self):
        self.assertIsNone(venting.check(values()))

    def test_off_is_not_checked(self):
        self.assertIsNone(venting.check(values(vents_on=0, vent_width=5.0)))

    def test_too_wide_is_over_cutoff(self):
        self.assertIn("cutoff", venting.check(values(vent_width=2.0)))

    def test_too_narrow_to_machine(self):
        self.assertIn("vent_width", venting.check(values(vent_width=0.1)))

    def test_too_deep_for_the_floor(self):
        self.assertIn("floor", venting.check(values(vent_depth=1.1, screw_engagement=1.2)))

    def test_layout_check_reports_it(self):
        self.assertIn("vent", constraints.check_layout(values(vent_width=0.1)) or "")


class FakeObject:
    def __init__(self, name):
        self.name = name


class FakeModeler:
    def __init__(self):
        self.boxes, self.cuts, self.unions = [], [], []

    def create_box(self, origin, sizes):
        self.boxes.append((origin, sizes))
        return FakeObject("tool")

    def subtract(self, blank, tools, keep_originals=True):
        self.cuts.append((blank, tuple(tools)))

    def unite(self, names):
        self.unions.append(tuple(names))


class FakeHfss:
    def __init__(self):
        self.modeler = FakeModeler()


class TestTheBuilder(unittest.TestCase):
    """The model cuts no vent and adds no skin. A vented combination is refused
    (constraints.check_vents), and the builder refuses it again before anything is cut."""

    stack = cg.Stack(pcb_bottom=3.0, pcb_top=3.381, chip_bottom=3.0, chip_top=3.381,
                     copper_thickness=0.035, chip_metal_thickness=0.0002)

    def test_a_vented_combination_is_refused_and_nothing_is_cut_or_added(self):
        hfss = FakeHfss()
        with self.assertRaises(ValueError) as caught:
            vent_builder.build_vents(hfss, cg.Frame(), self.stack, values())
        self.assertIn("untick vents_on", str(caught.exception))
        self.assertEqual(hfss.modeler.boxes, [])
        self.assertEqual(hfss.modeler.cuts, [])
        self.assertEqual(hfss.modeler.unions, [])

    def test_a_vent_too_wide_for_cutoff_is_refused_too(self):
        with self.assertRaises(ValueError):
            vent_builder.build_vents(FakeHfss(), cg.Frame(), self.stack, values(vent_width=3.0))

    def test_nothing_when_off(self):
        hfss = FakeHfss()
        self.assertIsNone(vent_builder.build_vents(hfss, cg.Frame(), self.stack, values(vents_on=0)))
        self.assertEqual(hfss.modeler.cuts, [])
        self.assertEqual(hfss.modeler.unions, [])

    def test_no_skin_is_added_to_the_box_on_any_board(self):
        # The box is imported as SolidWorks made it: no skin under the copper, over a
        # recess or a groove, on any board (VENTING.md, "Why vents are not simulated").
        blind = cg.Stack(pcb_bottom=3.0, pcb_top=3.508, chip_bottom=3.127, chip_top=3.508,
                         copper_thickness=0.035, chip_metal_thickness=0.0002)
        for kind, stack, extra in (
                ("through opening", self.stack, {}),
                ("blind pocket", blind, dict(pcb_width=0.508)),
                ("bare board", self.stack, dict(no_chip=1))):
            with self.subTest(kind):
                hfss = FakeHfss()
                self.assertIsNone(vent_builder.build_vents(
                    hfss, cg.Frame(), stack, values(vents_on=0, pedestal_depth=0.5, **extra)))
                self.assertEqual(hfss.modeler.unions, [])
                self.assertEqual(hfss.modeler.cuts, [])

    def test_the_skin_constants_and_the_recess_skin_are_gone(self):
        self.assertFalse(hasattr(vent_builder, "build_recess_skin"))
        self.assertFalse(hasattr(vent_builder, "SKIN_DEPTH"))
        self.assertFalse(hasattr(vent_builder, "WALL_OVERLAP"))


if __name__ == "__main__":
    unittest.main()
