"""The recess rule: a pedestal recess under copper that stays whole is refused.

A recess in the box floor under the die is a sealed air void when the board's
bottom copper lies over it unchanged: a blind pocket, or a bare board (no
opening at all). A through opening takes the copper out over the recess and is
allowed. A run with such a combination is refused when it is made, whole; the
refinement and field parts made from a parent reproduce its geometry, so they
are not refused. Nothing opens Altium, SolidWorks or AEDT.

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


# 0.127 mm over the 0.381 mm die: the die's opening stays blind.
BLIND_BOARD = 0.508


def values(**overrides):
    base = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    return {**base, **overrides}


def sweep(name, *vals):
    return SweepParameter(name=name, values=list(vals))


class TestTheRule(unittest.TestCase):
    def test_the_committed_defaults_do_not_trip_it(self):
        # default.txt: a 0.381 mm board over the 0.381 mm die (a through opening), 0.5 mm recess
        self.assertIsNone(constraints.check_recess_copper(values()))

    def test_a_through_opening_keeps_its_recess(self):
        self.assertIsNone(constraints.check_recess_copper(values(pedestal_depth=0.5)))

    def test_a_board_within_the_floor_rule_of_the_die_is_through(self):
        # 0.069 mm more than the die, under MIN_FLOOR (0.1 mm): the opening goes through
        self.assertIsNone(constraints.check_recess_copper(values(pedestal_depth=0.5, pcb_width=0.45)))

    def test_a_blind_pocket_with_a_recess_is_refused_in_plain_words(self):
        reason = constraints.check_recess_copper(values(pedestal_depth=0.5, pcb_width=BLIND_BOARD))
        self.assertIn("pedestal_depth 0.5 mm", reason)
        self.assertIn("set pedestal_depth to 0", reason)
        self.assertIn("0.1 mm thicker than the die", reason)

    def test_a_blind_pocket_without_a_recess_is_fine(self):
        self.assertIsNone(constraints.check_recess_copper(values(pedestal_depth=0, pcb_width=BLIND_BOARD)))

    def test_a_bare_board_with_a_typed_recess_is_refused(self):
        reason = constraints.check_recess_copper(values(no_chip=1, pedestal_depth=0.5))
        self.assertIn("bare board", reason)
        self.assertIn("set pedestal_depth to 0", reason)
        # a thicker board does not help a bare one: it has no opening
        self.assertNotIn("thicker than the die", reason)

    def test_a_bare_board_without_a_recess_is_fine(self):
        self.assertIsNone(constraints.check_recess_copper(values(no_chip=1, pedestal_depth=0)))

    def test_the_square_and_the_circle_are_both_refused(self):
        for shape in (0, 1):
            with self.subTest(shape=shape):
                self.assertIsNotNone(constraints.check_recess_copper(
                    values(pedestal_depth=0.5, pcb_width=BLIND_BOARD, pedestal_shape=shape)))

    def test_the_launch_cutout_neither_exempts_a_die_recess_nor_is_refused(self):
        # the cutout is in the copper under the launch, not under the die
        self.assertIsNotNone(constraints.check_recess_copper(
            values(pedestal_depth=0.5, pcb_width=BLIND_BOARD, launch_bottom_cutout_radius=1.0)))
        self.assertIsNone(constraints.check_recess_copper(
            values(pedestal_depth=0, pcb_width=BLIND_BOARD, launch_bottom_cutout_radius=1.0)))
        self.assertIsNone(constraints.check_recess_copper(
            values(pedestal_depth=0.5, launch_bottom_cutout_radius=1.0)))


class TestThePedestalVent(unittest.TestCase):
    def test_vents_with_a_pedestal_recess_under_a_through_opening_are_refused(self):
        reason = constraints.check_pedestal_vent(values(vents_on=1, pedestal_depth=0.5))
        self.assertIn("untick vents_on", reason)
        self.assertIn("Vents are cut after manufacturing", reason)

    def test_vents_off_are_not_refused(self):
        self.assertIsNone(constraints.check_pedestal_vent(values(vents_on=0, pedestal_depth=0.5)))

    def test_no_pedestal_recess_means_no_pedestal_vent(self):
        self.assertIsNone(constraints.check_pedestal_vent(values(vents_on=1, pedestal_depth=0)))

    def test_a_blind_pocket_is_left_to_the_recess_rule(self):
        self.assertIsNone(constraints.check_pedestal_vent(
            values(vents_on=1, pedestal_depth=0.5, pcb_width=BLIND_BOARD)))

    def test_a_bare_board_has_no_pedestal_vent(self):
        self.assertIsNone(constraints.check_pedestal_vent(values(vents_on=1, no_chip=1, pedestal_depth=0.5)))


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

    def test_the_copper_cut_reaches_the_bottom_face_exactly_when_the_rule_says_so(self):
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
                # the cut starts below the bottom face only for a through opening
                cuts = origin[1] < self.stack.pcb_bottom - 1e-6
                self.assertEqual(cuts, opening.cuts_bottom_copper)
                self.assertEqual(cuts, kind == "through")


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

    def test_a_blind_pocket_recess_is_refused_and_no_folder_is_made(self):
        with self.assertRaises(ValueError) as caught:
            pipeline.create_run([sweep("pedestal_depth", 0.5)], self.settings(run_both=False),
                                {"pcb_width": BLIND_BOARD})
        self.assertIn("Run not created", str(caught.exception))
        self.assertIn("pedestal_depth 0.5 mm", str(caught.exception))
        self.assertEqual(self.runs_on_disk(), [])

    def test_a_pair_is_refused_whole_and_neither_half_is_made(self):
        # run_both (the default) makes an Eigenmode and a Driven run on one geometry
        self.assertTrue(self.settings()["run_both"])
        with self.assertRaises(ValueError):
            pipeline.create_run([sweep("pedestal_depth", 0.5)], self.settings(), {"pcb_width": BLIND_BOARD})
        self.assertEqual(self.runs_on_disk(), [])

    def test_a_bare_board_with_a_typed_recess_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            pipeline.create_run([], self.settings(), {"no_chip": 1.0, "pedestal_depth": 0.5})
        self.assertIn("bare board", str(caught.exception))
        self.assertEqual(self.runs_on_disk(), [])

    def test_a_refusal_names_the_combinations_it_refuses_and_refuses_the_whole_run(self):
        with self.assertRaises(ValueError) as caught:
            pipeline.create_run([sweep("pcb_width", 0.381, BLIND_BOARD), sweep("pedestal_depth", 0.5)],
                                self.settings(run_both=False), {})
        message = str(caught.exception)
        self.assertIn("1 of 2 combinations are refused", message)
        self.assertIn("pcb_width = 0.508", message)
        self.assertNotIn("pcb_width = 0.381", message)
        self.assertEqual(self.runs_on_disk(), [])

    def test_a_through_opening_with_a_recess_is_created(self):
        run = pipeline.create_run([sweep("pedestal_depth", 0.5)], self.settings(), {})
        self.assertIsInstance(run, Run)
        self.assertTrue(os.path.isdir(run.run_dir))

    def test_without_altium_no_copper_is_drawn_so_nothing_is_sealed(self):
        run = pipeline.create_run([sweep("pedestal_depth", 0.5)],
                                  self.settings(run_both=False, use_altium=False),
                                  {"pcb_width": BLIND_BOARD})
        self.assertTrue(os.path.isdir(run.run_dir))

    def test_vents_with_a_pedestal_recess_under_a_through_opening_are_refused_at_creation(self):
        with self.assertRaises(ValueError) as caught:
            pipeline.create_run([sweep("pedestal_depth", 0.5)], self.settings(run_both=False),
                                {"vents_on": 1.0})
        self.assertIn("vents_on = 1", str(caught.exception))
        self.assertIn("untick vents_on", str(caught.exception))
        self.assertEqual(self.runs_on_disk(), [])

    def test_vents_without_a_pedestal_recess_are_not_refused_here(self):
        run = pipeline.create_run([], self.settings(run_both=False),
                                  {"vents_on": 1.0, "pedestal_depth": 0.0})
        self.assertTrue(os.path.isdir(run.run_dir))


class TestTheChildren(unittest.TestCase):
    """Refinement and field parts are made from the parent's geometry, so they are refused
    where the builder cannot close it (companion._refuse_unbuildable_geometry)."""

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)

    def parent(self, defaults, **settings):
        """A driven run made the way the web app makes one, with its one combination done."""
        _eigen, run = companion.create_pair([sweep("pedestal_depth", 0.5)],
                                            {**pipeline.DEFAULT_ANALYSIS_SETTINGS, **settings}, defaults)
        combo = next(iter(run.state["combos"]))
        run.update_combo(combo, post_status="done")
        return Run.load(run.run_id), combo

    def runs_on_disk(self):
        return sorted(os.listdir(paths.RUNS_ROOT))

    def test_a_refinement_of_a_blind_pocket_recess_is_refused_and_nothing_is_made(self):
        parent, combo = self.parent({"pcb_width": BLIND_BOARD})
        before = self.runs_on_disk()
        with self.assertRaises(ValueError) as caught:
            companion.create_refinement(parent, "12:13", [combo])
        self.assertIn("cannot be rebuilt here", str(caught.exception))
        self.assertIn("PORT=5001", str(caught.exception))
        self.assertEqual(self.runs_on_disk(), before)

    def test_a_field_image_of_a_blind_pocket_recess_is_refused(self):
        parent, combo = self.parent({"pcb_width": BLIND_BOARD})
        with mock.patch.object(companion, "field_exports_available", return_value=True):
            with self.assertRaises(ValueError) as caught:
                companion.create_field_images(parent, "15", [combo], ["E"])
        self.assertIn("cannot be rebuilt here", str(caught.exception))

    def test_a_refinement_of_a_bare_board_with_a_recess_is_refused(self):
        parent, combo = self.parent({"no_chip": 1.0, "pedestal_depth": 0.5})
        with self.assertRaises(ValueError) as caught:
            companion.create_refinement(parent, "12:13", [combo])
        self.assertIn("cannot be rebuilt here", str(caught.exception))

    def test_a_refinement_of_a_vented_through_parent_is_refused_with_the_vent_message(self):
        parent, combo = self.parent({"vents_on": 1.0})
        with self.assertRaises(ValueError) as caught:
            companion.create_refinement(parent, "12:13", [combo])
        self.assertIn("vents_on = 1", str(caught.exception))

    def test_a_field_image_of_a_vented_through_parent_is_refused(self):
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
        # no copper is drawn without Altium, so nothing is sealed to rebuild
        parent, combo = self.parent({"pcb_width": BLIND_BOARD}, use_altium=False)
        child = companion.create_refinement(parent, "12:13", [combo])
        self.assertTrue(os.path.isdir(child.run_dir))


if __name__ == "__main__":
    unittest.main()
