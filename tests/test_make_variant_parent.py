"""make_variant_parent: the standalone run it makes loads with the web app's own Run class, the lid is
refused for a variant that leaves TV0's relief channel, and a wider pocket is written when asked for.
Every run folder goes under a temporary runs root; nothing touches the repo's runs folder.

Run: python -m unittest tests.test_make_variant_parent
"""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import make_variant_parent as mvp
from microwave_package import paths
from microwave_package.design import test_lines as tl
from microwave_package.workflow.run_manager import Run

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "test_lines", "tv0_copper.txt")
DIMENSIONS = (
    '"pcb_length"= 50.0\n"pcb_side"= 25\n"pcb_width"= 0.508\n'
    '"route_width"= 1.2\t\' the lid\'s relief channel, mm\n'
    '"route_clearence"= 0.3\n"cpw_trace_width"= 0.48\n'
)


class TestMakeVariantParent(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="variant_parent_")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.runs = os.path.join(self.work, "runs")
        os.makedirs(self.runs)
        # a finished TV0 run, with placeholder STEP files (the script only copies them)
        self.source = os.path.join(self.work, "run_tv0")
        models = os.path.join(self.source, "models", "default")
        os.makedirs(models)
        os.makedirs(os.path.join(self.source, "plots"))
        shutil.copyfile(FIXTURE, os.path.join(models, "copper.txt"))
        with open(os.path.join(models, "bonds.txt"), "w", encoding="utf-8") as f:
            f.write("# wire bonds, millimetres, relative to the board centre\n# kind|pcb_x|pcb_y|chip_x|chip_y\n")
        with open(os.path.join(models, "dimensions.txt"), "w", encoding="utf-8") as f:
            f.write(DIMENSIONS)
        for name in mvp.STEP_FILES.values():
            with open(os.path.join(models, name), "wb") as f:
                f.write(b"ISO-10303-21;\n")
        with open(os.path.join(self.source, "run_config.json"), "w", encoding="utf-8") as f:
            json.dump({"sweep_params": [], "parameter_defaults": {"route_width": 1.2},
                       "analysis_settings": {"f_start_ghz": 0.1, "f_stop_ghz": 20.0},
                       "label": "TV0", "part_of": "run_parent", "part_role": "field",
                       "refines_run_id": "run_parent"}, f)
        self.copper = {}
        for name in ("TV1", "TV3"):
            folder = os.path.join(self.work, "variants", name)
            with open(FIXTURE, encoding="utf-8") as f:
                base = f.read()
            tl.write_variant(base, tl.VARIANTS[name](), os.path.join(folder, "copper.txt"))
            with open(os.path.join(folder, "variant.json"), "w", encoding="utf-8") as f:
                json.dump({"vertices": [list(p) for p in tl.VARIANTS[name]().vertices]}, f)
            self.copper[name] = os.path.join(folder, "copper.txt")

    def test_a_variant_outside_the_straight_channel_is_refused_and_nothing_is_written(self):
        with self.assertRaises(mvp.VariantRefused):
            mvp.make_parent(self.source, self.copper["TV3"], self.runs, "TV3")
        self.assertEqual(os.listdir(self.runs), [])

    def test_a_variant_inside_the_channel_is_a_standalone_run_the_web_app_loads(self):
        run_dir = mvp.make_parent(self.source, self.copper["TV1"], self.runs, "TV1: 0.30 mm step")
        run_id = os.path.basename(run_dir)
        with mock.patch.object(paths, "RUNS_ROOT", self.runs):
            run = Run.load(run_id)
        self.assertEqual(run.config["label"], "TV1: 0.30 mm step")
        for key in ("part_of", "part_role", "refines_run_id"):
            self.assertNotIn(key, run.config)
        combo = run.state["combos"]["default"]
        self.assertEqual(combo["step_status"], "done")
        self.assertEqual(combo["altium_status"], "done")
        self.assertIsNone(combo["project_path"], "no AEDT model yet, so no project is claimed")
        for path in combo["step_paths"].values():
            self.assertTrue(path.startswith(run_dir), path)
            self.assertTrue(os.path.isfile(path))
        with open(os.path.join(run_dir, "models", "default", "copper.txt"), encoding="utf-8") as f:
            self.assertIn("track|top|", f.read())

    def test_a_wider_pocket_is_written_and_the_lid_is_left_for_the_pipeline_to_rebuild(self):
        run_dir = mvp.make_parent(self.source, self.copper["TV3"], self.runs, "TV3", route_width=12.0)
        models = os.path.join(run_dir, "models", "default")
        self.assertFalse(os.path.isfile(os.path.join(models, mvp.STEP_FILES["lid_inside"])))
        with open(os.path.join(models, "dimensions.txt"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn('"route_width"= 12', text)
        with open(os.path.join(run_dir, "run_config.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["parameter_defaults"]["route_width"], 12.0)
        with open(os.path.join(run_dir, "state.json"), encoding="utf-8") as f:
            combo = json.load(f)["combos"]["default"]
        self.assertEqual(combo["step_status"], "pending")
        self.assertEqual(combo["step_paths"], {})

    def test_a_pocket_too_narrow_for_the_copper_is_refused(self):
        with self.assertRaises(mvp.VariantRefused):
            mvp.make_parent(self.source, self.copper["TV3"], self.runs, "TV3", route_width=6.0)
        self.assertEqual(os.listdir(self.runs), [])

    def test_each_call_makes_a_new_folder(self):
        first = mvp.make_parent(self.source, self.copper["TV1"], self.runs, "a")
        second = mvp.make_parent(self.source, self.copper["TV1"], self.runs, "b")
        self.assertNotEqual(first, second)
        self.assertEqual(len(os.listdir(self.runs)), 2)

    def test_the_command_line_refuses_with_a_message_and_exit_code_two(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = mvp.main([self.source, self.copper["TV3"], "--label", "TV3", "--runs-root", self.runs])
        self.assertEqual(code, 2)
        self.assertIn("perfect-E", err.getvalue())


if __name__ == "__main__":
    unittest.main()
