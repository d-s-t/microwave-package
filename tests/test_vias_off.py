"""The "vias off" switch: a board with no vias at all.

Run: python -m unittest discover -s tests
"""
import logging
import re
import tempfile
import unittest
from unittest import mock

from microwave_package import paths, pipeline, webapp
from microwave_package.design import altium_generation as ag
from microwave_package.design import constraints, equations as eq, parameter_meta
from microwave_package.design.sweep import SweepParameter
from microwave_package.hfss.launch import geometry as lg
from microwave_package.workflow import companion, run_facets
from microwave_package.workflow.run_manager import Run

SWEEP = [SweepParameter(name="pedestal_depth", values=[0.0, 1.0])]


def defaults():
    return eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))


class TestTheParameter(unittest.TestCase):
    def test_it_is_a_pinned_off_by_default_boolean_in_the_equation_file(self):
        self.assertEqual(defaults()["vias_off"], 0.0)
        self.assertIn("vias_off", parameter_meta.BOOLEAN_PARAMETERS)
        self.assertEqual(parameter_meta.SWITCH_PINS["vias_off"], 0.0)
        self.assertFalse(parameter_meta.vias_are_off(defaults()))
        self.assertTrue(parameter_meta.vias_are_off({"vias_off": 1.0}))
        self.assertFalse(parameter_meta.vias_are_off({}))

    def test_the_generator_reads_it_and_skips_the_lattice(self):
        with open(paths.ALTIUM_GENERATOR, encoding="utf-8", errors="replace") as handle:
            script = handle.read()
        self.assertIn("'vias_off'", script)
        guarded = re.search(r"'vias_off', 0\.0\) > 0\.5 Then\s+Say\('vias_off=1'\)\s+Else\s+AddViaLattice",
                            script)
        self.assertIsNotNone(guarded, "AddViaLattice must sit in the Else of the vias_off test")

    def test_it_is_part_of_the_board_identity_so_a_board_with_vias_is_not_reused(self):
        on = ag.board_fingerprint({**defaults(), "vias_off": 1.0})
        off = ag.board_fingerprint({**defaults(), "vias_off": 0.0})
        self.assertIsNotNone(on)
        self.assertNotEqual(on, off)


class TestConstraints(unittest.TestCase):
    def test_a_via_grid_that_would_be_refused_is_fine_with_no_vias(self):
        values = dict(defaults(), via_min_spacing=1.0)
        self.assertIsNotNone(constraints.check_via_grid(values))
        self.assertIsNone(constraints.check_via_grid(dict(values, vias_off=1.0)))

    def test_a_fence_that_does_not_fit_is_not_refused_with_no_vias(self):
        values = dict(defaults(), pcb_side=1.0)
        self.assertIn("via fence", constraints.check_layout(values) or "")
        reason = constraints.check_layout(dict(values, vias_off=1.0)) or ""
        self.assertNotIn("via fence", reason)

    def test_the_shipped_defaults_with_no_vias_are_buildable(self):
        self.assertIsNone(constraints.check_layout(dict(defaults(), vias_off=1.0)))


class TestModelSide(unittest.TestCase):
    def test_the_launch_has_no_vias(self):
        values = defaults()
        with_vias = lg.from_values(values, gap=0.2)
        self.assertGreater(len(lg.vias(with_vias)), 0)
        self.assertEqual(lg.vias(lg.from_values(dict(values, vias_off=1.0), gap=0.2)), [])

    def test_a_board_with_no_vias_has_no_via_bodies(self):
        from microwave_package.design import copper_geometry as cg
        board = cg.BoardCopper("package")
        self.assertEqual(board.vias, [])
        self.assertEqual(len(board), 0)


class RunsRoot(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)


class TestTheForm(RunsRoot):
    def start(self, **extra):
        data = {"range_pedestal_depth": "0.5", **extra}
        with mock.patch.object(webapp, "_start_pipeline_thread") as started:
            webapp.app.test_client().post("/runs/new", data=data)
        return started.call_args[0][0]

    def test_unticked_pins_off_and_ticked_pins_on_in_the_run_config(self):
        self.assertEqual(self.start().parameter_defaults["vias_off"], 0.0)
        run = self.start(vias_off="1")
        self.assertEqual(run.parameter_defaults["vias_off"], 1.0)
        self.assertEqual(Run.load(run.run_id).parameter_defaults["vias_off"], 1.0)

    def test_the_box_is_on_the_form_unticked_and_reopens_ticked_from_a_run(self):
        client = webapp.app.test_client()
        box = re.search(r'<input type="checkbox" name="vias_off"[^>]*>', client.get("/").get_data(as_text=True), re.S)
        self.assertIsNotNone(box)
        self.assertNotIn("checked", box.group(0))
        run = self.start(vias_off="1")
        page = client.get(f"/?from_run={run.run_id}").get_data(as_text=True)
        self.assertIn("checked", re.search(r'<input type="checkbox" name="vias_off"[^>]*>', page, re.S).group(0))

    def test_the_run_page_rows_say_so(self):
        run = self.start(vias_off="1")
        inputs = {"sweep_params": [], "parameter_defaults": run.parameter_defaults}
        settings = run_facets.settings_of(inputs)
        self.assertEqual(settings[run_facets.VIAS_OFF].values, (1.0,))


class TestChildrenInherit(RunsRoot):
    def test_refinement_field_and_eigenmode_children_keep_it(self):
        parent = Run.create(SWEEP, {**pipeline.DEFAULT_ANALYSIS_SETTINGS, "solution_type": "DrivenModal",
                                    "f_stop_ghz": 20.0}, {"vias_off": 1.0})
        for combo in parent.state["combos"]:
            parent.update_combo(combo, post_status="done")
        combos = list(parent.state["combos"])
        child = companion.create_refinement(parent, "12:13:0.01", combos)
        self.assertEqual(child.parameter_defaults["vias_off"], 1.0)
        eigen = companion.create_eigenmode_part(parent, combos)
        self.assertEqual(eigen.parameter_defaults["vias_off"], 1.0)


if __name__ == "__main__":
    unittest.main()
