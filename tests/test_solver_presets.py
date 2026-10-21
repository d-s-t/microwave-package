"""Solver presets: the five analysis settings given one name.

The two sets are pinned to the saved configs of the runs they were made
with (run_config.json in each run folder), so a change to either preset
shows up here before it can reach a sweep:

  quick      run_20261018_165006 (chip-gap-fix-77482c) and the quick depth
             sweep run_20261021_141838 (depth-sweep-h2)
  converged  run_20261020_150512 (youthful-cray-e0b220)

Run: python -m unittest discover -s tests
"""
import json
import logging
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO

from microwave_package import paths, pipeline, solver_presets
from microwave_package.design.sweep import SweepParameter
from microwave_package.hfss import registry

QUICK_FROM_RUNS = {
    "max_passes": 4, "min_passes": 1, "min_converged": 1,
    "max_delta_s": 0.05, "max_delta_f": 5.0,
}
CONVERGED_FROM_RUN = {
    "max_passes": 99, "min_passes": 3, "min_converged": 2,
    "max_delta_s": 0.02, "max_delta_f": 3.0,
}

DRIVEN_ARGS = ["--sweep", "pedestal_depth=0.1", "--solution-type", "DrivenModal", "--no-pair"]


def settings_for(*extra):
    return pipeline.build_analysis_settings(pipeline.parse_args([*DRIVEN_ARGS, *extra]))


def solver_part(settings):
    return {key: settings[key] for key in solver_presets.SOLVER_KEYS}


class PresetValues(unittest.TestCase):
    def test_quick_matches_the_quick_reference_runs(self):
        self.assertEqual(solver_presets.PRESETS["quick"], QUICK_FROM_RUNS)

    def test_converged_matches_the_converged_reference_run(self):
        self.assertEqual(solver_presets.PRESETS["converged"], CONVERGED_FROM_RUN)

    def test_registry_defaults_are_the_converged_values(self):
        defaults = registry.all_defaults()
        self.assertEqual({key: defaults[key] for key in solver_presets.SOLVER_KEYS}, CONVERGED_FROM_RUN)


class Resolve(unittest.TestCase):
    DEFAULTS = {"max_passes": 99, "min_passes": 3, "min_converged": 2, "max_delta_s": 0.02, "max_delta_f": 3.0}

    def test_no_preset_uses_the_defaults_and_overrides_nothing(self):
        values, overrides = solver_presets.resolve({}, None, self.DEFAULTS)
        self.assertEqual(values, self.DEFAULTS)
        self.assertEqual(overrides, [])

    def test_preset_fills_every_unset_setting(self):
        values, overrides = solver_presets.resolve({}, "quick", self.DEFAULTS)
        self.assertEqual(values, QUICK_FROM_RUNS)
        self.assertEqual(overrides, [])

    def test_explicit_value_beats_the_preset_and_is_recorded(self):
        values, overrides = solver_presets.resolve({"max_passes": 10}, "quick", self.DEFAULTS)
        self.assertEqual(values["max_passes"], 10)
        self.assertEqual(values["max_delta_s"], 0.05)
        self.assertEqual(overrides, ["max_passes"])

    def test_explicit_none_means_not_given(self):
        values, overrides = solver_presets.resolve({"max_passes": None}, "converged", self.DEFAULTS)
        self.assertEqual(values, CONVERGED_FROM_RUN)
        self.assertEqual(overrides, [])

    def test_unknown_preset_is_refused(self):
        with self.assertRaises(ValueError):
            solver_presets.resolve({}, "medium", self.DEFAULTS)


class CommandLine(unittest.TestCase):
    def test_no_preset_gives_the_converged_numbers_and_no_preset_name(self):
        settings = settings_for()
        self.assertEqual(solver_part(settings), CONVERGED_FROM_RUN)
        self.assertIsNone(settings["solver_preset"])
        self.assertEqual(settings["solver_overrides"], [])

    def test_quick_preset_sets_all_five_and_is_named(self):
        settings = settings_for("--preset", "quick")
        self.assertEqual(solver_part(settings), QUICK_FROM_RUNS)
        self.assertEqual(settings["solver_preset"], "quick")
        self.assertEqual(settings["solver_overrides"], [])

    def test_converged_preset_sets_all_five(self):
        settings = settings_for("--preset", "converged")
        self.assertEqual(solver_part(settings), CONVERGED_FROM_RUN)
        self.assertEqual(settings["solver_preset"], "converged")

    def test_explicit_flag_on_top_of_a_preset_is_an_override(self):
        settings = settings_for("--preset", "quick", "--max-delta-s", "0.03")
        self.assertEqual(settings["max_delta_s"], 0.03)
        self.assertEqual(settings["max_passes"], 4)
        self.assertEqual(settings["solver_overrides"], ["max_delta_s"])

    def test_setting_flag_on_top_of_a_preset_is_an_override_too(self):
        settings = settings_for("--preset", "converged", "--setting", "min_passes=2")
        self.assertEqual(settings["min_passes"], 2)
        self.assertEqual(settings["solver_overrides"], ["min_passes"])

    def test_explicit_flag_without_a_preset_is_just_the_value(self):
        settings = settings_for("--max-passes", "7")
        self.assertEqual(settings["max_passes"], 7)
        self.assertIsNone(settings["solver_preset"])
        self.assertEqual(settings["solver_overrides"], [])

    def test_unknown_preset_name_is_refused_by_the_parser(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            pipeline.parse_args([*DRIVEN_ARGS, "--preset", "medium"])

    def test_preset_with_resume_is_refused(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            pipeline.parse_args(["--resume", "run_20261021_141838", "--preset", "quick"])


class RecordedWithTheRun(unittest.TestCase):
    def test_the_preset_is_saved_in_run_config(self):
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)

        args = pipeline.parse_args([*DRIVEN_ARGS, "--preset", "quick", "--min-passes", "2"])
        run = pipeline.create_run([SweepParameter("pedestal_depth", [0.1, 0.25])],
                                  pipeline.build_analysis_settings(args), {})
        with open(os.path.join(run.run_dir, "run_config.json")) as handle:
            recorded = json.load(handle)["analysis_settings"]
        self.assertEqual(recorded["solver_preset"], "quick")
        self.assertEqual(recorded["solver_overrides"], ["min_passes"])
        self.assertEqual(recorded["max_passes"], 4)
        self.assertEqual(recorded["min_passes"], 2)


if __name__ == "__main__":
    unittest.main()
