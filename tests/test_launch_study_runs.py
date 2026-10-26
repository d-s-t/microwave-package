"""The launch study's run creator: every combination posts through the real web
form handler and becomes the run it names. No AEDT; the pipeline thread is not
started.

Run: python -m unittest discover -s tests
"""
import json
import os
import tempfile
import unittest
from unittest import mock
from werkzeug.datastructures import MultiDict
from urllib.parse import parse_qs

from microwave_package import launch_study_runs as runs, paths, webapp
from microwave_package.workflow.run_manager import Run


class TestTheRuns(unittest.TestCase):
    def setUp(self):
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)
        patcher = mock.patch.object(webapp, "_start_pipeline_thread")
        self.started = patcher.start()
        self.addCleanup(patcher.stop)
        self.client = webapp.app.test_client()

    def post(self, run):
        response = self.client.post("/runs/new", data=MultiDict(runs.build_form(run)))
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:300])
        location = response.headers["Location"]
        self.assertNotIn("error=", location, parse_qs(location))
        return Run.load(location.split("/runs/")[1].split("?")[0].split("/")[0])

    def test_there_are_twelve_with_unique_names_and_the_gate_is_the_three_baselines(self):
        self.assertEqual(len(runs.RUNS), 12)
        self.assertEqual(len({r["name"] for r in runs.RUNS}), 12)
        self.assertEqual(runs.GATE, [r["name"] for r in runs.RUNS[:3]])

    def test_the_built_in_spec_file_is_the_one_in_the_repo(self):
        path = os.path.join(os.path.dirname(__file__), "..", "sweeps", "examples", "launch_study_runs.json")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"runs": runs.RUNS})

    def test_every_run_is_created_as_one_launch_study_combination_with_its_levers(self):
        for spec in runs.RUNS:
            run = self.post(spec)
            settings = run.analysis_settings
            self.assertEqual(settings["solution_type"], "LaunchStudy", spec["name"])
            self.assertFalse(settings["use_altium"], spec["name"])
            self.assertFalse(settings["run_both"], spec["name"])
            self.assertEqual(settings["materials"]["mat_pcb"], spec["substrate"], spec["name"])
            self.assertEqual((settings["launch_f_start_ghz"], settings["launch_f_stop_ghz"]), (2.0, 20.0))
            self.assertEqual(len(run.state["combos"]), 1, spec["name"])
            params = {**run.parameter_defaults, **next(iter(run.state["combos"].values()))["params"]}
            for name, value in spec["params"].items():
                self.assertAlmostEqual(params[name], float(value), msg=f"{spec['name']} {name}")

    def test_unset_levers_are_the_vendors_and_the_board_is_bare(self):
        run = self.post(runs.RUNS[0])
        self.assertEqual(run.parameter_defaults.get("no_chip"), 1.0)
        combo = next(iter(run.state["combos"].values()))["params"]
        for name in ("launch_taper_length", "launch_pad_width", "launch_void_radius",
                     "launch_bottom_cutout_radius"):
            self.assertEqual(combo.get(name, run.parameter_defaults.get(name, 0.0)), 0.0, name)

    def test_the_form_never_sends_the_carry_over_boxes(self):
        keys = [k for k, _v in runs.build_form(runs.RUNS[0])]
        self.assertNotIn("run_both", keys)
        self.assertNotIn("graphical", keys)
        self.assertIn("perfect_e_box", keys)

    def test_the_cli_dry_run_prints_a_form_per_run_and_posts_nothing(self):
        with mock.patch("builtins.print") as printed, mock.patch.object(runs, "post_run") as post:
            self.assertEqual(runs.main(["--dry-run", "--gate"]), 0)
        post.assert_not_called()
        self.assertEqual(printed.call_count, 3)


if __name__ == "__main__":
    unittest.main()
