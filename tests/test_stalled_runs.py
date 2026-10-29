"""Tests for a run in a live stage that nothing drives: it is stalled, not going.

A run is going when its stage is live AND something drives it -- its pipeline
thread in this process, or another live process holding its driver claim. A run
in a live stage that nothing drives reads "Stalled (was building geometry)" on
the run list and on its page, is not shown above the fold on its own, and its
page still offers Resume without saying it is running. The run list (the poll
feeds data-active) and the run page use the same rule.

Every run here is written into a temporary runs folder, and every process or
thread a test starts is stopped again before the test ends.

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
import threading
import time
import unittest

import psutil

from microwave_package import paths, webapp

PARENT = "run_20261026_121722"


def done_combo():
    return {"altium_status": "done", "step_status": "done", "model_status": "built",
            "handoff_status": "done", "queue_status": "done", "fetch_status": "done",
            "post_status": "done", "params": {}}


def building_combo():
    return {"altium_status": "done", "step_status": "running", "params": {}}


def state_of(stage, combos):
    return {"stage": stage, "combos": {f"c{i}": combo for i, combo in enumerate(combos)}}


def analysis(solution_type="DrivenModal"):
    return {"solution_type": solution_type, "materials": {"mat_pcb": "Rogers TMM 10i (tm)"}}


def write_run(root, run_id, config, state):
    folder = os.path.join(root, run_id)
    os.makedirs(os.path.join(folder, "plots"), exist_ok=True)
    with open(os.path.join(folder, "run_config.json"), "w", encoding="utf-8") as handle:
        json.dump(config, handle)
    with open(os.path.join(folder, "state.json"), "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    return folder


def live_claim(test):
    """A driver claim held by a live process other than this one, until the test ends.
    This process's own claim does not count: its thread is the authority for it."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])

    def stop():
        process.kill()
        process.wait()
    test.addCleanup(stop)
    return {"pid": process.pid, "started": psutil.Process(process.pid).create_time(),
            "host": socket.gethostname()}


def stage_pill(html):
    return re.search(r'<span class="pill [^"]*" id="stage">([^<]*)</span>', html).group(1)


def running_indicator(html):
    return re.search(r'id="running-indicator">([^<]*)</span>', html).group(1)


def tag_of(html, element_id):
    """The opening tag of the element with this id."""
    return re.search(r'<[a-z]+[^>]*id="%s"[^>]*>' % re.escape(element_id), html, re.S).group(0)


class StalledRuns(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.root = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(shutil.rmtree, self.root, True)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = self.root
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)
        self.client = webapp.app.test_client()

    def run_in(self, stage, run_id=PARENT, combos=None, config=None):
        return write_run(self.root, run_id, {"analysis_settings": analysis(), **(config or {})},
                         state_of(stage, combos or [building_combo()]))

    def newer_runs(self):
        """Three complete runs newer than the run under test, so it is the fourth row."""
        for index in range(3):
            write_run(self.root, f"run_20261029_12000{index}", {"analysis_settings": analysis()},
                      state_of("complete", [done_combo()]))

    def entry(self, run_id):
        return next(e for e in self.client.get("/runs.json").get_json() if e["run_id"] == run_id)

    def live_thread_for(self, run_id):
        """A pipeline thread that is alive in this process, registered as the web app's."""
        stop = threading.Event()
        thread = threading.Thread(target=stop.wait, daemon=True)
        thread.start()
        webapp._active_threads[run_id] = thread

        def finish():
            stop.set()
            thread.join(5)
            webapp._active_threads.pop(run_id, None)
        self.addCleanup(finish)


class AStaleLiveStageWithNothingDrivingIt(StalledRuns):
    def test_it_is_stalled_not_going_and_is_folded_behind_show_more(self):
        self.run_in("generating_and_queuing")
        self.newer_runs()
        entry = self.entry(PARENT)
        self.assertFalse(entry["active"])
        self.assertTrue(entry["stalled"])
        self.assertEqual(entry["stage_display"]["label"], "Stalled (was building geometry)")
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("hidden", re.search(r'<tr class="run-row[^"]*"[^>]*data-run-id="%s"[^>]*>' % PARENT,
                                          html, re.S).group(0))
        self.assertIn('id="runs-more">Show 1 more</button>', html)
        self.assertIn('data-stage="stalled"', html)

    def test_its_page_says_stalled_and_still_offers_resume(self):
        self.run_in("generating_and_queuing")
        html = self.client.get(f"/runs/{PARENT}").get_data(as_text=True)
        self.assertEqual(stage_pill(html), "Stalled (was building geometry)")
        self.assertEqual(running_indicator(html), "idle")
        self.assertNotIn("hidden", tag_of(html, "resume-form"))
        self.assertIn("hidden", tag_of(html, "stop-form"))

    def test_the_status_poll_agrees_with_the_list_and_the_page(self):
        self.run_in("postprocessing")
        status = self.client.get(f"/runs/{PARENT}/status.json").get_json()
        self.assertFalse(status["is_running"])
        self.assertEqual(status["stage_display"]["label"], "Stalled (was post-processing)")
        self.assertEqual(self.entry(PARENT)["stage_display"]["label"], status["stage_display"]["label"])
        self.assertEqual(stage_pill(self.client.get(f"/runs/{PARENT}").get_data(as_text=True)),
                         status["stage_display"]["label"])

    def test_a_driver_claim_held_by_this_process_alone_does_not_make_it_going(self):
        """Only the thread is this process's authority; a leftover claim naming this
        process, with no thread behind it, is nobody driving the run."""
        folder = self.run_in("generating_and_queuing")
        with open(os.path.join(folder, "driver.json"), "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "started": psutil.Process().create_time(),
                       "host": socket.gethostname()}, handle)
        self.assertTrue(self.entry(PARENT)["stalled"])


class WhatIsGoing(StalledRuns):
    def test_a_live_driver_process_makes_it_going(self):
        folder = self.run_in("generating_and_queuing")
        with open(os.path.join(folder, "driver.json"), "w", encoding="utf-8") as handle:
            json.dump(live_claim(self), handle)
        self.newer_runs()
        entry = self.entry(PARENT)
        self.assertTrue(entry["active"])
        self.assertFalse(entry["stalled"])
        self.assertEqual(entry["stage_display"]["label"], "Building geometry")
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("hidden", re.search(r'<tr class="run-row[^"]*"[^>]*data-run-id="%s"[^>]*>' % PARENT,
                                             html, re.S).group(0))
        page = self.client.get(f"/runs/{PARENT}").get_data(as_text=True)
        self.assertEqual(running_indicator(page), "running elsewhere")
        self.assertEqual(stage_pill(page), "Building geometry")

    def test_a_live_thread_in_this_process_makes_it_going(self):
        self.run_in("generating_and_queuing")
        self.live_thread_for(PARENT)
        self.assertTrue(self.entry(PARENT)["active"])
        page = self.client.get(f"/runs/{PARENT}").get_data(as_text=True)
        self.assertEqual(running_indicator(page), "running")
        self.assertEqual(stage_pill(page), "Building geometry")

    def test_a_run_with_a_part_going_stays_active_while_its_own_stage_is_stalled(self):
        self.run_in("generating_and_queuing", config={"refined_by": [PARENT + "_fine1"]})
        write_run(self.root, PARENT + "_fine1", {
            "part_of": PARENT, "part_role": "fine1", "refines_run_id": PARENT,
            "analysis_settings": {**analysis(), "fine_windows_manual": "12.1:12.4:0.002"},
        }, state_of("geometry_ready", [{"model_status": "built", "handoff_status": "done",
                                        "queue_status": "pending", "params": {}}]))
        self.newer_runs()
        entry = self.entry(PARENT)
        self.assertTrue(entry["active"])
        self.assertTrue(entry["stalled"])
        self.assertEqual(entry["stage_display"]["label"], "Stalled (was building geometry)")
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("hidden", re.search(r'<tr class="run-row[^"]*"[^>]*data-run-id="%s"[^>]*>' % PARENT,
                                             html, re.S).group(0))


class StagesOutsideTheLiveOnesAreUnchanged(StalledRuns):
    def test_created_complete_stopped_and_error_keep_their_words_and_are_not_active(self):
        expectations = {
            "created": "Created",
            "complete": "Complete",
            "stopped": "Stopped",
            "error": "Error",
        }
        for index, (stage, label) in enumerate(expectations.items()):
            run_id = f"run_2026100{index}_120000"
            write_run(self.root, run_id, {"analysis_settings": analysis()}, state_of(stage, [done_combo()]))
            entry = self.entry(run_id)
            self.assertEqual(entry["stage_display"]["label"], label, stage)
            self.assertFalse(entry["active"], stage)
            self.assertFalse(entry["stalled"], stage)


class TheStageFilterFindsStalledRuns(StalledRuns):
    def test_the_filter_offers_stalled_and_a_stalled_run_is_not_running(self):
        self.run_in("generating_and_queuing")
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('<option value="stalled">Stalled</option>', html)
        self.assertIn('data-stage="stalled"', html)
        self.assertNotIn('data-stage="running"', html)


if __name__ == "__main__":
    unittest.main()
