"""Tests for the four M2 corner screws (screws_on = 1).

The plan, the layout checks and the HFSS builder are exercised here, the
last against a modeller that records what it was asked to import, cut and
move. Nothing opens Altium, SolidWorks or AEDT.

Run: python -m unittest discover -s tests
"""
import logging
import os
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


def fit(**overrides):
    """Screws on, with a floor and a lid thick enough for them -- the
    shipped defaults are the thin ones the parts have today."""
    return with_screws(lid_width=1.5, **overrides)


class TestTheParameter(unittest.TestCase):
    def test_it_is_on_by_default_and_off_when_told(self):
        self.assertEqual(defaults()["screws_on"], 1)
        self.assertIsNotNone(screws.plan(defaults()))
        self.assertIsNone(screws.plan(dict(defaults(), screws_on=0)))
        self.assertFalse(screws.enabled(dict(defaults(), screws_on=0)))

    def test_it_is_a_flag_not_a_swept_dimension(self):
        self.assertIn(parameter_meta.SCREWS_PARAM, parameter_meta.BOOLEAN_PARAMETERS)
        listed = [n for g in parameter_meta.grouped(defaults()) for n in g["names"]]
        self.assertNotIn("screws_on", listed)

    def test_its_dimensions_are_in_the_form(self):
        listed = [n for g in parameter_meta.grouped(defaults()) for n in g["names"]]
        for name in parameter_meta.SCREW_GEOMETRY_PARAMETERS + ["lid_width"]:
            self.assertIn(name, listed)
            self.assertIn(name, defaults())
            self.assertNotEqual(parameter_meta.label_for(name), name, f"{name} has no label")

    def test_every_dimension_has_a_drawing(self):
        for name in parameter_meta.SCREW_GEOMETRY_PARAMETERS + ["lid_width"]:
            self.assertTrue((paths.PARAMETER_DRAWINGS_DIR / f"{name}.svg").is_file(),
                            f"no drawing for {name}")

    def test_the_default_floor_and_lid_take_the_screws(self):
        """The shipped stack has to satisfy the rules the screws impose,
        or the shipped defaults would be refused."""
        v = defaults()
        plan = screws.plan(v)
        self.assertGreaterEqual(v["lid_width"], plan.min_lid_thickness)
        self.assertGreaterEqual(plan.engagement, screws.MIN_ENGAGEMENT)

    def test_the_vendor_models_are_all_there(self):
        for length in screws.MODEL_LENGTHS:
            self.assertTrue(os.path.isfile(screw_builder.model_path(length)), length)


class TestThePlan(unittest.TestCase):
    def test_four_screws_inset_from_the_corners(self):
        plan = screws.plan(with_screws(pcb_length=50.0, pcb_side=25.0, screw_inset=2.5))
        self.assertEqual(len(plan.positions), 4)
        for u, v in plan.positions:
            self.assertAlmostEqual(abs(u), 22.5)
            self.assertAlmostEqual(abs(v), 10.0)
        self.assertEqual(len({(round(u, 6), round(v, 6)) for u, v in plan.positions}), 4)

    def test_the_countersink_continues_the_heads_cone(self):
        """A 90 degree cone grows one millimetre of radius per millimetre,
        from the 2.0 mm shank, so over the head's 1.14 mm it reaches 4.28."""
        self.assertAlmostEqual(screws.COUNTERSINK_DIAMETER, 2.0 + 2 * 1.14)
        plan = screws.plan(with_screws())
        self.assertAlmostEqual(plan.countersink_depth, (screws.COUNTERSINK_DIAMETER - 2.3) / 2.0)
        self.assertLess(plan.countersink_depth, screws.HEAD_HEIGHT)

    def test_the_lid_needs_the_head_and_a_seat_under_it(self):
        self.assertAlmostEqual(screws.plan(with_screws()).min_lid_thickness, 1.14 + 0.3)


class TestLength(unittest.TestCase):
    def test_it_is_the_stack_plus_the_engagement(self):
        plan = screws.plan(with_screws(screw_engagement=3.0))
        self.assertAlmostEqual(screws.length_for(plan, lid_top=7.4, floor_top=5.5), 7.4 - 5.5 + 3.0)

    def test_a_given_length_wins(self):
        plan = screws.plan(with_screws(screw_length=6.0))
        self.assertEqual(screws.length_for(plan, lid_top=7.4, floor_top=5.5), 6.0)

    def test_the_model_is_the_shortest_one_that_is_long_enough(self):
        self.assertEqual(screws.model_length(4.9), 5)
        self.assertEqual(screws.model_length(5.0), 5)
        self.assertEqual(screws.model_length(5.1), 6)
        self.assertEqual(screws.model_length(2.0), 3)

    def test_longer_than_any_model_says_what_to_change(self):
        with self.assertRaisesRegex(ValueError, "screw_engagement"):
            screws.model_length(25.0)

class TestTheFloorIsWorkedOut(unittest.TestCase):
    """floor_width is a formula in default.txt, so it is not among the parsed
    numbers; the code works out the same thing. The two must agree."""

    def formula(self):
        content = eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE))
        return eq.parse(content)["floor_width"].raw_value

    def test_it_is_a_formula_not_a_number(self):
        self.assertNotIn("floor_width", defaults())
        self.assertIn("IIF", self.formula())

    def test_the_formula_is_the_engagement_less_the_pedestal(self):
        self.assertIn('"screw_engagement" - "pedestal_depth"', self.formula())

    def test_with_the_screws_off_it_is_the_wall(self):
        v = dict(defaults(), screws_on=0, wall_width=1.3)
        self.assertEqual(screws.floor_width(v), 1.3)

    def test_with_the_screws_on_the_floor_at_the_corners_is_the_thread_length(self):
        """The hole goes right through, so floor_width plus the pedestal's
        recess is exactly screw_engagement."""
        v = with_screws(pedestal_depth=0.5)
        self.assertAlmostEqual(screws.floor_width(v), 3 - 0.5)
        self.assertAlmostEqual(screws.plan(v).engagement, 3.0)
        flat = dict(v, pedestal_depth=0.0)
        self.assertAlmostEqual(screws.floor_width(flat), 3.0)
        self.assertAlmostEqual(screws.plan(flat).engagement, 3.0)

    def test_a_shallow_thread_never_makes_it_thinner_than_the_wall(self):
        v = with_screws(screw_engagement=1.0, wall_width=2.5)
        self.assertEqual(screws.floor_width(v), 2.5)

    def test_a_pinned_number_wins(self):
        self.assertEqual(screws.floor_width(with_screws(floor_width=7.0)), 7.0)

    def test_the_shipped_defaults_need_no_thicker_floor_than_they_get(self):
        self.assertIsNone(constraints.check_layout(defaults()))


class TestConstraints(unittest.TestCase):
    def test_the_shipped_defaults_are_buildable(self):
        self.assertIsNone(constraints.check_layout(defaults()))

    def test_a_lid_as_thin_as_the_old_one_is_refused(self):
        reason = constraints.check_layout(with_screws(lid_width=1.0))
        self.assertIn("lid_width", reason)

    def test_without_the_screws_the_old_thin_stack_is_fine(self):
        self.assertIsNone(constraints.check_layout(
            dict(defaults(), screws_on=0, floor_width=1.0, lid_width=1.0)))

    def test_a_thick_enough_stack_takes_the_screws(self):
        self.assertIsNone(constraints.check_layout(fit()))

    def test_a_bare_board_takes_them_too(self):
        self.assertIsNone(constraints.check_layout(fit(no_chip=1)))

    def test_a_hole_too_near_the_board_edge_is_caught(self):
        self.assertIn("screw_inset", constraints.check_layout(fit(screw_inset=1.4)))

    def test_a_countersink_that_would_leave_the_lid_is_caught(self):
        self.assertIn("lid", constraints.check_layout(fit(screw_inset=2.2)))

    def test_a_tap_drill_as_big_as_the_screw_is_caught(self):
        self.assertIn("thread", constraints.check_layout(fit(screw_tap_diameter=2.0)))

    def test_a_clearance_hole_the_screw_cannot_fit_is_caught(self):
        self.assertIn("screw_clearance_diameter",
                      constraints.check_layout(fit(screw_clearance_diameter=2.0)))

    def test_a_clearance_hole_wider_than_the_countersink_is_caught(self):
        self.assertIn("countersink", constraints.check_layout(
            fit(screw_clearance_diameter=4.5, screw_keepout_diameter=5.0)))

    def test_a_thread_too_short_to_hold_is_caught(self):
        reason = constraints.check_layout(
            fit(screw_engagement=0.5, pedestal_depth=0.0, wall_width=0.8))
        self.assertIn("screw_engagement", reason)

    def test_a_keepout_that_reaches_the_cpw_gap_is_caught(self):
        self.assertIn("CPW", constraints.check_layout(fit(pcb_side=7.0, screw_inset=2.5)))

    def test_the_square_the_plane_gives_up_is_what_has_to_clear_the_cpw(self):
        """The square is the keep-out's radius plus half a millimetre, and a
        board too narrow for it is refused naming the CPW."""
        plan = screws.plan(fit(pcb_side=7.0, screw_inset=2.5))
        self.assertAlmostEqual(screws.block_half(plan), 1.5 + screws.KEEPOUT_BLOCK_MARGIN)
        self.assertIn("CPW", constraints.check_layout(fit(pcb_side=7.0, screw_inset=2.5)))

    def test_a_keepout_that_reaches_the_connector_is_caught(self):
        """A narrow board puts the corner screw alongside the launch."""
        reason = constraints.check_layout(fit(pcb_side=10.0, port_space=2.0, screw_inset=2.5))
        self.assertIsNotNone(reason)
        self.assertIn("SMP", reason)

    def test_with_no_chip_the_die_does_not_count(self):
        big = dict(chip_length=44.0, chip_side=20.0, bonding_clearence=0.6)
        gap = 0.27
        self.assertIn("die", constraints.check_screws(fit(**big), gap))
        self.assertIsNone(constraints.check_screws(fit(no_chip=1, **big), gap))

    def test_the_generator_is_told_when_the_screws_are_on(self):
        self.assertEqual(ag.build_params(defaults())["screws_on"], 1)
        self.assertEqual(ag.build_params(dict(defaults(), screws_on=0))["screws_on"], 0)


class TestTheAltiumBoardHasItsHoles(unittest.TestCase):
    BOARD = {
        "package_body": "ok", "chip_body": "ok", "package_cavity": "ok",
        "package_ports": "2", "package_bond_pads": "54", "chip_bond_pads": "54",
        "cavity_kind_readback": "board_cutout",
    }

    def test_a_board_without_screws_is_not_asked_for_holes(self):
        self.assertEqual(ag._missing_features(self.BOARD), "")

    def test_with_screws_the_four_holes_are_required(self):
        on = dict(self.BOARD, screws_on="1")
        self.assertIn("never reported on", ag._missing_features(on))
        self.assertIn("failed", ag._missing_features(dict(on, package_screw_holes="FAIL")))
        self.assertEqual(ag._missing_features(dict(on, package_screw_holes="4")), "")

    def test_the_generator_draws_the_copper_round_the_holes_not_a_void(self):
        """A region cannot carry an interior hole, so the plane gives up a
        square and four pieces with the keep-out bitten out are drawn back."""
        script = paths.ALTIUM_GENERATOR.read_text(encoding="utf-8", errors="replace")
        for name in ("AddScrewBlocks", "AddScrewBlock", "AddPlaneCorner", "IsScrewCorner"):
            self.assertIn(name, script)
        self.assertIn("ScrewBlockS := ScrewKeepR + %g;" % screws.KEEPOUT_BLOCK_MARGIN, script)

    def test_the_die_board_is_drawn_without_the_notches(self):
        script = paths.ALTIUM_GENERATOR.read_text(encoding="utf-8", errors="replace")
        reset = script.index("ScrewBlockS := 0.0;", script.index("AddScrewBlocks(Board);"))
        self.assertLess(reset, script.index("OpenNewDocument('PCB', 'chip'"))

    def test_the_generator_reads_the_hole_size_from_the_parameters(self):
        script = (paths.ALTIUM_GENERATOR).read_text(encoding="utf-8", errors="replace")
        self.assertIn("screw_clearance_diameter", script)
        self.assertIn("eRegionKind_BoardCutout", script)


# --- the HFSS builder, against a recorder -------------------------------

class FakeBody:
    def __init__(self, name, box=None):
        self.name = name
        self.bounding_box = box


class FakeModeler:
    """Records what would have been imported, cut and moved. An imported
    screw model is a body of its own file's length, tip on the origin."""

    def __init__(self, lid, box):
        self.bodies = {"lid_inside": FakeBody("lid_inside", list(lid)),
                       "box": FakeBody("box", list(box))}
        self.cylinders, self.cones, self.boxes = [], [], []
        self.subtractions, self.unites, self.moves, self.imports = [], [], [], []
        self._n = 0

    @property
    def object_names(self):
        return list(self.bodies)

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

    def create_box(self, origin, sizes, **kw):
        return self._make(self.boxes, dict(origin=origin, sizes=sizes))

    def import_3d_cad(self, path):
        self.imports.append(path)
        length = int(os.path.basename(path).split("M2x")[1].split("_")[0])
        self._n += 1
        self.bodies[f"imported{self._n}"] = FakeBody(f"imported{self._n}", [-1, 0, -1, 1, length, 1])

    def unite(self, names):
        self.unites.append(list(names))

    def subtract(self, target, tools, keep_originals=True):
        self.subtractions.append((target, list(tools), keep_originals))

    def move(self, names, vector):
        self.moves.append((list(names), list(vector)))


class FakeHfss:
    def __init__(self, lid, box):
        self.modeler = FakeModeler(lid, box)


FRAME = cg.Frame(x_offset=26.0, z_offset=13.5)
# floor 2.5 mm + 0.5 pedestal: the board's underside, and the floor's top, at 3.0
# floor 5 mm + 0.5 pedestal: the board's underside at 5.5
STACK = cg.Stack(pcb_bottom=3.0, pcb_top=3.381, chip_bottom=3.0, chip_top=3.381,
                 copper_thickness=0.035, chip_metal_thickness=0.0002)
LID = (0, 3.416, 0, 52, 4.916, 27)      # 1.5 mm thick, resting on the copper
BOX = (0, 0.0, 0, 52, 6.0, 27)


def build(values=None, lid=LID, box=BOX):
    hfss = FakeHfss(lid, box)
    names = screw_builder.build_screws(
        hfss, FRAME, STACK, values or fit(), log=lambda _m: None)
    return hfss, names


class TestTheBuilder(unittest.TestCase):
    def test_nothing_is_built_when_the_screws_are_off(self):
        hfss = FakeHfss(LID, BOX)
        self.assertIsNone(screw_builder.build_screws(
            hfss, FRAME, STACK, dict(defaults(), screws_on=0),
            log=lambda _m: None))
        self.assertEqual(hfss.modeler.imports, [])
        self.assertEqual(hfss.modeler.subtractions, [])

    def test_the_vendor_model_is_imported_not_drawn(self):
        hfss, names = build()
        self.assertEqual(names, ["screw_1", "screw_2", "screw_3", "screw_4"])
        self.assertEqual(len(hfss.modeler.imports), 4)
        for path in hfss.modeler.imports:
            self.assertTrue(os.path.isfile(path), path)
            self.assertIn("ISO_10642_M2x", path)

    def test_the_screw_is_cut_to_length_from_the_next_model_up(self):
        """Lid top 7.416, board underside 5.5, 3 mm engagement: 4.916 mm,
        from the M2x5, shortened by 0.084 mm."""
        hfss, _ = build()
        self.assertTrue(all("M2x5_" in p for p in hfss.modeler.imports))
        trims = [r for _, r in hfss.modeler.boxes]
        self.assertEqual(len(trims), 4)
        self.assertAlmostEqual(trims[0]["sizes"][1], 0.084 + 2.0, places=3)

    def test_a_model_of_exactly_the_length_is_not_cut(self):
        hfss, _ = build(lid=(0, 3.416, 0, 52, 5.0, 27))
        self.assertEqual(hfss.modeler.boxes, [])

    def test_the_top_ends_up_flush_with_the_lid(self):
        """Moved up by lid top minus the model's length, since its top
        is at Y = length."""
        hfss, _ = build()
        for _, vector in hfss.modeler.moves:
            self.assertAlmostEqual(vector[1], 4.916 - 5)

    def test_it_is_moved_onto_each_corner(self):
        hfss, _ = build()
        xs = sorted(round(v[0], 3) for _, v in hfss.modeler.moves)
        inset = fit()["screw_inset"]
        near, far = 26.0 - (25.0 - inset), 26.0 + (25.0 - inset)
        self.assertEqual(xs, [round(near, 3)] * 2 + [round(far, 3)] * 2)

    def test_the_screws_take_the_box_material_by_name(self):
        rule = next(r for r in model_builder.MATERIAL_RULES if r[0] in "screw_1")
        self.assertEqual(rule[2:], ("mat_box", "perfect_e_box"))

    def test_every_part_is_cut_at_every_corner(self):
        hfss, _ = build()
        targets = [t for t, _, _ in hfss.modeler.subtractions]
        for part in ("box", "pcb_substrate", "lid_inside"):
            self.assertEqual(targets.count(part), 4, part)

    def test_the_box_is_cut_to_the_screws_own_diameter(self):
        """The tap drill is for the drawing; the model must not overlap."""
        hfss, _ = build()
        radii = {round(r["radius"], 3) for _, r in hfss.modeler.cylinders}
        self.assertIn(1.0, radii)
        self.assertNotIn(0.8, radii)

    def test_the_countersink_reaches_the_cone_diameter_at_the_lid_top(self):
        hfss, _ = build()
        self.assertEqual({round(c["top"], 3) for _, c in hfss.modeler.cones}, {2.14})

    def test_a_lid_thinner_than_the_head_and_seat_is_refused(self):
        with self.assertRaisesRegex(ValueError, "lid_width"):
            build(lid=(0, 5.916, 0, 52, 6.9, 27))

    def test_a_screw_that_would_come_out_of_the_underside_is_refused(self):
        with self.assertRaisesRegex(ValueError, "underside"):
            build(box=(0, 1.0, 0, 52, 6.0, 27))

    def test_a_screw_longer_than_every_model_is_refused(self):
        with self.assertRaisesRegex(ValueError, "longest"):
            build(fit(screw_length=30.0), box=(0, -40.0, 0, 52, 9.0, 27))


if __name__ == "__main__":
    unittest.main()