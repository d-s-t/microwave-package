"""Tests for deleting a run, and for saying so when it is refused.

Delete is guarded twice over -- the run's own id has to be typed in, and
a run that is still going is refused outright -- and both guards used to
fail silently: the server redirected back to the same page with the
answer only in its own log. A button that does nothing and explains
nothing is indistinguishable from a broken one, which is exactly how it
was reported.

Run: python -m unittest discover -s tests
"""
import logging
import os
import tempfile
import threading
import unittest

from microwave_package import paths, webapp
from microwave_package.design.sweep import SweepParameter
from microwave_package.workflow.run_manager import Run


class DeleteTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.client = webapp.app.test_client()

        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)

        self.run = Run.create(
            [SweepParameter(name="pedestal_depth", values=[0.0, 1.0])],
            dict(webapp.pipeline.DEFAULT_ANALYSIS_SETTINGS),
        )
        self.addCleanup(webapp._active_threads.pop, self.run.run_id, None)
        self.addCleanup(webapp._stop_events.pop, self.run.run_id, None)

    def pretend_it_is_running(self):
        alive = threading.Event()
        thread = threading.Thread(target=alive.wait, daemon=True)
        thread.start()
        self.addCleanup(alive.set)
        webapp._active_threads[self.run.run_id] = thread

    def delete(self, confirm):
        return self.client.post(
            f"/runs/{self.run.run_id}/delete", data={"confirm_run_id": confirm}
        )

    def followed(self, response) -> str:
        """The page the redirect lands on, which is where the person who
        pressed the button is looking."""
        return self.client.get(response.headers["Location"]).get_data(as_text=True)

    # -- when it should happen -----------------------------------------
    def test_the_typed_id_is_what_deletes_it(self):
        self.assertTrue(os.path.isdir(self.run.run_dir))
        self.delete(self.run.run_id)
        self.assertFalse(os.path.exists(self.run.run_dir))

    # -- when it should not --------------------------------------------
    def test_a_run_that_is_still_going_is_not_deleted(self):
        self.pretend_it_is_running()
        self.delete(self.run.run_id)
        self.assertTrue(os.path.isdir(self.run.run_dir))

    def test_being_told_to_stop_it_first(self):
        self.pretend_it_is_running()
        self.assertIn("Stop it first", self.followed(self.delete(self.run.run_id)))

    def test_a_confirmation_that_does_not_match_deletes_nothing(self):
        self.delete("run_00000000_000000")
        self.assertTrue(os.path.isdir(self.run.run_dir))

    def test_being_told_the_confirmation_did_not_match(self):
        page = self.followed(self.delete("not the run id"))
        self.assertIn("did not match", page)

    def test_an_empty_confirmation_deletes_nothing(self):
        self.delete("")
        self.assertTrue(os.path.isdir(self.run.run_dir))

    # -- the page around it --------------------------------------------
    def test_the_delete_section_is_offered_even_while_the_run_is_going(self):
        """It is disabled rather than absent: a page loaded during a run
        used to have no delete section at all, and never grew one when
        the run stopped."""
        self.pretend_it_is_running()
        page = self.client.get(f"/runs/{self.run.run_id}").get_data(as_text=True)
        self.assertIn("Delete this run", page)
        self.assertIn("delete-blocked-note", page)

    # -- and what it leaves in the queue -------------------------------
    def test_a_task_is_cleared_wherever_it_has_got_to(self):
        """The worker moves a task between folders as it works on it, so
        clearing only the one it started in leaves a run that no longer
        exists being analysed anyway -- which is what happened."""
        import os
        from ansys_analyze_common import queue_common

        root = tempfile.mkdtemp(prefix="queue_")
        queue = queue_common.QueuePaths(root)
        queue.ensure_exist()
        task_id = "eigenmode_20260916_121513_000001"
        landed = []
        for folder in (queue.pending, queue.in_progress, queue.done, queue.failed):
            path = os.path.join(folder, f"{task_id}.task.json")
            with open(path, "w") as f:
                f.write("{}")
            landed.append(path)
        project_dir = os.path.join(queue.projects, task_id)
        os.makedirs(project_dir, exist_ok=True)

        webapp._clear_queued_task(queue, task_id)

        for path in landed:
            self.assertFalse(os.path.exists(path), f"{path} was left behind")
        self.assertFalse(os.path.exists(project_dir), "the project folder was left behind")

    def test_clearing_a_task_that_is_not_in_the_queue_is_fine(self):
        from ansys_analyze_common import queue_common

        queue = queue_common.QueuePaths(tempfile.mkdtemp(prefix="queue_"))
        queue.ensure_exist()
        webapp._clear_queued_task(queue, "eigenmode_20260916_121513_000002")

    def test_nothing_hides_behind_a_browser_dialog(self):
        """The delete used to be cancelled by a confirm() dialog, which a
        browser that suppresses dialogs answers "no" to -- silently, so
        the button simply did nothing. Matched on the call itself rather
        than the name, which the comment explaining this uses too."""
        page = self.client.get(f"/runs/{self.run.run_id}").get_data(as_text=True)
        self.assertNotIn('confirm("', page)


if __name__ == "__main__":
    unittest.main()
