"""The recess under the die, the vents, and the refinement and field parts made from a run.

The board's bottom copper has a hole the size of the die's opening, blind or through, so a
recess under the die lies under the hole and no copper is drawn over it. Refused: a recess
on a bare board (its bottom copper is whole: a sealed air void under the ground plane), a
recess wider than the opening (copper would lie over its edge), and vents in every run. A
refinement or field image is refused when its parent cannot be rebuilt: a vented parent, a
parent recess the copper cannot close, or a blind parent built with its bottom copper whole
under the die (made from 2026-11-09 14:00 on, and without the hole's marker). Nothing opens
Altium, SolidWorks or AEDT.

Run: python -m unittest discover -s tests
"""
import logging
import os
import tempfile
import unittest
from unittest import mock

from microwave_package import paths, pipeline
from microwave_package.design import cavity, constraints, copper_geometry as cg, equations as eq
from microwave_package.design.sweep import SweepParameter
from microwave_package.workflow import companion
from microwave_package.workflow.run_manager import Run


def setUpModule():
    logging.disable(logging.INFO)


def tearDownModule():
    logging.disable(logging.NOTSET)


# 0.127 mm over the 0.381 mm die: the die's opening is a blind pocket.
BLIND_BOARD = 0.508

# The package board's bottom copper in copper.txt, relative to the board centre: a
# rectangle over the die, as Altium draws a blind pocket's back plane; and copper that
# does not reach the die at all.
WHOLE_UNDER_DIE = "board=package\nregion|bottom|-3,-3,3,-3,3,3,-3,3\n"
NOTHING_UNDER_DIE = "board=package\nregion|bottom|4,-5,5,-5,5,5,4,5\n"


def values(**overrides):
    base = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    return {**base, **overrides}


def sweep(name, *vals):
    return SweepParameter(name=name, values=list(vals))


class TestTheRule(unittest.TestCase):
    def test_the_committed_defaults_do_not_trip_it(self):
        # default.txt: a 0.381 mm board over the 0.381 mm die (a through opening), 0.5 mm recess
        self.assertIsNone(constraints.check_recess_copper(values()))

    def test_a_blind_pocket_keeps_a_recess_inside_its_hole(self):
        self.assertIsNone(constraints.check_recess_copper(values(pedestal_depth=0.5, pcb_width=BLIND_BOARD)))

    def test_a_square_recess_wider_than_the_hole_is_refused(self):
        reason = constraints.check_recess_copper(values(pedestal_depth=0.5, pcb_width=BLIND_BOARD,
                                                        pedestal_size=6.6))
        self.assertIn("pedestal_size 6.6 mm is wider than the die's opening (6.5 mm)", reason)
        self.assertIn("cavity_clearance", reason)

    def test_a_circular_recess_is_checked_by_its_diameter(self):
        self.assertIsNone(constraints.check_recess_copper(
            values(pedestal_depth=0.5, pedestal_shape=1, pedestal_diameter=6.4)))
        reason = constraints.check_recess_copper(
            values(pedestal_depth=0.5, pedestal_shape=1, pedestal_diameter=6.6))
        self.assertIn("pedestal_diameter 6.6 mm", reason)

    def test_a_bare_board_with_a_typed_recess_is_refused(self):
        reason = constraints.check_recess_copper(values(no_chip=1, pedestal_depth=0.5))
        self.assertIn("bare board", reason)
        self.assertIn("set pedestal_depth to 0", reason)
        # a thicker board does not help a bare one: it has no opening
        self.assertNotIn("thicker than the die", reason)

    def test_a_bare_board_without_a_recess_is_fine(self):
        self.assertIsNone(constraints.check_recess_copper(values(no_chip=1, pedestal_depth=0)))

    def test_the_launch_cutout_does_not_change_the_decision(self):
        self.assertIsNone(constraints.check_recess_copper(
            values(pedestal_depth=0.5, pcb_width=BLIND_BOARD, launch_bottom_cutout_radius=1.0)))
        self.assertIsNotNone(constraints.check_recess_copper(
            values(no_chip=1, pedestal_depth=0.5, launch_bottom_cutout_radius=1.0)))


class TestTheVents(unittest.TestCase):
    def test_vents_are_refused_in_every_run(self):
        for extra in (dict(pedestal_depth=0.5), dict(pedestal_depth=0), dict(no_chip=1),
                      dict(pcb_width=BLIND_BOARD, pedestal_depth=0.5)):
            with self.subTest(**extra):
                self.assertEqual(constraints.check_vents(values(vents_on=1, **extra)),
                                 constraints.VENTS_REFUSAL)

    def test_vents_off_are_not_refused(self):
        self.assertIsNone(constraints.check_vents(values(vents_on=0)))

    def test_the_message_says_what_to_do(self):
        self.assertIn("Vents are cut after manufacturing", constraints.VENTS_REFUSAL)
        self.assertIn("untick vents_on", constraints.VENTS_REFUSAL)


class FakeModeler:
    def __init__(self):
        self.boxes = []

    def create_box(self, origin, sizes):
        self.boxes.append((origin, sizes))
        return type("Tool", (), {"name": ""})()

    def subtract(self, blank, tools, keep_originals=True):
        pass


class TestTheCopperCutSharesTheDecision(unittest.TestCase):
    stack = cg.Stack(pcb_bottom=3.0, pcb_top=3.381, chip_bottom=3.0, chip_top=3.381,
                     copper_thickness=0.035, chip_metal_thickness=0.0002)

    def test_the_copper_cut_reaches_the_bottom_copper_exactly_when_there_is_a_hole(self):
        for kind, extra in (("through", {}), ("blind", dict(pcb_width=BLIND_BOARD)),
                            ("bare board", dict(no_chip=1))):
            with self.subTest(kind):
                opening = cavity.plan(values(**extra))
                modeler = FakeModeler()
                cg.clear_cavity_area(modeler, cg.Frame(), self.stack, opening, ["pcb_copper_bottom"])
                if opening.is_empty:
                    self.assertEqual(modeler.boxes, [])
                    self.assertFalse(opening.cuts_bottom_copper)
                    continue
                (origin, _sizes), = modeler.boxes
                # the cut goes down through the bottom copper, for a blind pocket as for a through cutout
                self.assertLess(origin[1], self.stack.pcb_bottom - 1e-6)
                self.assertTrue(opening.cuts_bottom_copper)


class TestTheCreatePath(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)

    def settings(self, **overrides):
        return {**pipeline.DEFAULT_ANALYSIS_SETTINGS, **overrides}

    def runs_on_disk(self):
        return sorted(os.listdir(paths.RUNS_ROOT))

    def test_a_blind_pocket_with_a_recess_inside_its_hole_is_created(self):
        run = pipeline.create_run([sweep("pedestal_depth", 0.5)], self.settings(run_both=False),
                                  {"pcb_width": BLIND_BOARD})
        self.assertIsInstance(run, Run)
        self.assertTrue(os.path.isdir(run.run_dir))

    def test_a_recess_wider_than_the_hole_is_refused_and_no_folder_is_made(self):
        with self.assertRaises(ValueError) as caught:
            pipeline.create_run([sweep("pedestal_depth", 0.5)], self.settings(run_both=False),
                                {"pcb_width": BLIND_BOARD, "pedestal_size": 6.6})
        self.assertIn("Run not created", str(caught.exception))
        self.assertIn("wider than the die's opening", str(caught.exception))
        self.assertEqual(self.runs_on_disk(), [])

    def test_a_pair_is_refused_whole_and_neither_half_is_made(self):
        # run_both (the default) makes an Eigenmode and a Driven run on one geometry
        self.assertTrue(self.settings()["run_both"])
        with self.assertRaises(ValueError):
            pipeline.create_run([sweep("pedestal_depth", 0.5)], self.settings(),
                                {"pcb_width": BLIND_BOARD, "pedestal_size": 6.6})
        self.assertEqual(self.runs_on_disk(), [])

    def test_a_bare_board_with_a_typed_recess_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            pipeline.create_run([], self.settings(), {"no_chip": 1.0, "pedestal_depth": 0.5})
        self.assertIn("bare board", str(caught.exception))
        self.assertEqual(self.runs_on_disk(), [])

    def test_a_refusal_names_the_combinations_and_refuses_the_whole_run(self):
        with self.assertRaises(ValueError) as caught:
            pipeline.create_run([sweep("pedestal_depth", 0.0, 0.5)], self.settings(run_both=False),
                                {"no_chip": 1.0})
        message = str(caught.exception)
        self.assertIn("1 of 2 combinations are refused", message)
        self.assertIn("pedestal_depth = 0.5", message)
        self.assertEqual(self.runs_on_disk(), [])

    def test_a_through_opening_with_a_recess_is_created(self):
        run = pipeline.create_run([sweep("pedestal_depth", 0.5)], self.settings(), {})
        self.assertIsInstance(run, Run)
        self.assertTrue(os.path.isdir(run.run_dir))

    def test_without_altium_a_bare_board_recess_is_created_because_nothing_is_sealed(self):
        run = pipeline.create_run([], self.settings(run_both=False, use_altium=False),
                                  {"no_chip": 1.0, "pedestal_depth": 0.5})
        self.assertTrue(os.path.isdir(run.run_dir))

    def test_vents_are_refused_at_creation_with_altium_and_without(self):
        for use_altium in (True, False):
            with self.subTest(use_altium=use_altium):
                with self.assertRaises(ValueError) as caught:
                    pipeline.create_run([], self.settings(run_both=False, use_altium=use_altium),
                                        {"vents_on": 1.0})
                self.assertIn("untick vents_on", str(caught.exception))
        self.assertEqual(self.runs_on_disk(), [])


class ChildBase(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)

    def parent(self, defaults, depth=0.5, **settings):
        """A driven run made the way the web app makes one, with its one combination done."""
        _eigen, run = companion.create_pair([sweep("pedestal_depth", depth)],
                                            {**pipeline.DEFAULT_ANALYSIS_SETTINGS, **settings}, defaults)
        combo = next(iter(run.state["combos"]))
        run.update_combo(combo, post_status="done")
        return Run.load(run.run_id), combo

    def built_parent(self, created_at, copper_text, depth=0.5, **defaults):
        """A parent with the given board copper and creation time (blind, unless `defaults` say so)."""
        parent, combo = self.parent({"pcb_width": BLIND_BOARD, **defaults}, depth=depth)
        folder = parent.combo_dir(combo)
        if copper_text is not None:
            with open(os.path.join(folder, "copper.txt"), "w", encoding="utf-8") as handle:
                handle.write(copper_text)
        parent.config["created_at"] = created_at
        parent.save_config()
        return Run.load(parent.run_id), combo

    def runs_on_disk(self):
        return sorted(os.listdir(paths.RUNS_ROOT))


class TestTheChildren(ChildBase):
    """Refinement and field parts are made from the parent's geometry, so they are refused
    where the builder cannot rebuild it (companion._refuse_unbuildable_geometry)."""

    def test_a_refinement_of_a_blind_pocket_with_a_recess_inside_its_hole_is_creatable(self):
        parent, combo = self.parent({"pcb_width": BLIND_BOARD})
        child = companion.create_refinement(parent, "12:13", [combo])
        self.assertTrue(os.path.isdir(child.run_dir))

    def test_a_refinement_of_a_recess_wider_than_the_hole_is_refused_and_nothing_is_made(self):
        parent, combo = self.parent({"pcb_width": BLIND_BOARD, "pedestal_size": 6.6})
        before = self.runs_on_disk()
        with self.assertRaises(ValueError) as caught:
            companion.create_refinement(parent, "12:13", [combo])
        self.assertIn("cannot be rebuilt here", str(caught.exception))
        self.assertEqual(self.runs_on_disk(), before)

    def test_a_refinement_of_a_bare_board_with_a_recess_is_refused(self):
        parent, combo = self.parent({"no_chip": 1.0, "pedestal_depth": 0.5})
        with self.assertRaises(ValueError) as caught:
            companion.create_refinement(parent, "12:13", [combo])
        self.assertIn("cannot be rebuilt here", str(caught.exception))

    def test_a_refinement_of_a_vented_parent_is_refused_with_the_vent_message(self):
        parent, combo = self.parent({"vents_on": 1.0})
        with self.assertRaises(ValueError) as caught:
            companion.create_refinement(parent, "12:13", [combo])
        self.assertIn("vents_on = 1", str(caught.exception))

    def test_a_field_image_of_a_vented_parent_is_refused(self):
        parent, combo = self.parent({"vents_on": 1.0})
        with mock.patch.object(companion, "field_exports_available", return_value=True):
            with self.assertRaises(ValueError) as caught:
                companion.create_field_images(parent, "15", [combo], ["E"])
        self.assertIn("vents_on = 1", str(caught.exception))

    def test_a_refinement_of_a_through_opening_with_a_recess_is_creatable(self):
        parent, combo = self.parent({})
        child = companion.create_refinement(parent, "12:13", [combo])
        self.assertTrue(os.path.isdir(child.run_dir))

    def test_a_refinement_of_a_blind_pocket_made_without_altium_is_creatable(self):
        # no copper is drawn without Altium, so nothing is sealed or cut to rebuild
        parent, combo = self.parent({"pcb_width": BLIND_BOARD}, use_altium=False)
        child = companion.create_refinement(parent, "12:13", [combo])
        self.assertTrue(os.path.isdir(child.run_dir))


class TestTheWholePlane(ChildBase):
    """A blind run built with its bottom copper whole under the die cannot be rebuilt
    with the hole, so its refinement and field image are refused."""

    def test_a_blind_run_made_since_the_change_with_copper_under_the_die_is_refused(self):
        parent, combo = self.built_parent("2026-11-09T14:35:23", WHOLE_UNDER_DIE)
        with self.assertRaises(ValueError) as caught:
            companion.create_refinement(parent, "12:13", [combo])
        self.assertEqual(str(caught.exception),
                         "This run was built with bottom copper under the die, which is not the design "
                         "(the bottom copper has a matching hole under the chip). Make a new run.")

    def test_a_field_image_of_such_a_run_is_refused_the_same_way(self):
        parent, combo = self.built_parent("2026-11-09T14:35:23", WHOLE_UNDER_DIE)
        with mock.patch.object(companion, "field_exports_available", return_value=True):
            with self.assertRaises(ValueError) as caught:
                companion.create_field_images(parent, "15", [combo], ["E"])
        self.assertEqual(str(caught.exception), companion.WHOLE_PLANE_CHILD_REFUSAL)

    def test_a_run_made_before_the_change_was_modelled_with_the_hole_and_is_creatable(self):
        # run_20261108_141226: made before 2026-11-09 14:05, so its model was cut with the hole
        parent, combo = self.built_parent("2026-11-08T14:12:26", WHOLE_UNDER_DIE)
        child = companion.create_refinement(parent, "12:13", [combo])
        self.assertTrue(os.path.isdir(child.run_dir))

    def test_a_run_built_with_the_hole_marker_is_creatable(self):
        parent, combo = self.built_parent("2026-11-09T14:35:23", WHOLE_UNDER_DIE)
        cg.mark_bottom_hole(parent.combo_dir(combo))
        child = companion.create_refinement(parent, "12:13", [combo])
        self.assertTrue(os.path.isdir(child.run_dir))

    def test_a_run_whose_board_copper_has_the_hole_is_creatable(self):
        # the Altium generator draws the hole itself: nothing lies under the die
        parent, combo = self.built_parent("2026-11-09T14:35:23", NOTHING_UNDER_DIE)
        child = companion.create_refinement(parent, "12:13", [combo])
        self.assertTrue(os.path.isdir(child.run_dir))

    def test_a_through_opening_and_a_bare_board_are_creatable(self):
        parent, combo = self.built_parent("2026-11-09T14:35:23", WHOLE_UNDER_DIE, pcb_width=0.381)
        self.assertTrue(os.path.isdir(companion.create_refinement(parent, "12:13", [combo]).run_dir))
        parent, combo = self.built_parent("2026-11-09T14:35:23", WHOLE_UNDER_DIE,
                                          no_chip=1.0, depth=0)
        self.assertTrue(os.path.isdir(companion.create_refinement(parent, "12:13", [combo]).run_dir))


class TestTheCopperHole(unittest.TestCase):
    def test_a_region_over_the_die_covers_the_centre(self):
        self.assertTrue(cg.bottom_copper_covers(cg.parse(WHOLE_UNDER_DIE)["package"], 0.0, 0.0))

    def test_copper_away_from_the_die_does_not_cover_the_centre(self):
        self.assertFalse(cg.bottom_copper_covers(cg.parse(NOTHING_UNDER_DIE)["package"], 0.0, 0.0))

    def test_a_top_layer_region_does_not_count(self):
        board = cg.parse("board=package\nregion|top|-3,-3,3,-3,3,3,-3,3\n")["package"]
        self.assertFalse(cg.bottom_copper_covers(board, 0.0, 0.0))

    def test_the_hole_marker_is_written_beside_the_model(self):
        folder = tempfile.mkdtemp(prefix="model_")
        cg.mark_bottom_hole(folder)
        self.assertTrue(os.path.isfile(os.path.join(folder, cg.HOLE_MARK)))


if __name__ == "__main__":
    unittest.main()
