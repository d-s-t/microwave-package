"""Tests for running Eigenmode and Driven Modal on the same geometry.

An HFSS design has one solution type and a queue task names one project,
one design and one solution type -- so a pair is two ordinary runs over
one sweep, linked in their configs (workflow/companion.py). What is worth
testing is the linking: that both exist, that the driven one finishes the
eigenmode one first, borrows its geometry, and reads its modes.

Run: python -m unittest discover -s tests
"""
import csv
import logging
import os
import tempfile
import unittest
from unittest import mock

import pandas as pd

from microwave_package import paths, pipeline, webapp
from microwave_package.design.sweep import SweepParameter
from microwave_package.hfss import registry
from microwave_package.workflow import companion, plotting
from microwave_package.workflow.run_manager import Run

SWEEP = [SweepParameter(name="pedestal_depth", values=[0.0, 1.0])]


def settings(**overrides):
    return {**pipeline.DEFAULT_ANALYSIS_SETTINGS, **overrides}


class RunsRoot(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)


class TestCreatingAPair(RunsRoot):
    def test_two_runs_one_of_each_type_linked_both_ways(self):
        eigen, driven = companion.create_pair(SWEEP, settings(), {})
        self.assertNotEqual(eigen.run_id, driven.run_id)   # made in the same second
        self.assertEqual(registry.solution_type_of(eigen.analysis_settings), "Eigenmode")
        self.assertEqual(registry.solution_type_of(driven.analysis_settings), "DrivenModal")
        self.assertEqual(driven.config[companion.EIGEN_LINK], eigen.run_id)
        self.assertEqual(eigen.config[companion.DRIVEN_LINK], driven.run_id)
        # and the links survive a reload
        self.assertEqual(Run.load(driven.run_id).config[companion.EIGEN_LINK], eigen.run_id)

    def test_same_sweep_so_same_combination_ids(self):
        eigen, driven = companion.create_pair(SWEEP, settings(), {})
        self.assertEqual(list(eigen.state["combos"]), list(driven.state["combos"]))

    def test_the_driven_run_is_the_one_to_start(self):
        run = pipeline.create_run(SWEEP, settings(run_both=True), {})
        self.assertEqual(registry.solution_type_of(run.analysis_settings), "DrivenModal")
        self.assertIsNotNone(companion.prerequisite_of(run))

    def test_without_run_both_it_is_one_run_of_the_chosen_type(self):
        run = pipeline.create_run(SWEEP, settings(run_both=False, solution_type="Eigenmode"), {})
        self.assertEqual(registry.solution_type_of(run.analysis_settings), "Eigenmode")
        self.assertIsNone(companion.prerequisite_of(run))
        self.assertIsNone(companion.partner_id(run))

    def test_a_pair_needs_two_ports_and_nothing_is_created_otherwise(self):
        before = set(os.listdir(paths.RUNS_ROOT))
        with self.assertRaisesRegex(ValueError, "2 ports"):
            pipeline.create_run(
                [SweepParameter(name="port_count", values=[1.0, 2.0])], settings(run_both=True), {})
        self.assertEqual(set(os.listdir(paths.RUNS_ROOT)), before)

    def test_the_cli_pairs_by_default_and_no_pair_turns_it_off(self):
        with mock.patch("sys.argv", ["pipeline"]):
            args = pipeline.parse_args()
        self.assertTrue(pipeline.build_analysis_settings(args)["run_both"])
        with mock.patch("sys.argv", ["pipeline", "--no-pair"]):
            args = pipeline.parse_args()
        self.assertFalse(pipeline.build_analysis_settings(args)["run_both"])

    def test_the_cli_carries_the_fine_window_and_box_settings(self):
        argv = ["pipeline", "--fine-windows", "off", "--fine-windows-manual", "8:9:0.1",
                "--adapt-at", "highest_mode", "--no-perfect-e-box", "--box-conductivity", "5e7"]
        with mock.patch("sys.argv", argv):
            built = pipeline.build_analysis_settings(pipeline.parse_args())
        self.assertEqual(built["fine_windows"], "off")
        self.assertEqual(built["fine_windows_manual"], "8:9:0.1")
        self.assertEqual(built["adapt_at"], "highest_mode")
        self.assertFalse(built["materials"]["perfect_e_box"])
        self.assertEqual(built["materials"]["box_conductivity"], 5e7)
        # settings the command line has no flag for still take their defaults
        self.assertEqual(built["fine_max_points"], 2000)


def write_modes(run, combo_id, rows):
    folder = run.combo_dir(combo_id)
    with open(os.path.join(folder, "results.csv"), "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["freq_ghz", "q", "combo_id"])
        writer.writeheader()
        for f, q in rows:
            writer.writerow({"freq_ghz": f, "q": q, "combo_id": combo_id})


class TestReadingTheModes(RunsRoot):
    def setUp(self):
        super().setUp()
        self.eigen, self.driven = companion.create_pair(SWEEP, settings(), {})
        self.combo = next(iter(self.eigen.state["combos"]))

    def test_modes_are_read_from_the_eigenmode_runs_results(self):
        write_modes(self.eigen, self.combo, [(12.0, 300.0), (5.0, 150.0)])
        self.assertEqual(companion.eigenmodes_for(self.driven, self.combo),
                         [(5.0, 150.0), (12.0, 300.0)])

    def test_a_blank_q_is_none_not_zero(self):
        write_modes(self.eigen, self.combo, [(5.0, "")])
        self.assertEqual(companion.eigenmodes_for(self.driven, self.combo), [(5.0, None)])

    def test_no_results_yet_is_no_modes(self):
        self.assertEqual(companion.eigenmodes_for(self.driven, self.combo), [])

    def test_an_unpaired_run_has_no_modes(self):
        lone = Run.create(SWEEP, settings(run_both=False), {})
        self.assertEqual(companion.eigenmodes_for(lone, self.combo), [])

    def test_the_driven_build_is_handed_its_combinations_modes(self):
        write_modes(self.eigen, self.combo, [(5.0, 150.0)])
        built = companion.settings_with_modes(
            self.driven, self.combo, self.driven.analysis_settings, log=lambda _m: None)
        self.assertEqual(built["eigenmodes"], [(5.0, 150.0)])
        self.assertNotIn("eigenmodes", self.driven.analysis_settings)   # not stored on the run

    def test_an_eigenmode_build_is_left_alone(self):
        same = companion.settings_with_modes(self.eigen, self.combo, self.eigen.analysis_settings)
        self.assertIs(same, self.eigen.analysis_settings)

    def test_markers_cover_the_requested_combinations_only(self):
        write_modes(self.eigen, self.combo, [(5.0, 150.0)])
        markers = companion.mode_markers(self.driven)
        self.assertEqual([(m["combo_id"], m["freq_ghz"]) for m in markers], [(self.combo, 5.0)])
        self.assertEqual(companion.mode_markers(self.driven, combo_ids=["nope"]), [])
        self.assertEqual(companion.mode_markers(self.eigen), [])


class TestBorrowingGeometry(RunsRoot):
    def setUp(self):
        super().setUp()
        self.eigen, self.driven = companion.create_pair(SWEEP, settings(), {})
        self.combo = next(iter(self.eigen.state["combos"]))
        folder = self.eigen.combo_dir(self.combo)
        step = {}
        for name in ("box", "lid_inside"):
            step[name] = os.path.join(folder, f"{self.combo}_{name}.STEP")
            with open(step[name], "w") as handle:
                handle.write(name)
        for name in ("dimensions.txt", "copper.txt", "bonds.txt"):
            with open(os.path.join(folder, name), "w") as handle:
                handle.write(name)
        # what a solve leaves behind must not travel
        with open(os.path.join(folder, "solved.aedt"), "w") as handle:
            handle.write("x")
        self.eigen.update_combo(self.combo, step_paths=step, step_status="done",
                                altium_status="done")

    def test_the_geometry_files_and_stage_records_are_carried_over(self):
        self.assertEqual(companion.seed_geometry(self.driven, self.eigen, log=lambda _m: None), 1)
        combo = self.driven.state["combos"][self.combo]
        folder = self.driven.combo_path(self.combo)
        self.assertEqual(combo["step_status"], "done")
        self.assertEqual(combo["altium_status"], "done")
        self.assertEqual(set(combo["step_paths"]), {"box", "lid_inside"})
        for path in combo["step_paths"].values():
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(os.path.dirname(path), folder)
        for name in ("dimensions.txt", "copper.txt", "bonds.txt"):
            self.assertTrue(os.path.isfile(os.path.join(folder, name)), name)
        self.assertFalse(os.path.exists(os.path.join(folder, "solved.aedt")))

    def test_it_is_done_once_and_a_second_call_changes_nothing(self):
        companion.seed_geometry(self.driven, self.eigen, log=lambda _m: None)
        self.assertEqual(companion.seed_geometry(self.driven, self.eigen, log=lambda _m: None), 0)

    def test_nothing_is_borrowed_that_was_not_built(self):
        self.eigen.update_combo(self.combo, step_status="failed")
        self.assertEqual(companion.seed_geometry(self.driven, self.eigen, log=lambda _m: None), 0)
        self.assertIsNone(self.driven.state["combos"][self.combo]["step_status"])

    def test_a_missing_step_file_means_it_is_rebuilt_not_half_borrowed(self):
        os.remove(self.eigen.state["combos"][self.combo]["step_paths"]["box"])
        self.assertEqual(companion.seed_geometry(self.driven, self.eigen, log=lambda _m: None), 0)
        self.assertIsNone(self.driven.state["combos"][self.combo]["step_status"])


class TestFinishingTheEigenmodeRunFirst(RunsRoot):
    def setUp(self):
        super().setUp()
        self.eigen, self.driven = companion.create_pair(SWEEP, settings(), {})
        self.calls = []

    def finish(self, run, *_a, **_k):
        self.calls.append(run.run_id)
        run.set_stage("complete")

    def test_the_eigenmode_run_goes_first_and_the_driven_one_then_proceeds(self):
        with mock.patch.object(pipeline, "run_pipeline", self.finish):
            self.assertTrue(pipeline._finish_prerequisite(self.driven, None, log=lambda _m: None))
        self.assertEqual(self.calls, [self.eigen.run_id])

    def test_a_finished_eigenmode_run_is_not_run_again(self):
        self.eigen.set_stage("complete")
        with mock.patch.object(pipeline, "run_pipeline", self.finish):
            self.assertTrue(pipeline._finish_prerequisite(self.driven, None, log=lambda _m: None))
        self.assertEqual(self.calls, [])

    def test_a_stop_during_the_eigenmode_run_stops_this_one_too(self):
        import threading
        stop = threading.Event()

        def stopped(run, *_a, **_k):
            stop.set()
            run.set_stage("stopped")

        with mock.patch.object(pipeline, "run_pipeline", stopped):
            self.assertFalse(pipeline._finish_prerequisite(
                self.driven, None, log=lambda _m: None, stop_event=stop))
        self.assertEqual(Run.load(self.driven.run_id).state["stage"], "stopped")

    def test_a_crashed_eigenmode_run_does_not_take_the_driven_one_down(self):
        def crash(run, *_a, **_k):
            raise RuntimeError("AEDT fell over")

        messages = []
        with mock.patch.object(pipeline, "run_pipeline", crash):
            self.assertTrue(pipeline._finish_prerequisite(self.driven, None, log=messages.append))
        self.assertEqual(Run.load(self.eigen.run_id).state["stage"], "error")
        self.assertTrue(any("no automatic fine windows" in m for m in messages))

    def test_an_unpaired_run_has_nothing_to_wait_for(self):
        lone = Run.create(SWEEP, settings(run_both=False), {})
        with mock.patch.object(pipeline, "run_pipeline", self.finish):
            self.assertTrue(pipeline._finish_prerequisite(lone, None, log=lambda _m: None))
        self.assertEqual(self.calls, [])


class TestTheWebForm(RunsRoot):
    def form(self, **fields):
        from werkzeug.datastructures import MultiDict
        return MultiDict(fields)

    def test_ticking_both_reads_both_types_settings(self):
        got = webapp._solve_settings_from_form(self.form(
            run_both="on", solution_type="Eigenmode", modes="7", sweep_points="123"))
        self.assertTrue(got["run_both"])
        self.assertEqual(got["modes"], 7)
        self.assertEqual(got["sweep_points"], 123)

    def test_without_it_only_the_chosen_types_settings_are_read(self):
        got = webapp._solve_settings_from_form(self.form(
            solution_type="Eigenmode", modes="7", sweep_points="123"))
        self.assertFalse(got["run_both"])
        self.assertEqual(got["modes"], 7)
        self.assertEqual(got["sweep_points"], 401)

    def test_the_text_field_comes_back_as_text(self):
        got = webapp._solve_settings_from_form(self.form(
            run_both="on", fine_windows_manual=" 8:8.2:0.01 "))
        self.assertEqual(got["fine_windows_manual"], "8:8.2:0.01")

    def test_the_box_conductivity_is_read_and_a_bad_one_falls_back(self):
        got = webapp._materials_from_form(self.form(box_conductivity="3e7"))
        self.assertEqual(got["box_conductivity"], 3e7)
        for bad in ("", "abc", "-1", "0"):
            got = webapp._materials_from_form(self.form(box_conductivity=bad))
            self.assertEqual(got["box_conductivity"],
                             webapp.model_builder.DEFAULT_BOX_CONDUCTIVITY, bad)

    def test_the_form_page_renders_with_the_new_controls(self):
        page = webapp.app.test_client().get("/").get_data(as_text=True)
        self.assertIn('name="run_both"', page)
        self.assertIn('name="fine_windows_manual"', page)
        self.assertIn('name="box_conductivity"', page)
        self.assertIn("exaggerates dip", page)


class TestModeMarkersOnThePlot(unittest.TestCase):
    def frame(self):
        rows = []
        for combo in ("a", "b"):
            for f in (4.0, 5.0, 6.0):
                rows.append({"combo_id": combo, "freq_ghz": f, "s21_db": -f, "s11_db": -1.0})
        return pd.DataFrame(rows)

    def markers(self, figure_html_text):
        return figure_html_text.count("eigenmode ")

    def test_modes_in_band_for_the_combinations_shown_become_markers(self):
        markers = [{"combo_id": "a", "freq_ghz": 5.0, "q": 100.0},
                   {"combo_id": "b", "freq_ghz": 5.5, "q": None},
                   {"combo_id": "a", "freq_ghz": 40.0, "q": 100.0}]       # out of band
        html = plotting.figure_html(self.frame(), [], x="freq_ghz", y="s21_db",
                                    solution_type="DrivenModal", mode_markers=markers)
        self.assertIn("Eigenmodes", html)
        self.assertEqual(self.markers(html), 2)

    def test_a_filtered_out_combination_takes_its_modes_with_it(self):
        frame = self.frame()
        frame = frame[frame["combo_id"] == "a"]
        markers = [{"combo_id": "a", "freq_ghz": 5.0, "q": 100.0},
                   {"combo_id": "b", "freq_ghz": 5.5, "q": 100.0}]
        html = plotting.figure_html(frame, [], x="freq_ghz", y="s21_db",
                                    solution_type="DrivenModal", mode_markers=markers)
        self.assertEqual(self.markers(html), 1)

    def test_no_markers_means_the_plot_is_as_it_was(self):
        html = plotting.figure_html(self.frame(), [], x="freq_ghz", y="s21_db",
                                    solution_type="DrivenModal")
        self.assertNotIn("Eigenmodes", html)

    def test_markers_are_only_for_a_curve_against_frequency(self):
        frame = self.frame()
        markers = [{"combo_id": "a", "freq_ghz": 5.0, "q": 100.0}]
        html = plotting.figure_html(frame, [], x="combo_id", y="s21_db",
                                    solution_type="DrivenModal", mode_markers=markers)
        self.assertNotIn("Eigenmodes", html)


if __name__ == "__main__":
    unittest.main()
