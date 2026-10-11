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
    return dict(defaults(), **overrides)


class TestThePlan(unittest.TestCase):
    def test_on_by_default_and_off_when_told(self):
        self.assertEqual(defaults()["vents_on"], 1)
        self.assertTrue(venting.plan(values()))
        self.assertEqual(venting.plan(values(vents_on=0)), [])

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
        # the box's recess follows pedestal_depth alone, so the vent does too
        names = [v.name for v in venting.plan(values(no_chip=1))]
        self.assertIn("pedestal", names)

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
    stack = cg.Stack(pcb_bottom=3.0, pcb_top=3.381, chip_bottom=3.0, chip_top=3.381,
                     copper_thickness=0.035, chip_metal_thickness=0.0002)

    def test_cuts_one_tool_per_vent_out_of_the_box(self):
        hfss = FakeHfss()
        names = vent_builder.build_vents(hfss, cg.Frame(), self.stack, values())
        self.assertEqual(len(names), 5)
        self.assertEqual(len(hfss.modeler.cuts), 5)
        self.assertTrue(all(blank == "box" for blank, _ in hfss.modeler.cuts))

    def test_the_groove_sits_just_under_the_board(self):
        hfss = FakeHfss()
        vent_builder.build_vents(hfss, cg.Frame(), self.stack, values())
        for origin, sizes in hfss.modeler.boxes[:5]:
            self.assertAlmostEqual(origin[1], 3.0 - 0.3)
            self.assertAlmostEqual(sizes[1], 0.3)

    def test_the_bottom_copper_keeps_its_box_under_the_pedestal_groove(self):
        # the copper is drawn down into the floor's top 35 um; a groove to
        # the floor's top would leave it half over air and HFSS refuses that
        hfss = FakeHfss()
        vent_builder.build_vents(hfss, cg.Frame(), self.stack, values())
        skins = hfss.modeler.boxes[5:]
        self.assertEqual(len(skins), 2)
        self.assertEqual(hfss.modeler.unions, [("box", "vent_skin_0"), ("box", "vent_skin_1")])
        for origin, sizes in skins:
            self.assertAlmostEqual(origin[1], 3.0 - 0.035)
            self.assertAlmostEqual(sizes[1], 0.035)
        # from the die's opening (3.25) to the board edge (12.5), both sides,
        # across the groove's own width
        spans = sorted((o[2], o[2] + s[2]) for o, s in skins)
        self.assertAlmostEqual(spans[0][0], -12.5)
        self.assertAlmostEqual(spans[0][1], -3.25)
        self.assertAlmostEqual(spans[1][0], 3.25)
        self.assertAlmostEqual(spans[1][1], 12.5)
        self.assertAlmostEqual(skins[0][1][0], 0.5)

    def test_no_skin_for_a_sheet_of_copper_or_without_the_pedestal_vent(self):
        sheet = cg.Stack(pcb_bottom=3.0, pcb_top=3.381, chip_bottom=3.0, chip_top=3.381,
                         copper_thickness=0.0, chip_metal_thickness=0.0)
        hfss = FakeHfss()
        vent_builder.build_vents(hfss, cg.Frame(), sheet, values())
        self.assertEqual(hfss.modeler.unions, [])
        hfss = FakeHfss()
        vent_builder.build_vents(hfss, cg.Frame(), self.stack, values(pedestal_depth=0))
        self.assertEqual(hfss.modeler.unions, [])

    def test_nothing_when_off(self):
        hfss = FakeHfss()
        self.assertIsNone(vent_builder.build_vents(hfss, cg.Frame(), self.stack, values(vents_on=0)))
        self.assertEqual(hfss.modeler.cuts, [])

    def test_a_vent_too_wide_for_cutoff_is_refused(self):
        with self.assertRaises(ValueError):
            vent_builder.build_vents(FakeHfss(), cg.Frame(), self.stack, values(vent_width=3.0))


if __name__ == "__main__":
    unittest.main()
