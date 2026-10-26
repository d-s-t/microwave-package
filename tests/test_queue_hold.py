"""Pausing a run's queued worker tasks (issue #106): workflow/queue_hold.py,
the copy-to-queue stage, the monitor, the web routes and the run pages.

Every queue here is a temporary folder that nothing else reads, and no
application is started: a test writes only into its own queue and runs
folders. The real worker queue is never named.

Run: python -m unittest discover -s tests
"""
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import psutil
from ansys_analyze_common import queue_common

from microwave_package import paths, pipeline, pipeline_child, webapp
from microwave_package.design.sweep import SweepParameter
from microwave_package.hfss import model_builder
from microwave_package.workflow import monitor, queue_hold, resilient_io
from microwave_package.workflow.run_manager import Run


class QueueHoldCase(unittest.TestCase):
    """A run of three combinations, and a temporary queue for it, with the
    runs folder and the queue both pointed at temporary folders."""

    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)

        runs_dir = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(shutil.rmtree, runs_dir, True)
        saved_runs = paths.RUNS_ROOT
        paths.RUNS_ROOT = runs_dir
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved_runs)

        root = tempfile.mkdtemp(prefix="queue_test_")
        self.addCleanup(shutil.rmtree, root, True)
        self.assertTrue(os.path.abspath(root).startswith(os.path.abspath(tempfile.gettempdir())))
        self.queue = queue_common.QueuePaths(root)
        self.queue.ensure_exist()
        env = mock.patch.dict(os.environ, {"ANSYS_ANALYZE_QUEUE_PATH": root})
        env.start()
        self.addCleanup(env.stop)

        self.settings = dict(pipeline.DEFAULT_ANALYSIS_SETTINGS, use_altium=False)
        self.run = self.make_run()
        self.combos = list(self.run.state["combos"])
        self.client = webapp.app.test_client()

    def make_run(self) -> Run:
        return Run.create([SweepParameter(name="pedestal_depth", values=[0.0, 1.0, 2.0])],
                          dict(self.settings))

    def queue_task(self, combo_id, folder="pending", run=None):
        """Put a task file for a combination in one of the queue's folders,
        and record it on the run the way the copy-to-queue stage does."""
        run = run or self.run
        task_id = f"driven_{run.run_id}_{combo_id}".replace(".", "_")
        os.makedirs(getattr(self.queue, folder), exist_ok=True)
        with open(os.path.join(getattr(self.queue, folder), f"{task_id}.task.json"), "w") as f:
            json.dump({"task_id": task_id, "task_type": "run_setups", "combo": combo_id}, f)
        status = {"pending": "pending", "in_progress": "in_progress", "done": "done"}[folder]
        run.update_combo(combo_id, task_id=task_id, queue_status=status)
        return task_id

    def in_folder(self, folder, task_id) -> bool:
        return os.path.exists(os.path.join(getattr(self.queue, folder), f"{task_id}.task.json"))

    def held_file(self, task_id, run=None):
        return queue_hold.held_task_path(self.queue, (run or self.run).run_id, task_id)

    def status_of(self, combo_id, run=None):
        return Run.load((run or self.run).run_id).state["combos"][combo_id].get("queue_status")

    def make_part(self) -> Run:
        """An eigenmode part of the test run, linked both ways as a driven pair is."""
        part = Run.create([SweepParameter(name="pedestal_depth", values=[0.0, 1.0])],
                          dict(self.settings))
        part.config["driven_run_id"] = self.run.run_id
        part.save_config()
        self.run.config["eigen_run_id"] = part.run_id
        self.run.save_config()
        return part


class HoldAndRelease(QueueHoldCase):
    def test_hold_moves_only_the_tasks_still_waiting(self):
        waiting = self.queue_task(self.combos[0], "pending")
        started = self.queue_task(self.combos[1], "in_progress")
        finished = self.queue_task(self.combos[2], "done")

        report = queue_hold.hold_run(self.run, self.queue)

        self.assertEqual(report["moved"], [waiting])
        self.assertEqual(report["started"], [started])
        self.assertEqual(report["finished"], [finished])
        self.assertTrue(os.path.exists(self.held_file(waiting)))
        self.assertFalse(self.in_folder("pending", waiting))
        self.assertTrue(self.in_folder("in_progress", started))
        self.assertTrue(self.in_folder("done", finished))
        self.assertEqual(self.status_of(self.combos[0]), "held")
        self.assertEqual(self.status_of(self.combos[1]), "in_progress")
        self.assertEqual(self.status_of(self.combos[2]), "done")
        self.assertTrue(Run.load(self.run.run_id).queue_paused)

    def test_held_tasks_read_as_held_and_polling_keeps_them(self):
        waiting = self.queue_task(self.combos[0], "pending")
        queue_hold.hold_run(self.run, self.queue)
        self.assertEqual(queue_common.get_task_status(self.queue, waiting), "unknown")
        self.assertEqual(queue_hold.task_status(self.queue, self.run.run_id, waiting), "held")

        stop = threading.Event()
        thread = threading.Thread(
            target=monitor.monitor_run, args=(self.run, self.queue),
            kwargs={"poll_seconds": 0.01, "log": lambda m: None, "stop_event": stop}, daemon=True)
        thread.start()
        time.sleep(0.3)  # several passes of the monitor
        stop.set()
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(self.run.state["combos"][self.combos[0]]["queue_status"], "held")
        self.assertEqual(self.status_of(self.combos[0]), "held")

    def test_release_gives_the_tasks_back_under_their_names(self):
        waiting = self.queue_task(self.combos[0], "pending")
        with open(os.path.join(self.queue.pending, f"{waiting}.task.json")) as f:
            original = json.load(f)
        queue_hold.hold_run(self.run, self.queue)

        report = queue_hold.release_run(self.run, self.queue)

        self.assertEqual(report["moved"], [waiting])
        self.assertEqual(report["gone"], [])
        self.assertTrue(self.in_folder("pending", waiting))
        with open(os.path.join(self.queue.pending, f"{waiting}.task.json")) as f:
            self.assertEqual(json.load(f), original)
        self.assertEqual(self.status_of(self.combos[0]), "pending")
        self.assertFalse(Run.load(self.run.run_id).queue_paused)
        self.assertFalse(os.path.isdir(os.path.dirname(self.held_file(waiting))))

    def test_release_leaves_a_stray_temporary_file_alone(self):
        waiting = self.queue_task(self.combos[0], "pending")
        queue_hold.hold_run(self.run, self.queue)
        stray = os.path.join(os.path.dirname(self.held_file(waiting)), ".tmp_interrupted.json")
        with open(stray, "w") as f:
            f.write("{")

        report = queue_hold.release_run(self.run, self.queue)

        self.assertEqual(report["moved"], [waiting])
        self.assertEqual(report["failed"], [])
        self.assertTrue(os.path.exists(stray))

    def test_release_reports_a_held_task_whose_file_is_gone(self):
        waiting = self.queue_task(self.combos[0], "pending")
        queue_hold.hold_run(self.run, self.queue)
        os.remove(self.held_file(waiting))

        report = queue_hold.release_run(self.run, self.queue)

        self.assertEqual(report["gone"], [waiting])
        self.assertEqual(report["moved"], [])
        self.assertIn("not found", queue_hold.describe([report], "release"))
        self.assertFalse(self.run.queue_paused)

    def test_a_task_queued_while_paused_is_held_then_released_with_the_rest(self):
        queue_hold.hold_run(self.run, self.queue)  # nothing queued yet: only the flag is set
        self.assertTrue(self.run.queue_paused)

        combo_id = self.combos[1]
        project = model_builder.project_file_for(self.run, combo_id)
        os.makedirs(os.path.dirname(project), exist_ok=True)
        with open(project, "w") as f:
            f.write("")
        model_builder.queue_model(combo_id, {}, self.run, self.queue, {}, log=lambda m: None)

        task_id = self.run.state["combos"][combo_id]["task_id"]
        self.assertEqual(self.status_of(combo_id), "held")
        self.assertTrue(os.path.exists(self.held_file(task_id)))
        self.assertFalse(self.in_folder("pending", task_id))
        self.assertTrue(os.path.isdir(os.path.join(self.queue.projects, task_id)))

        queue_hold.release_run(self.run, self.queue)
        self.assertTrue(self.in_folder("pending", task_id))
        self.assertEqual(self.status_of(combo_id), "pending")

    def test_a_task_queued_after_resume_goes_to_pending(self):
        queue_hold.hold_run(self.run, self.queue)
        queue_hold.release_run(self.run, self.queue)

        combo_id = self.combos[0]
        project = model_builder.project_file_for(self.run, combo_id)
        os.makedirs(os.path.dirname(project), exist_ok=True)
        with open(project, "w") as f:
            f.write("")
        model_builder.queue_model(combo_id, {}, self.run, self.queue, {}, log=lambda m: None)

        task_id = self.run.state["combos"][combo_id]["task_id"]
        self.assertTrue(self.in_folder("pending", task_id))
        self.assertEqual(self.status_of(combo_id), "pending")


class Refusals(QueueHoldCase):
    def test_a_task_the_worker_has_started_cannot_be_paused(self):
        started = self.queue_task(self.combos[0], "in_progress")

        report = queue_hold.hold_run(self.run, self.queue)

        self.assertEqual(report["started"], [started])
        self.assertTrue(self.in_folder("in_progress", started))
        self.assertIn("cannot be paused", queue_hold.describe([report], "hold"))

    def test_nothing_moves_while_the_queue_is_unreachable(self):
        waiting = self.queue_task(self.combos[0], "pending")
        with mock.patch.object(resilient_io, "path_available", return_value=False):
            report = queue_hold.hold_run(self.run, self.queue)

        self.assertIsNotNone(report["error"])
        self.assertFalse(Run.load(self.run.run_id).queue_paused)
        self.assertTrue(self.in_folder("pending", waiting))


class Requests(QueueHoldCase):
    def test_a_request_is_taken_once(self):
        queue_hold.write_request(self.run.run_dir, "release")
        self.assertEqual(queue_hold.take_request(self.run.run_dir), "release")
        self.assertIsNone(queue_hold.take_request(self.run.run_dir))
        with self.assertRaises(ValueError):
            queue_hold.write_request(self.run.run_dir, "stop")

    def test_a_driven_run_is_changed_by_its_driver_not_by_the_web_app(self):
        waiting = self.queue_task(self.combos[0], "pending")
        alive = threading.Event()
        thread = threading.Thread(target=alive.wait, daemon=True)
        thread.start()
        self.addCleanup(alive.set)
        webapp._active_threads[self.run.run_id] = thread
        self.addCleanup(webapp._active_threads.pop, self.run.run_id, None)

        response = self.client.post(f"/runs/{self.run.run_id}/queue/hold")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(queue_hold.request_pending(self.run.run_dir))
        self.assertFalse(Run.load(self.run.run_id).queue_paused)
        self.assertTrue(self.in_folder("pending", waiting))

        # The driving process applies the request (its watcher calls this).
        action = pipeline_child.apply_queue_request(Run.load(self.run.run_id), self.queue)

        self.assertEqual(action, "hold")
        self.assertFalse(queue_hold.request_pending(self.run.run_dir))
        self.assertTrue(Run.load(self.run.run_id).queue_paused)
        self.assertTrue(os.path.exists(self.held_file(waiting)))
        self.assertEqual(self.status_of(self.combos[0]), "held")

    def test_the_queue_is_not_probed_while_nothing_is_asked(self):
        with mock.patch.object(resilient_io, "path_available") as probe:
            self.assertIsNone(pipeline_child.apply_queue_request(self.run, self.queue))
        probe.assert_not_called()

    def test_the_driver_asks_a_part_that_another_process_drives(self):
        part = self.make_part()
        in_run = self.queue_task(self.combos[0], "pending")
        in_part = self.queue_task(list(part.state["combos"])[0], "pending", run=part)
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: (other.kill(), other.wait()))
        with open(part.driver_path, "w") as f:  # the part's own process claims it
            json.dump({"pid": other.pid, "started": psutil.Process(other.pid).create_time(),
                       "host": socket.gethostname()}, f)
        queue_hold.write_request(self.run.run_dir, "hold")

        action = pipeline_child.apply_queue_request(Run.load(self.run.run_id), self.queue)

        self.assertEqual(action, "hold")
        self.assertTrue(os.path.exists(self.held_file(in_run)))    # the run's own tasks: held here
        self.assertTrue(queue_hold.request_pending(part.run_dir))   # the part's process is asked
        self.assertTrue(self.in_folder("pending", in_part))         # nothing of the part is written
        self.assertFalse(Run.load(part.run_id).queue_paused)


class Routes(QueueHoldCase):
    def test_pause_and_resume_an_idle_run_from_its_page(self):
        waiting = self.queue_task(self.combos[0], "pending")

        response = self.client.post(f"/runs/{self.run.run_id}/queue/hold")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(os.path.exists(self.held_file(waiting)))
        self.assertTrue(Run.load(self.run.run_id).queue_paused)
        page = self.client.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn("Resume queue", page)
        self.assertIn("held out of the worker queue", page)

        response = self.client.post(f"/runs/{self.run.run_id}/queue/release")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.in_folder("pending", waiting))
        self.assertFalse(Run.load(self.run.run_id).queue_paused)
        page = self.client.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn("Pause queue", page)

    def test_pausing_a_run_pauses_its_eigenmode_part_too(self):
        eigen = Run.create([SweepParameter(name="pedestal_depth", values=[0.0, 1.0])],
                           dict(self.settings))
        eigen.config["driven_run_id"] = self.run.run_id
        eigen.save_config()
        self.run.config["eigen_run_id"] = eigen.run_id
        self.run.save_config()
        in_run = self.queue_task(self.combos[0], "pending")
        in_part = self.queue_task(list(eigen.state["combos"])[0], "pending", run=eigen)

        self.client.post(f"/runs/{eigen.run_id}/queue/hold")  # from the part's own id

        self.assertTrue(os.path.exists(self.held_file(in_run)))
        self.assertTrue(os.path.exists(self.held_file(in_part, run=eigen)))
        self.assertTrue(Run.load(eigen.run_id).queue_paused)

    def test_a_part_with_its_own_driver_is_asked_not_written(self):
        part = self.make_part()
        in_run = self.queue_task(self.combos[0], "pending")
        in_part = self.queue_task(list(part.state["combos"])[0], "pending", run=part)
        alive = threading.Event()
        thread = threading.Thread(target=alive.wait, daemon=True)
        thread.start()
        self.addCleanup(alive.set)
        webapp._active_threads[part.run_id] = thread  # the part runs in a process of its own
        self.addCleanup(webapp._active_threads.pop, part.run_id, None)

        response = self.client.post(f"/runs/{self.run.run_id}/queue/hold")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(os.path.exists(self.held_file(in_run)))      # the run's tasks: held here
        self.assertTrue(queue_hold.request_pending(part.run_dir))      # the part is asked
        self.assertTrue(self.in_folder("pending", in_part))           # its task is left alone
        self.assertFalse(Run.load(part.run_id).queue_paused)          # its state is untouched
        self.assertEqual(self.status_of(list(part.state["combos"])[0], run=part), "pending")

    def test_the_run_list_counts_held_tasks_including_a_parts(self):
        eigen = Run.create([SweepParameter(name="pedestal_depth", values=[0.0, 1.0])],
                           dict(self.settings))
        eigen.config["driven_run_id"] = self.run.run_id
        eigen.save_config()
        self.run.config["eigen_run_id"] = eigen.run_id
        self.run.save_config()
        self.queue_task(self.combos[0], "pending")
        self.queue_task(list(eigen.state["combos"])[0], "pending", run=eigen)
        queue_hold.hold_run(self.run, self.queue)
        queue_hold.hold_run(eigen, self.queue)

        summary = next(r for r in Run.list_runs() if r["run_id"] == self.run.run_id)

        self.assertEqual(summary["held"], 2)
        self.assertNotIn(eigen.run_id, [r["run_id"] for r in Run.list_runs()])

    def test_the_index_says_what_is_held_and_only_when_something_is(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("Tasks are held out of the worker queue", page)

        self.queue_task(self.combos[0], "pending")
        queue_hold.hold_run(self.run, self.queue)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Tasks are held out of the worker queue", page)
        self.assertIn(f"/runs/{self.run.run_id}", page)

    def test_deleting_the_run_removes_its_held_files(self):
        waiting = self.queue_task(self.combos[0], "pending")
        queue_hold.hold_run(self.run, self.queue)
        held_folder = os.path.dirname(self.held_file(waiting))
        self.assertTrue(os.path.isdir(held_folder))

        response = self.client.post(f"/runs/{self.run.run_id}/delete",
                                    data={"confirm_run_id": self.run.run_id})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(os.path.exists(self.held_file(waiting)))
        self.assertFalse(os.path.isdir(held_folder))
        self.assertFalse(os.path.isdir(self.run.run_dir))


if __name__ == "__main__":
    unittest.main()
