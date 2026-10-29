"""Tests for the run list's filter and the run page's settings, through the web
app's test client.

The home page carries the words the filter matches on in every run's row,
`/runs.json` carries the same words, and the run page shows what the run
was made with -- the materials, the solver's settings, the geometry it was
built from -- and does not fall over on a record missing most of its keys.
The filter itself runs in the browser; how it was checked is in the PR.

Every run here is written into a temporary runs folder.

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
from microwave_package.workflow.run_manager import Run

ROW = re.compile(r'<tr class="run-row[^"]*"[^>]*data-run-id="(?P<id>[^"]+)"(?P<rest>[^>]*)>', re.S)
DATA = re.compile(r'data-([a-z]+)="([^"]*)"')


def write_run(root, run_id, config, state=None, common=None):
    """A run folder as the pipeline leaves one: its config, its state, and
    optionally the equation lines every combination shared."""
    run_dir = os.path.join(root, run_id)
    os.makedirs(os.path.join(run_dir, "plots"), exist_ok=True)
    with open(os.path.join(run_dir, "run_config.json"), "w", encoding="utf-8") as handle:
        json.dump(config, handle)
    if state is None:
        state = {"stage": "complete", "combos": {"default": {"params": {}, "post_status": "done"}}}
    with open(os.path.join(run_dir, "state.json"), "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    if common is not None:
        with open(os.path.join(run_dir, "common_dimensions.txt"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(f'"{name}" = {value}' for name, value in common.items()) + "\n")
    return run_dir


def analysis(solution_type="DrivenModal", pcb="Rogers TMM 10i (tm)", **extra):
    """The analysis settings a web-made run records, with a substrate of choice."""
    return {
        "solution_type": solution_type,
        "f_start_ghz": 2.0, "f_stop_ghz": 20.0, "sweep_points": 361,
        "max_passes": 12, "min_passes": 3, "min_converged": 2, "max_delta_s": 0.02,
        "coarse_sweep": "on", "fine_windows": "off", "port_impedance_ohm": 50.0,
        "reference_plane": "connector", "student_version": True, "graphical": False,
        "use_altium": True,
        "materials": {
            "mat_box": "aluminum", "mat_pcb": pcb, "mat_chip": "silicon", "mat_vacuum": "vacuum",
            "perfect_e_box": True, "box_conductivity": 1e8, "mat_conductor_pcb": "copper",
            "mat_conductor_chip": "niobium", "perfect_e_conductors": True,
            "mat_wirebond": "aluminum", "include_wirebonds": True,
        },
        **extra,
    }


class RunsOnDisk(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.root = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(shutil.rmtree, self.root, True)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = self.root
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)
        self.client = webapp.app.test_client()

    def row_of(self, html, run_id):
        """The attributes of one run's table row on the home page."""
        for match in ROW.finditer(html):
            if match.group("id") == run_id:
                return dict(DATA.findall(match.group(0))), match.group("rest")
        self.fail(f"no row for {run_id} on the home page")


class TheHomePage(RunsOnDisk):
    def test_each_row_carries_the_words_the_filter_matches(self):
        write_run(self.root, "run_20261025_142208", {
            "label": "C1 bare", "created_at": "2026-10-25T14:22:08",
            "parameter_defaults": {"no_chip": 1.0, "pedestal_depth": 0.5},
            "analysis_settings": analysis()})
        html = self.client.get("/").get_data(as_text=True)
        attrs, _ = self.row_of(html, "run_20261025_142208")
        self.assertEqual(attrs["substrate"], "TMM 10i")
        self.assertEqual(attrs["chip"], "bare")
        self.assertEqual(attrs["pedestal"], "recess")
        self.assertEqual(attrs["shape"], "square")
        self.assertEqual(attrs["solution"], "DrivenModal")
        self.assertEqual(attrs["stage"], "complete")
        self.assertIn("c1 bare", attrs["search"])
        self.assertIn("run_20261025_142208", attrs["search"])
        self.assertIn("25 oct 2026", attrs["search"])

    def test_the_filter_bar_is_there_and_is_not_a_form(self):
        write_run(self.root, "run_20261025_142208", {"analysis_settings": analysis()})
        html = self.client.get("/").get_data(as_text=True)
        start = html.index('id="run-filter"')
        bar = html[start:html.index("</div>", start)]
        self.assertNotIn("<form", bar)
        for control in ("filter-text", "filter-substrate", "filter-chip", "filter-pedestal",
                        "filter-shape", "filter-solution", "filter-stage", "filter-count", "filter-clear"):
            self.assertIn(f'id="{control}"', html)

    def test_the_drop_downs_offer_the_substrates_and_solutions_the_runs_have(self):
        write_run(self.root, "run_20261025_142208", {"analysis_settings": analysis(pcb="Rogers TMM 10i (tm)")})
        write_run(self.root, "run_20261029_001700", {
            "analysis_settings": analysis(solution_type="LaunchStudy", pcb="Rogers RO3003 (tm)")})
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('<option value="TMM 10i">TMM 10i</option>', html)
        self.assertIn('<option value="RO3003">RO3003</option>', html)
        self.assertIn('<option value="LaunchStudy">Launch study</option>', html)
        self.assertNotIn('<option value="RT/duroid 5880">', html)

    def test_rows_past_the_first_three_stay_folded_away(self):
        for index in range(5):
            write_run(self.root, f"run_2026102{index}_120000", {"analysis_settings": analysis()})
        html = self.client.get("/").get_data(as_text=True)
        hidden = [self.row_of(html, f"run_2026102{index}_120000")[1] for index in range(5)]
        self.assertEqual(sum("hidden" in rest for rest in hidden), 2)
        self.assertIn("Show 2 more", html)

    def test_a_pair_matches_both_solution_types(self):
        write_run(self.root, "run_20261025_142208", {
            "eigen_run_id": "run_20261025_142208_eigen", "analysis_settings": analysis()})
        write_run(self.root, "run_20261025_142208_eigen", {
            "part_of": "run_20261025_142208", "part_role": "eigen",
            "analysis_settings": analysis(solution_type="Eigenmode", modes=7)})
        html = self.client.get("/").get_data(as_text=True)
        attrs, _ = self.row_of(html, "run_20261025_142208")
        self.assertEqual(attrs["solution"], "DrivenModal Eigenmode")
        self.assertNotIn("run_20261025_142208_eigen", html)  # a part, listed under its run

    def test_the_run_list_json_carries_the_words_and_no_inputs(self):
        write_run(self.root, "run_20261025_142208", {
            "label": "C1 bare", "parameter_defaults": {"no_chip": 1.0}, "analysis_settings": analysis()})
        entries = self.client.get("/runs.json").get_json()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["facets"]["chip"], "bare")
        self.assertEqual(entry["facets"]["substrate"], "TMM 10i")
        self.assertIn("search", entry["facets"])
        self.assertNotIn("facet_inputs", entry)
        self.assertEqual(Run.list_runs()[0]["run_id"], "run_20261025_142208")


class TheRunPage(RunsOnDisk):
    def test_it_shows_the_materials_and_the_solver_settings(self):
        write_run(self.root, "run_20261025_142208", {
            "label": "C1 bare", "parameter_defaults": {"no_chip": 1.0, "pedestal_depth": 0.5},
            "analysis_settings": analysis(pcb="Rogers RO3003 (tm)")})
        html = self.client.get("/runs/run_20261025_142208").get_data(as_text=True)
        self.assertIn('<details class="collapsible settings-section" id="settings">', html)
        self.assertIn("Settings of this run", html)
        for text in ("Materials", "PCB material", "Rogers RO3003 (tm)", "Chip material", "silicon",
                     "Solver and AEDT", "Sweep start (GHz)", "Sweep points", "Student version",
                     "Solver preset", "not recorded"):
            self.assertIn(text, html, text)

    def test_the_geometry_links_to_the_pins_and_says_where_each_value_came_from(self):
        write_run(self.root, "run_20261025_142208", {
            "parameter_defaults": {"no_chip": 1.0, "pedestal_depth": 0.5}, "analysis_settings": analysis()},
            common={"pedestal_shape": 0.0})
        html = self.client.get("/runs/run_20261025_142208").get_data(as_text=True)
        self.assertIn('href="#pinned"', html)
        self.assertIn('id="pinned"', html)
        self.assertIn("no chip (bare board)", html)
        self.assertIn("pinned for this run", html)
        self.assertIn("from the equation file", html)  # the vents, not pinned

    def test_the_chips_beside_the_title_say_what_the_board_is(self):
        write_run(self.root, "run_20261025_142208", {
            "parameter_defaults": {"no_chip": 1.0, "pedestal_depth": 0.5}, "analysis_settings": analysis()})
        html = self.client.get("/runs/run_20261025_142208").get_data(as_text=True)
        for chip in ("TMM 10i", "No chip", "Recess 0.5 mm"):
            self.assertIn(f'<span class="run-chip">{chip}</span>', html)

    def test_a_pair_shows_its_eigenmode_part_settings(self):
        write_run(self.root, "run_20261025_142208", {
            "eigen_run_id": "run_20261025_142208_eigen", "analysis_settings": analysis()})
        write_run(self.root, "run_20261025_142208_eigen", {
            "part_of": "run_20261025_142208", "part_role": "eigen",
            "analysis_settings": analysis(solution_type="Eigenmode", modes=7)})
        html = self.client.get("/runs/run_20261025_142208").get_data(as_text=True)
        self.assertIn("Eigenmode part (solved first)", html)
        self.assertIn("Modes", html)
        self.assertIn("<td>7</td>", html)

    def test_a_minimal_record_shows_not_recorded_and_does_not_fail(self):
        """An old run whose record has almost nothing in it: the page still
        comes up, and says what it does not know."""
        write_run(self.root, "run_20261001_120000", {"analysis_settings": {"solution_type": "Eigenmode"}},
                  state={"stage": "created", "combos": {}})
        response = self.client.get("/runs/run_20261001_120000")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("not recorded", html)
        self.assertIn("Settings of this run", html)

    def test_a_run_with_no_record_at_all_is_still_listed_and_opened(self):
        write_run(self.root, "run_20261001_120000", {}, state={"stage": "created", "combos": {}})
        self.assertEqual(self.client.get("/runs.json").status_code, 200)
        self.assertEqual(self.client.get("/runs/run_20261001_120000").status_code, 200)


if __name__ == "__main__":
    unittest.main()
