"""Tests for the hidden parts of a run as the run list and the run page show them:
a refined solve or an eigenmode run still going is a row of its own under the
run it belongs to, a finished refined solve is a note on that run, and the run
page's "Refine a frequency range" section opens by itself while one is going.

A part's state is read from its combinations' own fields, not from its stage
name: the fixtures below say "postprocessing" for a part the worker is still
solving, and the page must still say it is solving.

Every run here is written into a temporary runs folder.

Run: python -m unittest discover -s tests
"""
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import psutil

from microwave_package import paths, webapp
from microwave_package.workflow.run_manager import part_kind, part_state

PARENT = "run_20261026_121722"
EIGEN = PARENT + "_eigen"
FINE = {n: f"{PARENT}_fine{n}" for n in range(1, 7)}
WINDOWS = {1: "2.05:2.1:0.001; 19.2:19.25:0.001", 2: "12.1:12.4:0.002"}

ROW_TAG = re.compile(r'<tr class="part-row"[^>]*data-part-id="(?P<id>[^"]+)"[^>]*>', re.S)
RUN_ROW_TAG = re.compile(r'<tr class="run-row[^"]*"[^>]*data-run-id="(?P<id>[^"]+)"[^>]*>', re.S)


def run_row_tag(html, run_id):
    """The opening tag of one run's row on the home page."""
    for match in RUN_ROW_TAG.finditer(html):
        if match.group("id") == run_id:
            return match.group(0)
    raise AssertionError(f"no row for {run_id} on the home page")


def live_claim(test):
    """A driver claim held by a live process other than this one, which lasts until
    the test ends. A claim by this very process does not count (the thread is the
    authority for this process), so tests that need a live driver use this."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])

    def stop():
        process.kill()
        process.wait()
    test.addCleanup(stop)
    return {"pid": process.pid, "started": psutil.Process(process.pid).create_time(),
            "host": socket.gethostname()}


def child_rows(html):
    """(part id, the row's opening tag, the row's whole markup) for each part row."""
    rows = []
    for match in ROW_TAG.finditer(html):
        end = html.index("</tr>", match.start()) + len("</tr>")
        rows.append((match.group("id"), match.group(0), html[match.start():end]))
    return rows


def done_combo(**overrides):
    return {"altium_status": "done", "step_status": "done", "model_status": "built",
            "handoff_status": "done", "queue_status": "done", "fetch_status": "done",
            "post_status": "done", **overrides}


def state_of(stage, combos):
    # every combination records its parameters, as the pipeline writes them
    return {"stage": stage,
            "combos": {f"c{i}": {"params": {}, **combo} for i, combo in enumerate(combos)}}


def write_run(root, run_id, config, state):
    run_dir = os.path.join(root, run_id)
    os.makedirs(os.path.join(run_dir, "plots"), exist_ok=True)
    with open(os.path.join(run_dir, "run_config.json"), "w", encoding="utf-8") as handle:
        json.dump(config, handle)
    with open(os.path.join(run_dir, "state.json"), "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    return run_dir


def analysis(solution_type="DrivenModal", **extra):
    return {"solution_type": solution_type, "materials": {"mat_pcb": "Rogers TMM 10i (tm)"}, **extra}


class PartStates(unittest.TestCase):
    """What a part is doing, from the fields of its combinations."""

    def test_a_part_with_every_combination_finished_is_complete(self):
        self.assertEqual(part_state(state_of("complete", [done_combo()])), "complete")

    def test_a_combination_running_on_the_worker_is_solving_whatever_the_stage_says(self):
        combo = {"model_status": "built", "handoff_status": "done", "queue_status": "in_progress"}
        self.assertEqual(part_state(state_of("postprocessing", [combo])), "solving")

    def test_a_queued_combination_is_queued_and_a_held_one_is_paused(self):
        queued = {"model_status": "built", "handoff_status": "done", "queue_status": "pending"}
        held = {"model_status": "built", "handoff_status": "done", "queue_status": "held"}
        self.assertEqual(part_state(state_of("geometry_ready", [queued])), "queued")
        self.assertEqual(part_state(state_of("geometry_ready", [held])), "paused")

    def test_a_worker_done_with_a_combination_whose_results_are_being_read_is_reading(self):
        combo = {"model_status": "built", "handoff_status": "done", "queue_status": "done",
                 "fetch_status": "running"}
        self.assertEqual(part_state(state_of("postprocessing", [combo])), "reading")

    def test_a_failed_copy_back_is_failed_not_reading(self):
        combo = {"model_status": "built", "handoff_status": "done", "queue_status": "done",
                 "fetch_status": "failed"}
        self.assertEqual(part_state(state_of("postprocessing", [combo])), "failed")

    def test_files_that_cannot_reach_the_queue_are_waiting(self):
        combo = {"model_status": "built", "handoff_status": "deferred"}
        self.assertEqual(part_state(state_of("generating_and_queuing", [combo])), "waiting")

    def test_a_part_stopped_before_its_geometry_was_done_is_stopped(self):
        combo = {"altium_status": "running", "step_status": "running"}
        self.assertEqual(part_state(state_of("stopped", [combo])), "stopped")

    def test_a_part_that_stopped_on_an_error_is_failed(self):
        combo = {"altium_status": "running"}
        self.assertEqual(part_state(state_of("error", [combo])), "failed")

    def test_a_part_whose_geometry_is_still_being_built_is_being_prepared(self):
        combo = {"model_status": "building"}
        self.assertEqual(part_state(state_of("generating_and_queuing", [combo])), "preparing")

    def test_a_part_with_nothing_done_yet_is_starting(self):
        self.assertEqual(part_state(state_of("created", [])), "starting")

    def test_a_skipped_combination_is_not_waited_for(self):
        skipped = {"skip_reason": "does not fit"}
        self.assertEqual(part_state(state_of("complete", [done_combo(), skipped])), "complete")

    def test_the_kind_of_a_part_comes_from_its_config(self):
        self.assertEqual(part_kind({"part_role": "eigen", "driven_run_id": PARENT}), "eigen")
        self.assertEqual(part_kind({"part_of": PARENT, "refines_run_id": PARENT}), "refinement")
        self.assertEqual(part_kind({}), "part")


class PartsOnTheRunList(unittest.TestCase):
    """A parent with a part in each state, on disk, and the web app over it."""

    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.root = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(shutil.rmtree, self.root, True)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = self.root
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)
        self.client = webapp.app.test_client()
        self.write_fixtures()

    def write_fixtures(self):
        combo_running = {"model_status": "built", "handoff_status": "done", "queue_status": "in_progress"}
        states = {
            1: state_of("geometry_ready", [{"model_status": "built", "handoff_status": "done",
                                             "queue_status": "pending"}]),
            2: state_of("postprocessing", [combo_running]),
            3: state_of("postprocessing", [{"model_status": "built", "handoff_status": "done",
                                             "queue_status": "done", "fetch_status": "running"}]),
            4: state_of("error", [{"model_status": "built", "handoff_status": "done",
                                    "queue_status": "done", "fetch_status": "failed"}]),
            5: state_of("stopped", [{"altium_status": "running", "step_status": "running"}]),
            6: state_of("complete", [done_combo()]),
        }
        write_run(self.root, PARENT, {
            "analysis_settings": analysis(), "label": "C2 chip",
            "eigen_run_id": EIGEN, "refined_by": [FINE[n] for n in range(1, 7)],
        }, state_of("complete", [done_combo()]))
        write_run(self.root, EIGEN, {
            "part_of": PARENT, "part_role": "eigen", "driven_run_id": PARENT,
            "analysis_settings": analysis("Eigenmode", modes=10),
        }, state_of("postprocessing", [combo_running]))
        for n, state in states.items():
            write_run(self.root, FINE[n], {
                "part_of": PARENT, "part_role": f"fine{n}", "refines_run_id": PARENT,
                "analysis_settings": analysis(fine_windows_manual=WINDOWS.get(n, "2.05:2.1:0.001")),
            }, state)
        # Newer runs above the parent, so that it is the fourth row: folded away.
        for index in range(3):
            write_run(self.root, f"run_20261029_12000{index}", {"analysis_settings": analysis()},
                      state_of("complete", [done_combo()]))

    def child_rows(self, html):
        return child_rows(html)

    def test_each_open_part_is_a_row_under_its_run_with_its_own_words(self):
        html = self.client.get("/").get_data(as_text=True)
        rows = {part_id: (tag, markup) for part_id, tag, markup in self.child_rows(html)}
        self.assertEqual(set(rows), {FINE[1], FINE[2], FINE[3], FINE[4], FINE[5], EIGEN})
        expected = {
            FINE[1]: ("refined solve: 2 ranges 2.05-19.25 GHz", "refined solve queued"),
            FINE[2]: ("refined solve: 1 range 12.1-12.4 GHz", "refined solve solving on the worker"),
            FINE[3]: ("refined solve: 1 range 2.05-2.1 GHz", "reading results"),
            FINE[4]: ("refined solve: 1 range 2.05-2.1 GHz", "failed"),
            FINE[5]: ("refined solve: 1 range 2.05-2.1 GHz", "stopped"),
            EIGEN: ("eigenmodes", "eigenmodes solving on the worker"),
        }
        for part_id, (label, word) in expected.items():
            markup = rows[part_id][1]
            self.assertIn(f"↳ {label}", markup, part_id)
            self.assertIn(f'<span class="pill ', markup)
            self.assertIn(f">{word}</span>", markup, part_id)

    def test_a_part_row_has_no_compare_box(self):
        html = self.client.get("/").get_data(as_text=True)
        for _part_id, _tag, markup in self.child_rows(html):
            self.assertNotIn("compare-pick", markup)

    def test_a_part_row_links_to_the_refine_section_of_its_run(self):
        html = self.client.get("/").get_data(as_text=True)
        rows = {part_id: markup for part_id, _tag, markup in self.child_rows(html)}
        self.assertIn(f'href="/runs/{PARENT}#refine"', rows[FINE[1]])
        self.assertIn(f'href="/runs/{PARENT}#settings"', rows[EIGEN])

    def test_a_finished_refined_solve_is_a_note_on_its_run_not_a_row(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn(f'data-part-id="{FINE[6]}"', html)
        self.assertIn(f'<a class="part-note" href="/runs/{PARENT}#refine">+1 refined solve</a>', html)

    def test_the_rows_of_a_run_folded_away_are_folded_away_with_it(self):
        """An older run that is not going folds behind "Show N more", and the
        refined solve that failed on it just now folds with it."""
        old = "run_20261001_120000"
        write_run(self.root, old, {"analysis_settings": analysis(), "refined_by": [old + "_fine1"]},
                  state_of("complete", [done_combo()]))
        write_run(self.root, old + "_fine1", {
            "part_of": old, "part_role": "fine1", "refines_run_id": old,
            "analysis_settings": analysis(fine_windows_manual="12.1:12.4:0.002"),
        }, state_of("error", [{"model_status": "built", "handoff_status": "done",
                               "queue_status": "done", "fetch_status": "failed"}]))
        html = self.client.get("/").get_data(as_text=True)
        tags = {part_id: tag for part_id, tag, _markup in self.child_rows(html)}
        self.assertIn("hidden", tags[old + "_fine1"])
        self.assertIn("hidden", run_row_tag(html, old))
        # the run going, with its own parts, is shown by default: its rows are not folded
        self.assertNotIn("hidden", tags[FINE[1]])
        self.assertNotIn("hidden", run_row_tag(html, PARENT))

    def test_the_run_list_json_carries_each_part_under_its_run(self):
        entries = {entry["run_id"]: entry for entry in self.client.get("/runs.json").get_json()}
        parent = entries[PARENT]
        self.assertEqual(parent["refined_done"], 1)
        open_parts = {part["run_id"]: part for part in parent["open_parts"]}
        self.assertEqual(set(open_parts), {FINE[1], FINE[2], FINE[3], FINE[4], FINE[5], EIGEN})
        self.assertEqual(open_parts[FINE[2]]["state"], "solving")
        self.assertEqual(open_parts[FINE[2]]["word"], "refined solve solving on the worker")
        self.assertEqual(open_parts[FINE[2]]["label"], "refined solve: 1 range 12.1-12.4 GHz")
        self.assertEqual(open_parts[FINE[1]]["windows"], "2 ranges 2.05-19.25 GHz")
        self.assertEqual(open_parts[EIGEN]["word"], "eigenmodes solving on the worker")
        self.assertEqual(open_parts[EIGEN]["kind"], "eigen")
        for part in open_parts.values():
            self.assertIn("total", part)
            self.assertIn("done", part)
            self.assertIn("failed", part)
        self.assertNotIn("parts", parent)  # the raw records stay on the server
        self.assertNotIn("facet_inputs", parent)

    def test_a_part_is_not_a_run_of_its_own_on_the_list_or_in_the_json(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn(f'data-run-id="{FINE[2]}"', html)
        self.assertNotIn(f'data-run-id="{EIGEN}"', html)
        listed = {entry["run_id"] for entry in self.client.get("/runs.json").get_json()}
        self.assertNotIn(FINE[2], listed)
        self.assertNotIn(EIGEN, listed)


class TheRunPageRefineSection(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.root = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(shutil.rmtree, self.root, True)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = self.root
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)
        self.client = webapp.app.test_client()

    def parent_with(self, *child_states):
        """A finished parent with a plot, and one refined solve per state given."""
        ids = []
        for n, state in enumerate(child_states, start=1):
            child = f"{PARENT}_fine{n}"
            ids.append(child)
            write_run(self.root, child, {
                "part_of": PARENT, "part_role": f"fine{n}", "refines_run_id": PARENT,
                "analysis_settings": analysis(fine_windows_manual="12.1:12.4:0.002"),
            }, state)
        write_run(self.root, PARENT, {"analysis_settings": analysis(), "refined_by": ids},
                  state_of("complete", [done_combo()]))
        plots = os.path.join(self.root, PARENT, "plots")
        open(os.path.join(plots, "modes_scatter_plot.html"), "w").close()
        with open(os.path.join(plots, "all_results.csv"), "w", encoding="utf-8") as handle:
            handle.write("combo_id,freq_ghz,s21_db,sweep\nc0,4.0,-1.0,coarse\n")
        return ids

    def section(self, html):
        start = html.index('id="refine"')
        return html[start:html.index("</details>", start)]

    def test_the_section_opens_by_itself_while_a_refined_solve_is_going(self):
        self.parent_with(state_of("postprocessing", [{"model_status": "built", "handoff_status": "done",
                                                      "queue_status": "in_progress"}]))
        html = self.client.get(f"/runs/{PARENT}").get_data(as_text=True)
        self.assertRegex(html, r'<details class="collapsible refine-section" id="refine" open>')
        section = self.section(html)
        self.assertIn("refined solve solving on the worker", section)
        self.assertNotIn("Post-processing", section)
        self.assertIn("Refine a frequency range", section)

    def test_the_section_stays_closed_when_every_refined_solve_is_finished(self):
        self.parent_with(state_of("complete", [done_combo()]))
        html = self.client.get(f"/runs/{PARENT}").get_data(as_text=True)
        self.assertRegex(html, r'<details class="collapsible refine-section" id="refine">')

    def test_the_states_read_in_words_from_the_combinations(self):
        self.parent_with(
            state_of("geometry_ready", [{"model_status": "built", "handoff_status": "done",
                                         "queue_status": "pending"}]),
            state_of("error", [{"model_status": "built", "handoff_status": "done",
                                "queue_status": "done", "fetch_status": "failed"}]),
            state_of("postprocessing", [{"model_status": "built", "handoff_status": "done",
                                         "queue_status": "done", "fetch_status": "running"}]),
        )
        section = self.section(self.client.get(f"/runs/{PARENT}").get_data(as_text=True))
        self.assertIn('<span class="pill busy">refined solve queued</span>', section)
        self.assertIn('<span class="pill bad">failed</span>', section)
        self.assertIn('<span class="pill busy">reading results</span>', section)
        self.assertNotIn("Post-processing", section)
        self.assertIn("Resume", section)  # not running here, so the resume control stays


class StageDecidesOverAStaleQueue(unittest.TestCase):
    """A part's own stage wins over a queue field that still says a task is in
    progress: a stopped part is never solving, and only a live stage can be."""

    IN_PROGRESS = {"model_status": "built", "handoff_status": "done", "queue_status": "in_progress"}

    def test_a_stopped_part_with_a_task_in_progress_is_stopped_not_solving(self):
        self.assertEqual(part_state(state_of("stopped", [self.IN_PROGRESS])), "stopped")

    def test_an_errored_part_with_a_task_in_progress_is_failed(self):
        self.assertEqual(part_state(state_of("error", [self.IN_PROGRESS])), "failed")

    def test_a_completed_stage_has_no_work_left_whatever_its_combinations_say(self):
        self.assertEqual(part_state(state_of("complete", [self.IN_PROGRESS])), "complete")

    def test_a_task_in_progress_in_a_stage_that_is_not_live_is_only_starting(self):
        self.assertEqual(part_state(state_of("created", [self.IN_PROGRESS])), "starting")

    def test_a_live_stage_with_a_task_in_progress_is_solving(self):
        self.assertEqual(part_state(state_of("postprocessing", [self.IN_PROGRESS])), "solving")


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

    def refined_solve(self, parent, n, state, days=0.0):
        """A refined solve of `parent`, its state written `days` days ago."""
        child = f"{parent}_fine{n}"
        folder = write_run(self.root, child, {
            "part_of": parent, "part_role": f"fine{n}", "refines_run_id": parent,
            "analysis_settings": analysis(fine_windows_manual="12.1:12.4:0.002"),
        }, state)
        when = time.time() - days * 24 * 3600
        os.utime(os.path.join(folder, "state.json"), (when, when))
        return child


class PartRowsByAge(RunsOnDisk):
    """A part that failed or stopped gets a row for three days; one still going
    always does. The run page lists every refined solve either way."""

    def parent_with(self, *children):
        write_run(self.root, PARENT, {"analysis_settings": analysis(), "refined_by": list(children)},
                  state_of("complete", [done_combo()]))
        plots = os.path.join(self.root, PARENT, "plots")
        os.makedirs(plots, exist_ok=True)
        open(os.path.join(plots, "modes_scatter_plot.html"), "w").close()

    def row_ids(self, html):
        return {part_id for part_id, _tag, _markup in child_rows(html)}

    def test_a_stopped_refined_solve_from_long_ago_gets_no_row_but_the_run_page_lists_it(self):
        child = self.refined_solve(PARENT, 1, state_of("stopped", [{"altium_status": "running"}]), days=10)
        self.parent_with(child)
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn(child, self.row_ids(html))
        section = self.client.get(f"/runs/{PARENT}").get_data(as_text=True)
        self.assertIn(child.rsplit("_", 1)[-1], section)
        self.assertIn('<span class="pill muted">stopped</span>', section)

    def test_a_stopped_refined_solve_from_today_has_a_row_worded_stopped(self):
        child = self.refined_solve(PARENT, 1, state_of("stopped", [{"altium_status": "running"}]))
        self.parent_with(child)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn(child, self.row_ids(html))
        self.assertIn(">stopped</span>", html)

    def test_a_failed_refined_solve_from_long_ago_gets_no_row(self):
        child = self.refined_solve(PARENT, 1, state_of("error", [{"model_status": "built",
                                                                  "handoff_status": "done",
                                                                  "queue_status": "done",
                                                                  "fetch_status": "failed"}]), days=10)
        self.parent_with(child)
        self.assertNotIn(child, self.row_ids(self.client.get("/").get_data(as_text=True)))

    def test_a_refined_solve_still_queued_has_a_row_whatever_its_age(self):
        child = self.refined_solve(PARENT, 1, state_of("geometry_ready", [{"model_status": "built",
                                                                           "handoff_status": "done",
                                                                           "queue_status": "pending"}]),
                                  days=30)
        self.parent_with(child)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn(child, self.row_ids(html))
        self.assertIn(">refined solve queued</span>", html)

    def test_a_stopped_run_with_a_task_left_in_progress_shows_stopped_not_solving(self):
        child = self.refined_solve(PARENT, 1, state_of("stopped", [{"model_status": "built",
                                                                    "handoff_status": "done",
                                                                    "queue_status": "in_progress"}]))
        self.parent_with(child)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn(">stopped</span>", html)
        self.assertNotIn("solving on the worker", html)


class RunsShownByDefault(RunsOnDisk):
    """With nothing filtered, the newest three runs show, and so does any run that is
    going; the others fold behind "Show N more", which counts only those."""

    def write_plain(self, run_id, stage="complete"):
        return write_run(self.root, run_id, {"analysis_settings": analysis()}, state_of(stage, [done_combo()]))

    def test_a_run_that_is_going_shows_by_default_and_the_rest_fold(self):
        for run_id in ("run_20261029_120002", "run_20261029_120001", "run_20261029_120000"):
            self.write_plain(run_id)
        self.write_plain("run_20261005_120000")                      # idle: folds
        self.write_plain("run_20261004_120000")                      # idle, but has a refined solve queued
        self.refined_solve("run_20261004_120000", 1, state_of("geometry_ready", [{
            "model_status": "built", "handoff_status": "done", "queue_status": "pending"}]))
        self.write_plain("run_20261003_120000")                      # idle: folds
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("hidden", run_row_tag(html, "run_20261005_120000"))
        self.assertNotIn("hidden", run_row_tag(html, "run_20261004_120000"))
        self.assertIn("hidden", run_row_tag(html, "run_20261003_120000"))
        self.assertIn('id="runs-more">Show 2 more</button>', html)

    def test_a_run_whose_own_stage_is_going_is_active(self):
        """Going = a live stage that something drives: here, a live driver process."""
        for run_id in ("run_20261029_120002", "run_20261029_120001", "run_20261029_120000"):
            self.write_plain(run_id)
        going = self.write_plain("run_20261005_120000", stage="postprocessing")
        with open(os.path.join(going, "driver.json"), "w", encoding="utf-8") as handle:
            json.dump(live_claim(self), handle)
        self.write_plain("run_20261003_120000")
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("hidden", run_row_tag(html, "run_20261005_120000"))
        self.assertIn('id="runs-more">Show 1 more</button>', html)
        active = {entry["run_id"]: entry["active"] for entry in self.client.get("/runs.json").get_json()}
        self.assertTrue(active["run_20261005_120000"])
        self.assertFalse(active["run_20261003_120000"])


class TheDriverOfAPartIsNotItsRunsDriver(RunsOnDisk):
    """A run's page says it is driven elsewhere only when its own folder names a
    live process other than this one. A refined solve's process claims its own
    folder, and must not make the run it belongs to look driven elsewhere."""

    def live_claim(self):
        return live_claim(self)

    def elsewhere_note_hidden(self, html):
        tag = re.search(r'<div class="note-box" id="elsewhere-note"([^>]*)>', html).group(1)
        return "hidden" in tag

    def test_a_refined_solves_claim_does_not_show_on_its_run(self):
        child = self.refined_solve(PARENT, 1, state_of("postprocessing", [{
            "model_status": "built", "handoff_status": "done", "queue_status": "in_progress"}]))
        write_run(self.root, PARENT, {"analysis_settings": analysis(), "refined_by": [child]},
                  state_of("complete", [done_combo()]))
        plots = os.path.join(self.root, PARENT, "plots")
        os.makedirs(plots, exist_ok=True)
        open(os.path.join(plots, "modes_scatter_plot.html"), "w").close()
        with open(os.path.join(self.root, child, "driver.json"), "w", encoding="utf-8") as handle:
            json.dump(self.live_claim(), handle)
        html = self.client.get(f"/runs/{PARENT}").get_data(as_text=True)
        self.assertTrue(self.elsewhere_note_hidden(html))

    def test_a_claim_in_the_runs_own_folder_does_show(self):
        write_run(self.root, PARENT, {"analysis_settings": analysis()}, state_of("complete", [done_combo()]))
        claim = self.live_claim()
        with open(os.path.join(self.root, PARENT, "driver.json"), "w", encoding="utf-8") as handle:
            json.dump(claim, handle)
        html = self.client.get(f"/runs/{PARENT}").get_data(as_text=True)
        self.assertFalse(self.elsewhere_note_hidden(html))
        self.assertIn(f'<span id="elsewhere-pid">{claim["pid"]}</span>', html)


class FineWindowsWords(unittest.TestCase):
    def test_the_windows_are_shortened_to_their_count_and_span(self):
        self.assertEqual(webapp._fine_windows_text("2.05:2.1:0.001; 19.2:19.25:0.001"),
                         "2 ranges 2.05-19.25 GHz")
        self.assertEqual(webapp._fine_windows_text("12.1:12.4:0.002"), "1 range 12.1-12.4 GHz")

    def test_text_that_is_not_windows_says_so(self):
        self.assertEqual(webapp._fine_windows_text("not a range"), "custom ranges")
        self.assertEqual(webapp._fine_windows_text(""), "")


if __name__ == "__main__":
    unittest.main()
