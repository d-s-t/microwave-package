"""Tests for the solver verdict on the web app's pages: the run list's badge and
filter, /runs.json, the run page's "Solver status" block and banner, and a refined
solve's own row and its place in the refine list.

The verdicts come from solver profiles written into a temporary runs folder (see
test_solver_status.py for the rules); these tests check what the pages show.

Run: python -m unittest discover -s tests
"""
import json
import logging
import os
import re
import shutil
import tempfile
import unittest

from microwave_package import paths, webapp
from microwave_package.workflow import plotting, solver_status

# The footnote and basis lines HFSS writes (escaped quotes and all).
OK_TEXT = (
    "\t\t\tProfileFootnote('I(1, 0, \\'Adaptive Passes converged\\')', 0)\n"
    "\t\t\t\tProfileItem('', 0, 0, 0, 0, 0, 'I(1, 0, \\'Basis Element # 1, Frequency: 2GHz; "
    "S Matrix Error =   0.072%\\')', false, true)\n"
    "\t\t\tProfileFootnote('I(1, 0, \\'Interpolating sweep converged and is passive\\')', 0)\n"
    "\t\t\tProfileFootnote('I(2, 1, \\'Stop Time\\', \\'10/26/2026 16:19:17\\', 1, \\'Status\\', "
    "\\'Normal Completion\\')', 0)\n")
WARNING_TEXT = (
    "\t\t\tProfileFootnote('I(1, 0, \\'Adaptive Passes did not converge\\')', 1)\n"
    "\t\t\tProfileFootnote('I(1, 0, \\'Interpolating sweep converged and is passive\\')', 0)\n"
    "\t\t\tProfileFootnote('I(2, 1, \\'Stop Time\\', \\'10/26/2026 16:19:17\\', 1, \\'Status\\', "
    "\\'Normal Completion\\')', 0)\n")
FAILED_TEXT = (
    "\t\t\t\tProfileItem('', 0, 0, 0, 0, 0, 'I(1, 0, \\'Basis Element # 3, Frequency: 11GHz; "
    "S Matrix Error =  91.501%\\')', false, true)\n"
    "\t\t\t\tProfileItem('', 0, 0, 0, 0, 0, 'I(1, 0, \\'Basis Element # 17, Frequency: 10.4375GHz; "
    "S Matrix Error = 100.918%\\')', false, true)\n"
    "\t\t\tProfileFootnote('I(1, 0, \\'Interpolating sweep did NOT converge\\')', 0)\n"
    "\t\t\tProfileFootnote('I(1, 0, \\'Sweep Simulation FAILED\\')', 0)\n"
    "\t\t\tProfileFootnote('I(2, 1, \\'Stop Time\\', \\'10/26/2026 16:19:17\\', 1, \\'Status\\', "
    "\\'Engine Detected Error\\')', 2)\n")
BANNER = ("The frequency sweep did not converge: values between the solved frequencies are unreliable "
          "(S11 or S21 above 0 dB are numerical errors). Re-run it, or refine the ranges.")
BADGE = "Sweep did not converge"
GOOD_CSV = "freq_ghz,s11_db,s21_db,s12_db,s22_db\n2,-10,-1,-1,-10\n3,-10,-3,-3,-10\n"
BAD_CSV = "freq_ghz,s11_db,s21_db,s12_db,s22_db\n2,0.5,-1,-1,-10\n3,-0.5,-0.5,-0.5,-0.5\n"


def write_run(root, run_id, config, state, profiles=None, results=None, plot=False):
    """A run folder: its config and state, and for each combination its profile
    text and results table (`profiles` and `results` map a combination to text)."""
    run_dir = os.path.join(root, run_id)
    os.makedirs(os.path.join(run_dir, "plots"), exist_ok=True)
    with open(os.path.join(run_dir, "run_config.json"), "w", encoding="utf-8") as handle:
        json.dump(config, handle)
    with open(os.path.join(run_dir, "state.json"), "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    for combo, text in (profiles or {}).items():
        folder = os.path.join(run_dir, "models", combo, f"{run_id}__{combo}.aedtresults", "HFSS_ABC.results")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "DV1_S1_V0.profile"), "w", encoding="utf-8") as handle:
            handle.write(text)
    for combo, text in (results or {}).items():
        folder = os.path.join(run_dir, "models", combo)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "results.csv"), "w", encoding="utf-8") as handle:
            handle.write(text)
    if plot:
        with open(os.path.join(run_dir, "plots", plotting.PLOT_FILENAME), "w", encoding="utf-8") as handle:
            handle.write("<html></html>")
    return run_dir


def analysis(solution_type="DrivenModal", **extra):
    return {"solution_type": solution_type, "materials": {"mat_pcb": "Rogers TMM 10i (tm)"}, **extra}


def done_state(*combos):
    return {"stage": "complete", "combos": {name: {"params": {}, "post_status": "done"} for name in combos}}


def row_html(html, run_id):
    """The whole table row of one run on the home page (its own row, not its parts')."""
    start = html.index(f'data-run-id="{run_id}"')
    start = html.rindex("<tr", 0, start)
    return html[start:html.index("</tr>", start) + len("</tr>")]


def first_pill(row):
    return re.search(r'<span class="pill[^"]*">([^<]*)</span>', row).group(1)


class SolverPages(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.root = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(shutil.rmtree, self.root, True)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = self.root
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)
        solver_status.clear_caches()
        self.addCleanup(solver_status.clear_caches)
        self.client = webapp.app.test_client()

    def home(self):
        return self.client.get("/").get_data(as_text=True)

    def entry(self, run_id):
        return next(e for e in self.client.get("/runs.json").get_json() if e["run_id"] == run_id)


class TheRunList(SolverPages):
    def test_a_failed_run_is_badged_and_its_stage_pill_is_still_the_first_pill(self):
        write_run(self.root, "run_20261026_121722", {"analysis_settings": analysis()},
                  done_state("default"), profiles={"default": FAILED_TEXT}, results={"default": BAD_CSV})
        row = row_html(self.home(), "run_20261026_121722")
        self.assertIn(f'<span class="solver-badge"', row)
        self.assertIn(BADGE, row)
        self.assertEqual(first_pill(row), "Complete")  # the badge is not a pill, so the poll still paints the stage
        self.assertIn('data-stage="complete unconverged"', row)

    def test_a_warning_run_gets_no_badge_in_the_list(self):
        write_run(self.root, "run_20261025_142208", {"analysis_settings": analysis()},
                  done_state("default"), profiles={"default": WARNING_TEXT}, results={"default": GOOD_CSV})
        row = row_html(self.home(), "run_20261025_142208")
        self.assertNotIn("solver-badge", row)
        self.assertNotIn("unconverged", row)

    def test_an_ok_run_has_neither_badge_nor_filter_word(self):
        write_run(self.root, "run_20261025_141347", {"analysis_settings": analysis()},
                  done_state("default"), profiles={"default": OK_TEXT}, results={"default": GOOD_CSV})
        row = row_html(self.home(), "run_20261025_141347")
        self.assertNotIn("solver-badge", row)
        self.assertIn('data-stage="complete"', row)

    def test_the_stage_filter_offers_sweep_not_converged(self):
        write_run(self.root, "run_20261025_141347", {"analysis_settings": analysis()}, done_state("default"))
        self.assertIn('<option value="unconverged">Sweep not converged</option>', self.home())

    def test_runs_json_carries_each_runs_verdict(self):
        write_run(self.root, "run_20261025_141347", {"analysis_settings": analysis()},
                  done_state("default"), profiles={"default": OK_TEXT}, results={"default": GOOD_CSV})
        write_run(self.root, "run_20261026_121722", {"analysis_settings": analysis()},
                  done_state("default"), profiles={"default": FAILED_TEXT}, results={"default": BAD_CSV})
        write_run(self.root, "run_20261001_120000", {"analysis_settings": analysis()}, done_state("default"))
        self.assertEqual(self.entry("run_20261025_141347")["solver_status"], "ok")
        self.assertEqual(self.entry("run_20261026_121722")["solver_status"], "failed")
        self.assertEqual(self.entry("run_20261001_120000")["solver_status"], "unknown")

    def test_an_eigenmode_part_that_failed_does_not_fail_its_run(self):
        write_run(self.root, "run_20261025_141347", {
            "eigen_run_id": "run_20261025_141347_eigen", "analysis_settings": analysis()},
            done_state("default"), profiles={"default": OK_TEXT}, results={"default": GOOD_CSV})
        write_run(self.root, "run_20261025_141347_eigen", {
            "part_of": "run_20261025_141347", "part_role": "eigen", "analysis_settings": analysis("Eigenmode")},
            done_state("default"), profiles={"default": FAILED_TEXT})
        self.assertEqual(self.entry("run_20261025_141347")["solver_status"], "ok")
        self.assertNotIn("solver-badge", row_html(self.home(), "run_20261025_141347"))

    def test_a_refined_solve_with_a_failed_sweep_has_a_row_with_the_badge(self):
        parent = "run_20261026_122222"
        child = parent + "_fine1"
        write_run(self.root, parent, {"refined_by": [child], "analysis_settings": analysis()},
                  done_state("default"), profiles={"default": OK_TEXT}, results={"default": GOOD_CSV})
        write_run(self.root, child, {
            "part_of": parent, "part_role": "fine1", "refines_run_id": parent,
            "analysis_settings": {**analysis(), "fine_windows_manual": "12.1:12.4:0.002"}},
            {"stage": "monitoring", "combos": {"default": {
                "model_status": "built", "queue_status": "in_progress", "params": {}}}},
            profiles={"default": FAILED_TEXT}, results={"default": BAD_CSV})
        html = self.home()
        part_rows = re.findall(r'<tr class="part-row".*?</tr>', html, re.S)
        self.assertEqual(len(part_rows), 1)
        self.assertIn(BADGE, part_rows[0])
        self.assertIn("refined solve", part_rows[0])
        self.assertNotIn("solver-badge", row_html(html, parent))  # the parent's own combinations are fine
        open_parts = self.entry(parent)["open_parts"]
        self.assertTrue(open_parts[0]["unconverged"])
        self.assertEqual(open_parts[0]["solver_status"], "failed")


class TheRunPage(SolverPages):
    def test_the_solver_status_block_lists_each_combination(self):
        write_run(self.root, "run_20261026_121722", {"analysis_settings": analysis()},
                  done_state("default"), profiles={"default": FAILED_TEXT}, results={"default": BAD_CSV})
        html = self.client.get("/runs/run_20261026_121722").get_data(as_text=True)
        self.assertIn('<h3 class="settings-title">Solver status</h3>', html)
        block = html[html.index('<h3 class="settings-title">Solver status</h3>'):]
        block = block[:block.index("<h3", 10)]
        self.assertIn('<span class="pill bad">Sweep did not converge</span>', block)
        self.assertIn("Interpolating sweep did NOT converge", block)
        # basis elements 2 (of the sweep that failed), its last S-matrix error, 1 point
        # above 0 dB, and 2 points with more power out than in
        self.assertRegex(block, r"<td>2</td>\s*<td>100\.918%</td>\s*<td>1</td>\s*<td>2</td>")

    def test_the_banner_sits_above_the_results_only_when_there_is_a_plot(self):
        write_run(self.root, "run_20261026_121722", {"analysis_settings": analysis()},
                  done_state("default"), profiles={"default": FAILED_TEXT}, results={"default": BAD_CSV})
        without = self.client.get("/runs/run_20261026_121722").get_data(as_text=True)
        self.assertNotIn(BANNER, without)
        write_run(self.root, "run_20261026_121723", {"analysis_settings": analysis()},
                  done_state("default"), profiles={"default": FAILED_TEXT}, results={"default": BAD_CSV}, plot=True)
        with_plot = self.client.get("/runs/run_20261026_121723").get_data(as_text=True)
        self.assertIn('<div class="error-box" role="alert">' + BANNER, with_plot)
        # Above the plot: after the Results heading, before the plot's frame.
        self.assertLess(with_plot.index("<h2>Results</h2>"), with_plot.index(BANNER))
        self.assertLess(with_plot.index(BANNER), with_plot.index('<iframe id="plot-frame"'))

    def test_a_warning_run_says_so_in_amber_and_has_no_banner(self):
        write_run(self.root, "run_20261025_142208", {"analysis_settings": analysis()},
                  done_state("default"), profiles={"default": WARNING_TEXT}, results={"default": GOOD_CSV},
                  plot=True)
        html = self.client.get("/runs/run_20261025_142208").get_data(as_text=True)
        self.assertIn('<span class="solver-warning">Adaptive passes not converged</span>', html)
        self.assertNotIn(BANNER, html)

    def test_a_run_with_no_profile_says_so(self):
        write_run(self.root, "run_20261001_120000", {"analysis_settings": analysis()}, done_state("default"))
        html = self.client.get("/runs/run_20261001_120000").get_data(as_text=True)
        self.assertIn("No solver profile was found for this run.", html)

    def test_a_refined_solve_that_failed_is_marked_in_the_refine_list(self):
        parent = "run_20261026_122222"
        child = parent + "_fine1"
        write_run(self.root, parent, {"refined_by": [child], "analysis_settings": analysis()},
                  done_state("default"), profiles={"default": OK_TEXT}, results={"default": GOOD_CSV}, plot=True)
        write_run(self.root, child, {
            "part_of": parent, "part_role": "fine1", "refines_run_id": parent,
            "analysis_settings": {**analysis(), "fine_windows_manual": "12.1:12.4:0.002"}},
            done_state("default"), profiles={"default": FAILED_TEXT}, results={"default": BAD_CSV})
        html = self.client.get(f"/runs/{parent}").get_data(as_text=True)
        refinements = html[html.index('<ul class="refinements">'):]
        refinements = refinements[:refinements.index("</ul>")]
        self.assertIn(BADGE, refinements)


if __name__ == "__main__":
    unittest.main()
