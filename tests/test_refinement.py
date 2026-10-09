"""Tests for refining a driven run over narrower frequency ranges, and for
viewing the broad data, the fine data, or both together.

A refinement is a new driven run on the same geometry that solves only
the chosen ranges for the chosen combinations; its rows join the parent's
in one table, told apart by the `sweep` column.

Run: python -m unittest discover -s tests
"""
import logging
import os
import tempfile
import threading
import unittest
from unittest import mock

import pandas as pd

from microwave_package import paths, pipeline, webapp
from microwave_package.design.sweep import SweepParameter
from microwave_package.hfss.driven import results as driven_results
from microwave_package.hfss.driven import setup as driven_setup
from microwave_package.hfss.driven import windows
from microwave_package.workflow import companion, plotting
from microwave_package.workflow.run_manager import Run

SWEEP = [SweepParameter(name="pedestal_depth", values=[0.0, 1.0])]


class RunsRoot(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)

    def driven_run(self, finished=True):
        run = Run.create(SWEEP, {**pipeline.DEFAULT_ANALYSIS_SETTINGS, "solution_type": "DrivenModal",
                                 "f_stop_ghz": 20.0}, {})
        if finished:
            for combo in run.state["combos"]:
                run.update_combo(combo, post_status="done")
        return run


class TestCreatingARefinement(RunsRoot):
    def setUp(self):
        super().setUp()
        self.parent = self.driven_run()
        self.combos = list(self.parent.state["combos"])

    def test_a_new_driven_run_solving_only_the_ranges_for_only_the_combinations(self):
        child = companion.create_refinement(
            self.parent, "12.1:12.4:0.002; 15:15.2:0.001", self.combos[:1])
        self.assertEqual(list(child.state["combos"]), self.combos[:1])
        s = child.analysis_settings
        self.assertEqual(s["solution_type"], "DrivenModal")
        self.assertEqual(s["coarse_sweep"], "off")
        self.assertEqual(s["fine_windows"], "off")
        self.assertEqual(windows.parse_manual(s["fine_windows_manual"]),
                         [windows.Window(12.1, 12.4, 0.002), windows.Window(15.0, 15.2, 0.001)])
        self.assertEqual((s["f_start_ghz"], s["f_stop_ghz"]), (12.1, 15.2))

    def test_it_adapts_where_the_parent_did_so_the_data_lines_up(self):
        child = companion.create_refinement(self.parent, "12:13:0.01", self.combos)
        self.assertEqual(windows.mesh_frequency_ghz(child.analysis_settings), 20.0)
        self.assertEqual(windows.mesh_frequency_ghz(
            {**child.analysis_settings, "adapt_at": "highest_mode"}, [(5.0, 1.0)]), 20.0)

    def test_the_two_runs_know_each_other_and_it_survives_a_reload(self):
        child = companion.create_refinement(self.parent, "12:13", self.combos)
        self.assertEqual(Run.load(child.run_id).config[companion.REFINES_LINK], self.parent.run_id)
        reloaded = Run.load(self.parent.run_id)
        self.assertEqual([r.run_id for r in companion.refinements_of(reloaded)], [child.run_id])
        self.assertEqual(companion.refined_parent(child).run_id, self.parent.run_id)

    def test_refining_again_adds_another_run(self):
        a = companion.create_refinement(self.parent, "12:13", self.combos)
        b = companion.create_refinement(Run.load(self.parent.run_id), "15:16", self.combos)
        self.assertNotEqual(a.run_id, b.run_id)
        self.assertEqual(len(companion.refinements_of(Run.load(self.parent.run_id))), 2)

    def test_a_deleted_refinement_is_just_not_listed(self):
        child = companion.create_refinement(self.parent, "12:13", self.combos)
        import shutil
        shutil.rmtree(child.run_dir)
        self.assertEqual(companion.refinements_of(Run.load(self.parent.run_id)), [])

    def test_the_things_that_cannot_be_refined_say_why(self):
        for ranges, combos, message in (
            ("", self.combos, "at least one frequency range"),
            ("12:13", [], "at least one combination"),
            ("12:13", ["nope"], "no results"),
            ("13:12:0.1", self.combos, "lo < hi"),
        ):
            with self.assertRaisesRegex(ValueError, message):
                companion.create_refinement(self.parent, ranges, combos)

    def test_an_unfinished_combination_cannot_be_refined(self):
        self.parent.update_combo(self.combos[0], post_status=None)
        with self.assertRaisesRegex(ValueError, "no results"):
            companion.create_refinement(Run.load(self.parent.run_id), "12:13", self.combos)

    def test_an_eigenmode_run_cannot(self):
        eigen = Run.create(SWEEP, {**pipeline.DEFAULT_ANALYSIS_SETTINGS, "solution_type": "Eigenmode"}, {})
        with self.assertRaisesRegex(ValueError, "Driven"):
            companion.create_refinement(eigen, "12:13", list(eigen.state["combos"]))


class TestStartingFromTheParentsGeometry(RunsRoot):
    def test_a_refinement_borrows_the_geometry_instead_of_rebuilding_it(self):
        parent = self.driven_run()
        combo = next(iter(parent.state["combos"]))
        folder = parent.combo_dir(combo)
        step = {}
        for name in ("box", "lid_inside"):
            step[name] = os.path.join(folder, f"{combo}_{name}.STEP")
            open(step[name], "w").write(name)
        for name in ("dimensions.txt", "copper.txt"):
            open(os.path.join(folder, name), "w").write(name)
        parent.update_combo(combo, step_paths=step, step_status="done", altium_status="done")

        child = companion.create_refinement(parent, "12:13", [combo])
        self.assertTrue(pipeline._finish_prerequisite(child, None, log=lambda _m: None))
        record = Run.load(child.run_id).state["combos"][combo]
        self.assertEqual(record["step_status"], "done")
        self.assertEqual(record["geometry_reused_from"], parent.run_id)


def sweep_frame():
    """Two combinations: a broad sweep, one in-run fine window on `a`, and
    a refinement's window on `b`."""
    rows = []
    for combo in ("a", "b"):
        for f in (4.0, 5.0, 6.0, 7.0, 8.0):
            rows.append({"combo_id": combo, "freq_ghz": f, "s21_db": -1.0, "sweep": "coarse"})
    for f in (5.4, 5.5, 5.6):
        rows.append({"combo_id": "a", "freq_ghz": f, "s21_db": -30.0, "sweep": "Fine_1"})
    for f in (6.0, 6.5, 7.0):                       # lands on two broad points
        rows.append({"combo_id": "b", "freq_ghz": f, "s21_db": -40.0, "sweep": "Fine_1",
                     "refinement": "run_x"})
    return pd.DataFrame(rows)


class TestTheThreeViews(unittest.TestCase):
    def test_broad_only_is_the_broad_sweep_alone(self):
        out = plotting.apply_view(sweep_frame(), plotting.VIEW_BROAD)
        self.assertEqual(set(out["sweep"]), {"coarse"})
        self.assertEqual(len(out), 10)

    def test_fine_only_is_just_the_fine_ranges(self):
        out = plotting.apply_view(sweep_frame(), plotting.VIEW_FINE)
        self.assertEqual(set(out["sweep"]), {"Fine_1"})
        self.assertEqual(len(out), 6)

    def test_fine_ranges_are_separate_lines_not_one_line_across_the_gap(self):
        frame = sweep_frame()
        extra = frame[frame["combo_id"] == "a"].iloc[:0].copy()
        extra.loc[0] = {"combo_id": "a", "freq_ghz": 7.4, "s21_db": -20.0, "sweep": "Fine_2"}
        out = plotting.apply_view(pd.concat([frame, extra], ignore_index=True), plotting.VIEW_FINE)
        lines_a = {line for line, combo in zip(out[plotting.LINE_COLUMN], out["combo_id"]) if combo == "a"}
        self.assertEqual(len(lines_a), 2)

    def test_both_is_one_line_per_combination_in_frequency_order(self):
        out = plotting.apply_view(sweep_frame(), plotting.VIEW_BOTH)
        for combo, part in out.groupby("combo_id"):
            self.assertEqual(part[plotting.LINE_COLUMN].nunique(), 1, combo)
            self.assertEqual(list(part["freq_ghz"]), sorted(part["freq_ghz"]), combo)

    def test_both_replaces_the_broad_points_a_fine_window_covers(self):
        out = plotting.apply_view(sweep_frame(), plotting.VIEW_BOTH)
        b = out[out["combo_id"] == "b"]
        # broad points at 6 and 7 sit inside the 6-7 window, so only the
        # fine rows are left there; 4, 5 and 8 stay
        self.assertEqual(list(b["freq_ghz"]), [4.0, 5.0, 6.0, 6.5, 7.0, 8.0])
        self.assertEqual(list(b["sweep"]).count("coarse"), 3)

    def test_a_window_of_one_combination_does_not_eat_anothers_broad_data(self):
        out = plotting.apply_view(sweep_frame(), plotting.VIEW_BOTH)
        a = out[out["combo_id"] == "a"]
        self.assertEqual(len(a), 5 + 3)                       # nothing of a's was inside 5.4-5.6
        self.assertEqual(len(out[out["combo_id"] == "b"]) + len(a), len(out))

    def test_a_run_with_no_fine_data_is_all_broad(self):
        frame = sweep_frame()
        frame = frame[frame["sweep"] == "coarse"].drop(columns=["refinement"])
        self.assertFalse(plotting.has_fine_data(frame))
        self.assertEqual(len(plotting.apply_view(frame, plotting.VIEW_BOTH)), 10)
        self.assertEqual(len(plotting.apply_view(frame, plotting.VIEW_BROAD)), 10)
        self.assertTrue(plotting.apply_view(frame, plotting.VIEW_FINE).empty)

    def test_rows_from_before_the_sweep_was_recorded_are_broad(self):
        frame = sweep_frame().drop(columns=["sweep", "refinement"])
        self.assertFalse(plotting.has_fine_data(frame))
        self.assertEqual(len(plotting.apply_view(frame, plotting.VIEW_BOTH)), len(frame))

    def test_the_figure_draws_a_line_per_group(self):
        out = plotting.apply_view(sweep_frame(), plotting.VIEW_FINE)
        html = plotting.figure_html(out, [], x="freq_ghz", y="s21_db", solution_type="DrivenModal")
        self.assertIn("scatter", html)


class TestTheParentsPlotCarriesItsRefinements(RunsRoot):
    def test_the_refinements_rows_join_the_parents_table(self):
        parent = self.driven_run()
        combo = next(iter(parent.state["combos"]))
        os.makedirs(parent.plots_dir, exist_ok=True)
        pd.DataFrame([{"combo_id": combo, "freq_ghz": f, "s21_db": -1.0, "sweep": "coarse"}
                      for f in (4.0, 8.0)]).to_csv(
            os.path.join(parent.plots_dir, plotting.RESULTS_FILENAME), index=False)
        child = companion.create_refinement(Run.load(parent.run_id), "5:6:0.5", [combo])
        os.makedirs(child.plots_dir, exist_ok=True)
        pd.DataFrame([{"combo_id": combo, "freq_ghz": f, "s21_db": -9.0, "sweep": "Fine_1"}
                      for f in (5.0, 5.5, 6.0)]).to_csv(
            os.path.join(child.plots_dir, plotting.RESULTS_FILENAME), index=False)

        table = webapp._results_frame(Run.load(parent.run_id))
        self.assertEqual(len(table), 5)
        self.assertEqual(set(table[plotting.REFINEMENT_COLUMN].dropna()), {child.run_id})
        self.assertTrue(plotting.has_fine_data(table))

        client = webapp.app.test_client()
        for view in ("broad", "fine", "both"):
            drawn = []
            real = plotting.figure_html

            def spy(df, *args, **kwargs):
                drawn.append(list(df["freq_ghz"]))
                return real(df, *args, **kwargs)

            with mock.patch.object(plotting, "figure_html", spy):
                page = client.get(f"/runs/{parent.run_id}/plot?x=freq_ghz&y=s21_db&data={view}")
            self.assertEqual(page.status_code, 200)
            self.assertEqual(drawn, [[float(f) for f in expected_x(view)]], view)

    def test_the_run_page_offers_the_views_and_the_refine_form(self):
        parent = self.driven_run()
        combo = next(iter(parent.state["combos"]))
        os.makedirs(parent.plots_dir, exist_ok=True)
        pd.DataFrame([{"combo_id": combo, "freq_ghz": 4.0, "s21_db": -1.0, "sweep": "Fine_1"}]).to_csv(
            os.path.join(parent.plots_dir, plotting.RESULTS_FILENAME), index=False)
        open(os.path.join(parent.plots_dir, plotting.PLOT_FILENAME), "w").write("<html></html>")
        page = webapp.app.test_client().get(f"/runs/{parent.run_id}").get_data(as_text=True)
        self.assertIn('id="data-view"', page)
        self.assertIn("Fine ranges only", page)
        self.assertIn('action="/runs/%s/refine"' % parent.run_id, page)
        self.assertIn('name="combo"', page)

    def test_the_refine_route_starts_a_run_and_a_bad_range_is_explained(self):
        parent = self.driven_run()
        client = webapp.app.test_client()
        bad = client.post(f"/runs/{parent.run_id}/refine", data={"ranges": "oops", "combo": "x"})
        self.assertIn("error=", bad.headers["Location"])
        combo = next(iter(parent.state["combos"]))
        with mock.patch.object(webapp, "_start_pipeline_thread") as start:
            good = client.post(f"/runs/{parent.run_id}/refine",
                               data={"ranges": "12:13:0.01", "combo": combo})
        self.assertEqual(start.call_count, 1)
        child = start.call_args[0][0]
        # the refinement belongs to the run: back to its page, not a new one
        self.assertTrue(good.headers["Location"].startswith("/runs/" + parent.run_id))
        self.assertEqual(child.parent_id, parent.run_id)


def expected_x(view):
    return {"broad": ["4.0", "8.0"], "fine": ["5.0", "5.5", "6.0"],
            "both": ["4.0", "5.0", "5.5", "6.0", "8.0"]}[view]


class FakeSetup:
    def __init__(self):
        self.props = {}

    def update(self):
        pass


class FakeHfss:
    def __init__(self):
        self.sweeps = []
        self.setup = FakeSetup()

    def create_setup(self, name):
        return self.setup

    def create_linear_count_sweep(self, **kwargs):
        self.sweeps.append(kwargs)
        return object()


BASE = {"f_start_ghz": 12.0, "f_stop_ghz": 13.0, "sweep_points": 401, "max_delta_s": 0.02,
        "max_passes": 5, "min_passes": 1, "min_converged": 1}


class TestASetupWithNoBroadSweep(unittest.TestCase):
    def test_only_the_fine_windows_are_created_and_the_mesh_adapts_where_told(self):
        hfss = FakeHfss()
        settings = {**BASE, "coarse_sweep": "off", "fine_windows": "off",
                    "fine_windows_manual": "12:12.5:0.01; 12.6:13:0.01", "adapt_ghz": 20.0}
        driven_setup.create_setup(hfss, "", settings, log=lambda _m: None)
        self.assertEqual([s["name"] for s in hfss.sweeps], ["Fine_1", "Fine_2"])
        self.assertEqual(hfss.setup.props["Frequency"], "20.0GHz")

    def test_no_broad_sweep_and_nothing_else_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no fine window"):
            driven_setup.create_setup(
                FakeHfss(), "", {**BASE, "coarse_sweep": "off", "fine_windows": "off"},
                log=lambda _m: None)

    def test_the_broad_sweep_is_still_the_default(self):
        hfss = FakeHfss()
        driven_setup.create_setup(hfss, "", dict(BASE), log=lambda _m: None)
        self.assertEqual([s["name"] for s in hfss.sweeps], ["Sweep_1"])


class TestReadingASetupWithNoBroadSweep(unittest.TestCase):
    class Setup:
        name = "Setup_1"

    class Hfss:
        def __init__(self, sweeps):
            self.existing_analysis_sweeps = sweeps

    def test_only_fine_solutions_is_fine_not_an_error(self):
        hfss = self.Hfss(["Setup_1 : LastAdaptive", "Setup_1 : Fine_1"])
        self.assertIsNone(driven_results._broad_solution_name(hfss, self.Setup()))

    def test_no_sweeps_at_all_still_says_so(self):
        with self.assertRaisesRegex(RuntimeError, "no frequency sweep"):
            driven_results._broad_solution_name(self.Hfss(["Setup_1 : LastAdaptive"]), self.Setup())

    def test_the_broad_one_is_found_beside_fine_ones(self):
        hfss = self.Hfss(["Setup_1 : Fine_1", "Setup_1 : Sweep_1"])
        self.assertEqual(driven_results._broad_solution_name(hfss, self.Setup()), "Setup_1 : Sweep_1")


class TestOneDriverPerRun(RunsRoot):
    def test_a_second_thread_cannot_drive_a_run_that_is_being_driven(self):
        run = self.driven_run(finished=False)
        self.assertTrue(pipeline._begin_driving(run.run_id))
        self.addCleanup(pipeline._end_driving, run.run_id)
        messages = []
        pipeline.run_pipeline(run, None, log=messages.append)
        self.assertTrue(any("already being driven" in m for m in messages))
        self.assertTrue(pipeline._being_driven(run.run_id))   # the first driver's claim is intact

    def test_a_driven_run_waits_for_its_eigenmode_run_when_another_thread_has_it(self):
        eigen, driven = companion.create_pair(SWEEP, dict(pipeline.DEFAULT_ANALYSIS_SETTINGS), {})
        self.assertTrue(pipeline._begin_driving(eigen.run_id))
        released = threading.Timer(0.3, lambda: (eigen.set_stage("complete"),
                                                 pipeline._end_driving(eigen.run_id)))
        released.start()
        self.addCleanup(released.cancel)
        self.addCleanup(pipeline._end_driving, eigen.run_id)
        with mock.patch.object(pipeline, "run_pipeline") as run_pipeline, \
                mock.patch.object(pipeline.time, "sleep", lambda s: threading.Event().wait(0.05)):
            self.assertTrue(pipeline._finish_prerequisite(driven, None, log=lambda _m: None))
        run_pipeline.assert_not_called()                  # it waited, it did not start a second one
        self.assertEqual(Run.load(eigen.run_id).state["stage"], "complete")


if __name__ == "__main__":
    unittest.main()
