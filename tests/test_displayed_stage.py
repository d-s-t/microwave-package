"""Tests for the stage words of a remote-worker run.

The pipeline keeps a run in stage "postprocessing" for its whole wait while
its analyses are on the worker. The words the page shows come from the
combinations' queue_status instead (run_manager.displayed_stage):
"Solving on the worker" while one is in progress, "Waiting for the worker"
while the rest are pending or held, and "Post-processing" only once no
analysis is left on the worker.

Run: python -m pytest -q tests/test_displayed_stage.py
"""
import unittest

from microwave_package import webapp
from microwave_package.workflow import run_manager
from microwave_package.workflow.run_manager import displayed_stage


def combo(queue_status, task_id="t1"):
    return {"task_id": task_id, "queue_status": queue_status}


class DisplayedStageTests(unittest.TestCase):

    def test_only_postprocessing_is_reworded(self):
        for stage in ("eigenmodes", "monitoring", "plotting", "complete", "stopped", None):
            self.assertEqual(displayed_stage(stage, {"a": combo("in_progress")}), stage)

    def test_all_pending_or_held_is_waiting_for_worker(self):
        combos = {"a": combo("pending"), "b": combo("held")}
        self.assertEqual(displayed_stage("postprocessing", combos), "waiting_for_worker")

    def test_one_in_progress_is_solving_on_worker(self):
        combos = {"a": combo("in_progress"), "b": combo("pending")}
        self.assertEqual(displayed_stage("postprocessing", combos), "solving_on_worker")

    def test_in_progress_wins_over_waiting(self):
        combos = {"a": combo("held"), "b": combo("in_progress")}
        self.assertEqual(displayed_stage("postprocessing", combos), "solving_on_worker")

    def test_picked_up_but_not_started_counts_as_waiting_when_pending(self):
        # A task the worker has not picked up yet is still pending in the queue.
        combos = {"a": combo("pending"), "b": combo("done")}
        self.assertEqual(displayed_stage("postprocessing", combos), "waiting_for_worker")

    def test_all_analyses_back_is_post_processing(self):
        combos = {"a": combo("done"), "b": combo("failed")}
        self.assertEqual(displayed_stage("postprocessing", combos), "postprocessing")

    def test_no_queued_tasks_is_post_processing(self):
        combos = {"a": {"task_id": None, "queue_status": None}}
        self.assertEqual(displayed_stage("postprocessing", combos), "postprocessing")
        self.assertEqual(displayed_stage("postprocessing", {}), "postprocessing")
        self.assertEqual(displayed_stage("postprocessing", None), "postprocessing")

    def test_done_combos_do_not_count_as_waiting(self):
        combos = {"a": combo("done"), "b": combo("done")}
        self.assertEqual(displayed_stage("postprocessing", combos), "postprocessing")


class StageWordsTests(unittest.TestCase):

    def test_new_words_are_labelled(self):
        self.assertEqual(webapp._stage_display("waiting_for_worker")["label"], "Waiting for the worker")
        self.assertEqual(webapp._stage_display("solving_on_worker")["label"], "Solving on the worker")

    def test_stored_postprocessing_still_reads_as_before_when_no_shown_stage(self):
        self.assertEqual(webapp._run_stage_display("postprocessing", 0, going=True)["label"],
                         "Post-processing")

    def test_running_remote_run_shows_worker_words(self):
        display = webapp._run_stage_display("postprocessing", 0, True, shown="solving_on_worker")
        self.assertEqual(display, {"label": "Solving on the worker", "kind": "busy"})
        display = webapp._run_stage_display("postprocessing", 0, True, shown="waiting_for_worker")
        self.assertEqual(display, {"label": "Waiting for the worker", "kind": "busy"})

    def test_stalled_remote_run_says_what_it_was_doing(self):
        display = webapp._run_stage_display("postprocessing", 0, False, shown="waiting_for_worker")
        self.assertEqual(display, {"label": "Stalled (was waiting for the worker)", "kind": "muted"})
        display = webapp._run_stage_display("postprocessing", 0, False, shown="solving_on_worker")
        self.assertEqual(display, {"label": "Stalled (was solving on the worker)", "kind": "muted"})

    def test_stored_stage_still_decides_stalling(self):
        # The stored stage is live, so the run is stalled when nothing drives it.
        self.assertIn("postprocessing", run_manager.ACTIVE_STAGES)
        self.assertNotIn("waiting_for_worker", run_manager.ACTIVE_STAGES)
        display = webapp._run_stage_display("postprocessing", 0, False, shown=None)
        self.assertEqual(display["label"], "Stalled (was post-processing)")

    def test_finished_run_is_unchanged(self):
        self.assertEqual(webapp._run_stage_display("complete", 2, False, shown="complete"),
                         {"label": "Complete · 2 failed", "kind": "busy"})


if __name__ == "__main__":
    unittest.main()
