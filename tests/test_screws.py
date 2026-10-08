"""Tests for the four M2 corner screws (screws_on = 1).

The plan, the layout checks and the HFSS builder are exercised here, the
last against a modeller that records what it was asked to cut and build.
Nothing opens Altium, SolidWorks or AEDT.

Run: python -m unittest discover -s tests
"""
import logging
import unittest

from microwave_package import paths
from microwave_package.design import (
    altium_generation as ag, constraints, copper_geometry as cg, equations as eq,
    parameter_meta, screws,
)
from microwave_package.hfss import model_builder, screws as screw_builder


def setUpModule():
    logging.disable(logging.INFO)


def tearDownModule():
    logging.disable(logging.NOTSET)


def defaults():
    return eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))


def with_screws(**overrides):
    return dict(defaults(), screws_on=1, **overrides)


class TestTheParameter(unittest.TestCase):
    def test_it_is_off_by_default(self):
        self.assertEqual(defaults()["screws_on"], 0)
        self.assertIsNone(screws.plan(defaults()))
        self.assertFalse(screws.enabled(defaults()))

    def test_it_is_a_flag_not_a_swept_dimension(self):
        self.assertIn(parameter_meta.SCREWS_PARAM, parameter_meta.BOOLEAN_PARAMETERS)
        listed = [n for g in parameter_meta.grouped(defaults()) for n in g["names"]]
        self.assertNotIn("screws_on", listed)

    def test_its_dimensions_are_in_the_form(self):
        listed = [n for g in parameter_meta.grouped(defaults()) for n in g["names"]]
        for name in parameter_meta.SCREW_GEOMETRY_PARAMETERS:
            self.assertIn(name, listed)
            self.assertIn(name, defaults())
            self.assertNotEqual(parameter_meta.label_for(name), name, f"{name} has no label")

    def test_every_dimension_has_a_drawing(self):
        for name in parameter_meta.SCREW_GEOMETRY_PARAMETERS:
            self.assertTrue((paths.PARAMETER_DRAWINGS_DIR / f"{name}.svg").is_file(),
                            f"no drawing for {name}")


class TestThePlan(unittest.TestCase):
    def test_four_screws_inset_from_the_corners(self):
        plan = screws.plan(with_screws(pcb_length=50.0, pcb_side=25.0, screw_inset=2.2))
        self.assertEqual(len(plan.positions), 4)
        for u, v in plan.positions:
            self.assertAlmostEqual(abs(u), 22.8)
            self.assertAlmostEqual(abs(v), 10.3)
        self.assertEqual(len({(round(u, 6), round(v, 6)) for u, v in plan.positions}), 4)

    def test_the_head_is_a_ninety_degree_cone(self):
        """One millimetre of radius per millimetre of depth, so the cone
        runs from the 2.0 mm shank out to the 3.8 mm head in 0.9 mm."""
        plan = screws.plan(with_screws())
        self.assertAlmostEqual(plan.head_height, 0.9)
        self.assertAlmostEqual(plan.countersink_depth, (3.8 - 2.3) / 2.0)

    def test_the_countersink_is_shallower_than_the_head(self):
        """It stops at the clearance hole, which is wider than the shank."""
        plan = screws.plan(with_screws())
        self.assertLess(plan.countersink_depth, plan.head_height)

    def test_the_lid_has_to_be_as_thick_as_the_head_is_tall(self):
        self.assertAlmostEqual(screws.plan(with_screws()).min_lid_thickness, 0.9)


class TestChoosingALength(unittest.TestCase):
    def test_the_longest_that_stays_within_the_wanted_bite(self):
        # 2.5 mm of lid and board above a 1 mm floor, 0.8 mm bite: 3.3 at most
        self.assertEqual(screws.choose_length(2.5, 0.8, 1.0), 3)

    def test_it_steps_up_a_size_when_the_stack_needs_it(self):
        self.assertEqual(screws.choose_length(3.4, 0.8, 1.0), 4)

    def test_it_never_comes_out_of_the_underside(self):
        for span in (2.0, 2.5, 3.0, 3.5, 5.0, 7.0, 11.0):
            try:
                length = screws.choose_length(span, 0.8, 1.0)
            except ValueError:
                continue
            self.assertLessEqual(length, span + 1.0 - screws.MIN_FLOOR_LEFT)
            self.assertGreaterEqual(length, span + screws.MIN_ENGAGEMENT)

    def test_a_stack_between_two_sizes_says_what_to_change(self):
        with self.assertRaisesRegex(ValueError, "wall_width"):
            screws.choose_length(3.9, 0.8, 1.0)


class TestConstraints(unittest.TestCase):
    def test_the_defaults_are_unchanged(self):
        self.assertIsNone(constraints.check_layout(defaults()))

    def test_the_default_board_takes_the_screws(self):
        self.assertIsNone(constraints.check_layout(with_screws()))

    def test_a_bare_board_takes_them_too(self):
        self.assertIsNone(constraints.check_layout(with_screws(no_chip=1)))

    def test_a_hole_too_near_the_board_edge_is_caught(self):
        self.assertIn("screw_inset", constraints.check_layout(with_screws(screw_inset=1.4)))

    def test_a_head_that_would_leave_the_lid_is_caught(self):
        reason = constraints.check_layout(with_screws(screw_inset=1.9))
        self.assertIn("lid", reason)

    def test_a_hole_wider_than_the_head_is_caught(self):
        self.assertIn("head", constraints.check_layout(
            with_screws(screw_clearance_diameter=4.0, screw_keepout_diameter=4.5)))

    def test_a_tap_drill_as_big_as_the_screw_is_caught(self):
        self.assertIn("thread", constraints.check_layout(with_screws(screw_tap_diameter=2.0)))

    def test_a_clearance_hole_the_screw_cannot_fit_is_caught(self):
        self.assertIn("screw_clearance_diameter",
                      constraints.check_layout(with_screws(screw_clearance_diameter=2.0)))

    def test_a_keepout_that_reaches_the_cpw_gap_is_caught(self):
        reason = constraints.check_layout(with_screws(pcb_side=7.0))
        self.assertIn("CPW", reason)

    def test_a_keepout_that_reaches_the_connector_is_caught(self):
        """A narrow board puts the corner screw alongside the launch."""
        reason = constraints.check_layout(with_screws(pcb_side=9.5, port_space=2.0))
        self.assertIsNotNone(reason)
        self.assertIn("SMP", reason)

    def test_with_no_chip_the_die_does_not_count(self):
        big = dict(chip_length=44.0, chip_side=20.0, bonding_clearence=0.6)
        gap = 0.27
        self.assertIn("die", constraints.check_screws(with_screws(**big), gap))
        self.assertIsNone(constraints.check_screws(with_screws(no_chip=1, **big), gap))

    def test_the_generator_is_told_the_screws_are_off_by_default(self):
        self.assertEqual(ag.build_params(defaults())["screws_on"], 0)


# --- the HFSS builder, against a recorder -------------------------------

class FakeBody:
    def __init__(self, name, box=None):
        self.name = name
        self.bounding_box = box


class FakeModeler:
    def __init__(self, lid=(0, 1.45, 0, 30, 3.5, 30), box=(0, 0.0, 0, 30, 6.0, 30)):
        self.bodies = {"lid_inside": FakeBody("lid_inside", list(lid)),
                       "box": FakeBody("box", list(box))}
        self.cylinders, self.cones, self.subtractions, self.unites = [], [], [], []
        self._n = 0

    def __getitem__(self, name):
        return self.bodies[name]

    def _make(self, store, record):
        self._n += 1
        body = FakeBody(f"obj{self._n}")
        self.bodies[body.name] = body
        store.append((body, record))
        return body

    def create_cylinder(self, orientation, origin, radius, height, **kw):
        return self._make(self.cylinders, dict(origin=origin, radius=radius, height=height))

    def create_cone(self, orientation, origin, bottom_radius, top_radius, height, **kw):
        return self._make(self.cones, dict(origin=origin, bottom=bottom_radius,
                                           top=top_radius, height=height))

    def unite(self, names):
        self.unites.append(list(names))

    def subtract(self, target, tools, keep_originals=True):
        self.subtractions.append((target, list(tools), keep_originals))


class FakeHfss:
    def __init__(self, **kw):
        self.modeler = FakeModeler(**kw)


FRAME = cg.Frame(x_offset=26.0, z_offset=13.5)
STACK = cg.Stack(pcb_bottom=1.0, pcb_top=1.381, chip_bottom=1.0, chip_top=1.381,
                 copper_thickness=0.035, chip_metal_thickness=0.0002)


def build(values=None, **modeler):
    hfss = FakeHfss(**modeler)
    names = screw_builder.build_screws(
        hfss, FRAME, STACK, values or with_screws(wall_width=1.0), ["pcb_copper_top"],
        log=lambda _m: None)
    return hfss, names


class TestTheBuilder(unittest.TestCase):
    def test_nothing_is_built_when_the_screws_are_off(self):
        hfss = FakeHfss()
        self.assertIsNone(screw_builder.build_screws(
            hfss, FRAME, STACK, defaults(), ["pcb_copper_top"], log=lambda _m: None))
        self.assertEqual(hfss.modeler.cylinders, [])
        self.assertEqual(hfss.modeler.subtractions, [])

    def test_four_screws_are_named_for_the_material_rule(self):
        _, names = build()
        self.assertEqual(names, ["screw_1", "screw_2", "screw_3", "screw_4"])
        rule = next(r for r in model_builder.MATERIAL_RULES if r[0] in "screw_1")
        self.assertEqual(rule[2:], ("mat_box", "perfect_e_box"))

    def test_every_part_is_cut_at_every_corner(self):
        hfss, _ = build()
        targets = [t for t, _, _ in hfss.modeler.subtractions]
        for part in ("box", "pcb_substrate", "lid_inside"):
            self.assertEqual(targets.count(part), 4, part)
        self.assertEqual(targets.count(["pcb_copper_top"]), 4)

    def test_the_box_is_cut_to_the_screws_own_diameter(self):
        """The tap drill is for the drawing; the model must not overlap."""
        hfss, _ = build()
        radii = {round(r["radius"], 3) for _, r in hfss.modeler.cylinders}
        self.assertIn(1.0, radii)
        self.assertNotIn(0.8, radii)

    def test_the_head_cone_is_the_countersink_cone(self):
        """Same radius at the flat top, so the head sits flush."""
        hfss, _ = build()
        top_radii = {round(c["top"], 3) for _, c in hfss.modeler.cones}
        self.assertEqual(top_radii, {1.9})

    def test_the_lid_must_be_thick_enough_for_the_head(self):
        with self.assertRaisesRegex(ValueError, "thinner"):
            build(lid=(0, 1.45, 0, 30, 1.9, 30))

    def test_a_stack_no_screw_fits_is_reported(self):
        with self.assertRaisesRegex(ValueError, "wall_width"):
            build(lid=(0, 1.45, 0, 30, 4.9, 30))

    def test_the_tip_stays_in_the_floor(self):
        hfss, _ = build()
        shanks = [r for _, r in hfss.modeler.cylinders
                  if r["radius"] == 1.0 and r["height"] > 1.5]
        self.assertEqual(len(shanks), 4)
        for shank in shanks:
            self.assertGreaterEqual(shank["origin"][1], screws.MIN_FLOOR_LEFT)


if __name__ == "__main__":
    unittest.main()