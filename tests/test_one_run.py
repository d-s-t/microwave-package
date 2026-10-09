"""Tests for a run that solves eigenmodes AND S-parameters being ONE run.

The two solves are stored as separate runs underneath (an HFSS design has
one solution type, and a queue task names one), but to the person using the
web app there is one: one entry in the run list, one page, one results view
with the eigenmodes marked on S21. A refinement of it is part of it too.

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
from microwave_package.workflow import companion, plotting
from microwave_package.workflow.run_manager import Run

SWEEP = [SweepParameter(name="pedestal_depth", values=[0.0, 1.0])]


class OneRun(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)
        self.client = webapp.app.test_client()
        self.eigen, self.run = companion.create_pair(
            SWEEP, dict(pipeline.DEFAULT_ANALYSIS_SETTINGS), {})
        self.combo = next(iter(self.run.state["combos"]))

    def finish(self, run, post="done"):
        for combo in run.state["combos"]:
            run.update_combo(combo, post_status=post)

    def write_modes(self, rows):
        path = os.path.join(self.eigen.combo_dir(self.combo), "results.csv")
        with open(path, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["freq_ghz", "q"])
            writer.writeheader()
            writer.writerows({"freq_ghz": f, "q": q} for f, q in rows)


class TestOneEntry(OneRun):
    def test_the_run_list_has_the_run_and_not_its_eigenmode_part(self):
        self.assertEqual([r["run_id"] for r in Run.list_runs()], [self.run.run_id])

    def test_refinements_are_not_listed_either(self):
        self.finish(self.run)
        child = companion.create_refinement(Run.load(self.run.run_id), "12:13", [self.combo])
        self.assertEqual([r["run_id"] for r in Run.list_runs()], [self.run.run_id])
        self.assertEqual(child.parent_id, self.run.run_id)

    def test_the_parts_are_named_for_what_they_are(self):
        self.finish(self.run)
        child = companion.create_refinement(Run.load(self.run.run_id), "12:13", [self.combo])
        self.assertTrue(self.eigen.run_id.startswith(self.run.run_id + "_eigen"))
        self.assertTrue(child.run_id.startswith(self.run.run_id + "_fine"))

    def test_the_listed_run_is_the_driven_one_and_the_page_has_no_second_entry(self):
        self.assertEqual(self.run.analysis_settings["solution_type"], "DrivenModal")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn(f'href="/runs/{self.run.run_id}"', page)
        self.assertNotIn(self.eigen.run_id, page)

    def test_a_hidden_part_sends_you_to_its_run(self):
        reply = self.client.get(f"/runs/{self.eigen.run_id}")
        self.assertEqual(reply.status_code, 302)
        self.assertTrue(reply.headers["Location"].endswith(self.run.run_id))

    def test_the_form_starts_from_the_listed_run_not_a_hidden_part(self):
        self.assertEqual(webapp._latest_run_id(), self.run.run_id)


class TestWhileTheEigenmodesAreBeingSolved(OneRun):
    def test_the_page_and_the_list_show_that_step_as_the_runs_own_progress(self):
        self.eigen.update_combo(self.combo, altium_status="done", step_status="done")
        self.run.set_stage("eigenmodes")
        self.assertEqual(webapp._shown_run(Run.load(self.run.run_id)).run_id, self.eigen.run_id)
        data = self.client.get(f"/runs/{self.run.run_id}/status.json").get_json()
        self.assertEqual(data["stage"], "eigenmodes")
        self.assertIn("eigenmodes first", data["stage_display"]["label"])
        self.assertEqual(data["combos"][self.combo].split(",")[:2], ["done", "done"])
        row = next(r for r in Run.list_runs() if r["run_id"] == self.run.run_id)
        self.assertEqual(row["stage"], "eigenmodes")
        self.assertEqual(row["phases"][0]["done"], 1)        # the eigenmode part's layout step
        self.assertEqual(self.client.get(f"/runs/{self.run.run_id}").status_code, 200)

    def test_afterwards_it_shows_its_own(self):
        self.run.set_stage("generating_and_queuing")
        self.assertEqual(webapp._shown_run(Run.load(self.run.run_id)).run_id, self.run.run_id)

    def test_the_driven_run_says_it_is_on_its_eigenmodes_while_the_first_step_runs(self):
        seen = []

        def finish_eigen(run, *_a, **_k):
            seen.append(Run.load(self.run.run_id).state["stage"])
            run.set_stage("complete")

        with mock.patch.object(pipeline, "run_pipeline", finish_eigen):
            pipeline._finish_prerequisite(self.run, None, log=lambda _m: None)
        self.assertEqual(seen, ["eigenmodes"])


class TestOneResultsView(OneRun):
    def results(self):
        os.makedirs(self.run.plots_dir, exist_ok=True)
        pd.DataFrame([{"combo_id": self.combo, "freq_ghz": f, "s21_db": -f, "sweep": "coarse"}
                      for f in (4.0, 8.0)]).to_csv(
            os.path.join(self.run.plots_dir, plotting.RESULTS_FILENAME), index=False)
        with open(os.path.join(self.run.plots_dir, plotting.PLOT_FILENAME), "w") as handle:
            handle.write("<html></html>")

    def test_the_page_lists_the_eigenmodes_beside_the_s_parameters(self):
        self.finish(self.eigen)
        self.write_modes([(5.0, 150.0), (9.5, None)])
        self.results()
        page = self.client.get(f"/runs/{self.run.run_id}").get_data(as_text=True)
        self.assertIn("<h2>Eigenmodes</h2>", page)
        self.assertIn("5.0000", page)
        self.assertIn("9.5000", page)
        self.assertIn(f"/runs/{self.run.run_id}/eigenmodes.csv", page)
        self.assertNotIn(self.eigen.run_id, page)             # no second run to point at

    def test_the_eigenmodes_csv(self):
        self.write_modes([(5.0, 150.0)])
        body = self.client.get(f"/runs/{self.run.run_id}/eigenmodes.csv").get_data(as_text=True)
        self.assertEqual(body.split()[0], "combo_id,mode,freq_ghz,q")
        self.assertIn(f"{self.combo},1,5.0,150.0", body)

    def test_the_plot_has_the_modes_as_markers(self):
        self.finish(self.eigen)
        self.write_modes([(5.0, 150.0)])
        self.results()
        html = self.client.get(
            f"/runs/{self.run.run_id}/plot?x=freq_ghz&y=s21_db").get_data(as_text=True)
        self.assertIn("Eigenmodes", html)

    def test_the_csv_download_carries_the_refinements_rows_too(self):
        self.finish(self.run)
        self.results()
        child = companion.create_refinement(Run.load(self.run.run_id), "5:6:0.5", [self.combo])
        os.makedirs(child.plots_dir, exist_ok=True)
        pd.DataFrame([{"combo_id": self.combo, "freq_ghz": 5.5, "s21_db": -9.0,
                       "sweep": "Fine_1"}]).to_csv(
            os.path.join(child.plots_dir, plotting.RESULTS_FILENAME), index=False)
        body = self.client.get(f"/runs/{self.run.run_id}/results.csv").get_data(as_text=True)
        self.assertEqual(len(body.strip().splitlines()), 1 + 3)
        self.assertIn("Fine_1", body)


class TestRefiningInTheSameRun(OneRun):
    def test_a_refinement_is_listed_on_the_runs_page_and_nowhere_else(self):
        self.finish(self.run)
        os.makedirs(self.run.plots_dir, exist_ok=True)
        pd.DataFrame([{"combo_id": self.combo, "freq_ghz": 4.0, "s21_db": -1.0,
                       "sweep": "coarse"}]).to_csv(
            os.path.join(self.run.plots_dir, plotting.RESULTS_FILENAME), index=False)
        open(os.path.join(self.run.plots_dir, plotting.PLOT_FILENAME), "w").close()
        child = companion.create_refinement(Run.load(self.run.run_id), "12:13:0.01", [self.combo])
        page = self.client.get(f"/runs/{self.run.run_id}").get_data(as_text=True)
        self.assertIn('class="refinements"', page)
        self.assertIn("12:13:0.01", page)
        self.assertIn(f"/runs/{child.run_id}/resume", page)       # controls, not a destination
        self.assertNotIn(f'href="/runs/{child.run_id}"', page)
        self.assertNotIn(child.run_id, self.client.get("/").get_data(as_text=True))

    def test_around_the_modes_fills_windows_twice_as_dense_as_the_automatic_ones(self):
        self.finish(self.run)
        self.write_modes([(5.0, 200.0), (12.0, 300.0), (30.0, 100.0)])
        run = Run.load(self.run.run_id)
        everything = companion.mode_ranges(run, [self.combo])
        self.assertEqual(len(everything.split(";")), 2)            # 30 GHz is above the sweep stop
        only_first = companion.mode_ranges(run, [self.combo], 4.0, 6.0)
        lo, hi, step = (float(x) for x in only_first.split(":"))
        self.assertLess(lo, 5.0)
        self.assertGreater(hi, 5.0)
        self.assertAlmostEqual(step, (5.0 / 200.0) / 40.0, places=4)   # f/(40Q): the automatic f/(20Q), halved
        self.assertEqual(companion.mode_ranges(run, [self.combo], 20.0, 21.0), "")

    def test_the_ranges_endpoint_serves_it_for_the_form(self):
        self.finish(self.run)
        self.write_modes([(5.0, 200.0)])
        body = self.client.get(
            f"/runs/{self.run.run_id}/mode-ranges.json?lo=4&hi=6&combo={self.combo}").get_json()
        self.assertEqual(len(body["ranges"].split(":")), 3)
        empty = self.client.get(f"/runs/{self.run.run_id}/mode-ranges.json?lo=50&hi=60").get_json()
        self.assertEqual(empty["ranges"], "")


class TestDeletingTakesTheParts(OneRun):
    def test_the_eigenmode_part_and_the_refinements_go_with_the_run(self):
        self.finish(self.run)
        child = companion.create_refinement(Run.load(self.run.run_id), "12:13", [self.combo])
        queue = tempfile.mkdtemp(prefix="queue_")
        with mock.patch.dict(os.environ, {"ANSYS_ANALYZE_QUEUE_PATH": queue}):
            self.client.post(f"/runs/{self.run.run_id}/delete",
                             data={"confirm_run_id": self.run.run_id})
        for gone in (self.run, self.eigen, child):
            self.assertFalse(os.path.exists(gone.run_dir), gone.run_id)


if __name__ == "__main__":
    unittest.main()
