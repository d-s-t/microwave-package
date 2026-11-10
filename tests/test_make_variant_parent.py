"""make_variant_parent: the standalone run it makes loads with the web app's own Run class; the lid is
copied only for a variant inside the parent's relief channel; a wider pocket is written when asked for;
and the parent must be the variant's straight reference outside the section.
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


def _dimensions(route_width: float) -> str:
    return (
        '"pcb_length"= 50.0\n"pcb_side"= 25\n"pcb_width"= 0.508\n'
        f'"route_width"= {route_width:g}\t\' the lid\'s relief channel, mm\'\n'
        '"route_clearence"= 0.3\n"cpw_trace_width"= 0.48\n'
    )


class TestMakeVariantParent(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="variant_parent_")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.runs = os.path.join(self.work, "runs")
        os.makedirs(self.runs)
        with open(FIXTURE, encoding="utf-8") as f:
            self.tv0_text = f.read()
        self.tv0 = self._parent("run_tv0", self.tv0_text, 1.2)  # TV0: the 1.2 mm channel
        self.pocket = self._parent("run_tv0b", self.tv0_text, 2.7)  # TV0b: the 2.7 mm pocket
        self.copper = {}
        for name in ("TV1", "TV3"):
            folder = os.path.join(self.work, "variants", name)
            tl.write_variant(self.tv0_text, tl.VARIANTS[name](), os.path.join(folder, "copper.txt"))
            with open(os.path.join(folder, "variant.json"), "w", encoding="utf-8") as f:
                json.dump({"vertices": [list(p) for p in tl.VARIANTS[name]().vertices]}, f)
            self.copper[name] = os.path.join(folder, "copper.txt")

    def _parent(self, name, copper_text, route_width):
        """A finished bare-board run, with placeholder STEP files (the script only copies them)."""
        run = os.path.join(self.work, name)
        models = os.path.join(run, "models", "default")
        os.makedirs(models)
        os.makedirs(os.path.join(run, "plots"))
        with open(os.path.join(models, "copper.txt"), "w", encoding="utf-8") as f:
            f.write(copper_text)
        with open(os.path.join(models, "bonds.txt"), "w", encoding="utf-8") as f:
            f.write("# wire bonds, millimetres, relative to the board centre\n# kind|pcb_x|pcb_y|chip_x|chip_y\n")
        with open(os.path.join(models, "dimensions.txt"), "w", encoding="utf-8") as f:
            f.write(_dimensions(route_width))
        for step in mvp.STEP_FILES.values():
            with open(os.path.join(models, step), "wb") as f:
                f.write(b"ISO-10303-21;\n")
        with open(os.path.join(run, "run_config.json"), "w", encoding="utf-8") as f:
            json.dump({"sweep_params": [], "parameter_defaults": {"route_width": route_width},
                       "analysis_settings": {"f_start_ghz": 0.1, "f_stop_ghz": 20.0},
                       "label": name, "part_of": "run_parent", "part_role": "field",
                       "refines_run_id": "run_parent"}, f)
        return run

    def test_a_routed_variant_is_refused_on_tv0s_channel_and_nothing_is_written(self):
        with self.assertRaises(mvp.VariantRefused):
            mvp.make_parent(self.tv0, self.copper["TV3"], self.runs, "TV3 on TV0")
        self.assertEqual(os.listdir(self.runs), [])

    def test_a_routed_variant_on_the_2_7_mm_pocket_parent_gets_its_lid_and_loads(self):
        run_dir = mvp.make_parent(self.pocket, self.copper["TV3"], self.runs, "TV3: matched stack and wave")
        run_id = os.path.basename(run_dir)
        with mock.patch.object(paths, "RUNS_ROOT", self.runs):
            run = Run.load(run_id)
        self.assertEqual(run.config["label"], "TV3: matched stack and wave")
        for key in ("part_of", "part_role", "refines_run_id"):
            self.assertNotIn(key, run.config)
        self.assertEqual(run.config["parameter_defaults"]["route_width"], 2.7, "the parent's pocket is kept")
        combo = run.state["combos"]["default"]
        self.assertEqual(combo["step_status"], "done")
        self.assertEqual(combo["altium_status"], "done")
        self.assertIsNone(combo["project_path"], "no AEDT model yet, so no project is claimed")
        self.assertEqual(set(combo["step_paths"]), set(mvp.STEP_FILES))
        for path in combo["step_paths"].values():
            self.assertTrue(path.startswith(run_dir), path)
            self.assertTrue(os.path.isfile(path))

    def test_a_variant_inside_the_channel_is_a_standalone_run(self):
        run_dir = mvp.make_parent(self.tv0, self.copper["TV1"], self.runs, "TV1: 0.30 mm step")
        with mock.patch.object(paths, "RUNS_ROOT", self.runs):
            run = Run.load(os.path.basename(run_dir))
        self.assertEqual(run.config["label"], "TV1: 0.30 mm step")
        self.assertEqual(run.state["combos"]["default"]["step_status"], "done")

    def test_a_wider_pocket_is_written_and_the_lid_is_left_for_the_pipeline_to_rebuild(self):
        run_dir = mvp.make_parent(self.tv0, self.copper["TV3"], self.runs, "TV3 at 2.7", route_width=2.7)
        models = os.path.join(run_dir, "models", "default")
        self.assertFalse(os.path.isfile(os.path.join(models, mvp.STEP_FILES["lid_inside"])))
        with open(os.path.join(models, "dimensions.txt"), encoding="utf-8") as f:
            self.assertIn('"route_width"= 2.7', f.read())
        with open(os.path.join(run_dir, "run_config.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["parameter_defaults"]["route_width"], 2.7)
        with open(os.path.join(run_dir, "state.json"), encoding="utf-8") as f:
            combo = json.load(f)["combos"]["default"]
        self.assertEqual(combo["step_status"], "pending")
        self.assertEqual(combo["step_paths"], {})

    def test_a_pocket_too_narrow_for_the_copper_is_refused(self):
        with self.assertRaises(mvp.VariantRefused):
            mvp.make_parent(self.tv0, self.copper["TV3"], self.runs, "TV3 at 2.0", route_width=2.0)
        self.assertEqual(os.listdir(self.runs), [])

    def test_a_parent_that_differs_outside_the_section_is_not_the_reference(self):
        changed = self.tv0_text.replace("pad|top|14.4|-2|0.5|0.5|90", "pad|top|14.4|-2|0.6|0.5|90")
        other = self._parent("run_other", changed, 2.7)
        with self.assertRaises(mvp.VariantRefused) as ctx:
            mvp.make_parent(other, self.copper["TV3"], self.runs, "TV3 on a different board")
        self.assertIn("differential reference", str(ctx.exception))
        self.assertEqual(os.listdir(self.runs), [])

    def test_each_call_makes_a_new_folder(self):
        first = mvp.make_parent(self.tv0, self.copper["TV1"], self.runs, "a")
        second = mvp.make_parent(self.tv0, self.copper["TV1"], self.runs, "b")
        self.assertNotEqual(first, second)
        self.assertEqual(len(os.listdir(self.runs)), 2)

    def test_the_command_line_takes_the_parent_and_refuses_with_a_message_and_exit_code_two(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = mvp.main([self.tv0, self.copper["TV3"], "--label", "TV3", "--runs-root", self.runs])
        self.assertEqual(code, 2)
        self.assertIn("perfect-E", err.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mvp.main([self.pocket, self.copper["TV3"], "--label", "TV3", "--runs-root", self.runs])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isdir(out.getvalue().strip()))


if __name__ == "__main__":
    unittest.main()
