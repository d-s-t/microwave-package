"""Tests for how the pipeline shares the one AEDT it may run.

The web app drives several runs at once, each in a thread of its own,
and the Student licence runs a single AEDT. On 2026-09-29 two runs
post-processed at the same moment and one of them failed with PyAEDT's
"Version argument is not valid".

Run: python -m unittest discover -s tests
"""
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from microwave_package.hfss import model_builder
from microwave_package.workflow import postprocess


def setUpModule():
    # The decorated functions also take the machine-wide AEDT lock file;
    # keep these tests off the PC's own copy of it.
    global _lock_folder, _lock_env
    _lock_folder = tempfile.mkdtemp(prefix="aedt_lock_test_")
    _lock_env = mock.patch.dict(
        os.environ, {model_builder.AEDT_LOCK_ENV: os.path.join(_lock_folder, "aedt_session.lock")})
    _lock_env.start()


def tearDownModule():
    _lock_env.stop()
    shutil.rmtree(_lock_folder, ignore_errors=True)


class TestTheVersionHandedToPyAEDT(unittest.TestCase):
    def test_a_run_that_names_one_keeps_it(self):
        self.assertEqual(model_builder.aedt_version_for("2024.1"), "2024.1")

    def test_otherwise_the_newest_installed_never_none(self):
        """Given None, PyAEDT reads a process-wide setting that is
        "2025.2SV" while any Desktop is starting, and refuses it."""
        with mock.patch.object(model_builder, "get_installed_versions",
                               return_value=["2025.2", "2024.1"]):
            self.assertEqual(model_builder.aedt_version_for(None), "2025.2")
            self.assertEqual(model_builder.aedt_version_for(""), "2025.2")


class TestOneAedtAtATime(unittest.TestCase):
    def test_both_entry_points_hold_the_lock(self):
        """The model build and the post-processing are the only places
        that start PyAEDT."""
        for fn in (model_builder.build_model, postprocess.postprocess_task):
            self.assertTrue(hasattr(fn, "__wrapped__"), f"{fn.__name__} does not take AEDT_LOCK")

    def test_two_runs_take_turns(self):
        events = []

        @model_builder.one_aedt_at_a_time
        def work(name):
            events.append(f"{name} starts")
            time.sleep(0.05)
            events.append(f"{name} ends")

        threads = [threading.Thread(target=work, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        first = events[0].split()[0]
        second = "b" if first == "a" else "a"
        self.assertEqual(events, [f"{first} starts", f"{first} ends",
                                  f"{second} starts", f"{second} ends"])

    def test_a_failure_lets_the_next_one_in(self):
        @model_builder.one_aedt_at_a_time
        def fails():
            raise RuntimeError("AEDT said no")

        with self.assertRaises(RuntimeError):
            fails()
        self.assertTrue(model_builder.AEDT_LOCK.acquire(timeout=1))
        model_builder.AEDT_LOCK.release()


class FakeRun:
    run_id = "r"

    def __init__(self, root, combos):
        self.root = root
        self.state = {"combos": combos}

    def combo_dir(self, combo_id):
        return os.path.join(self.root, combo_id)

    def update_combo(self, combo_id, **fields):
        self.state["combos"][combo_id].update(fields)


class TestRetryingAfterTheFetch(unittest.TestCase):
    """Fetching a combination's files clears its task off the queue, so a
    post-processing that failed after the fetch went looking for the
    task there, was marked "task file not found", and was never tried
    again -- which is where the race above left its combination."""

    def test_it_is_retried_from_the_local_files(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "c"))
            project = os.path.join(root, "c", "r__c.aedt")
            open(project, "w").close()
            run = FakeRun(root, {"c": {
                "task_id": "t1", "queue_status": "done", "fetch_status": "done",
                "post_status": "failed", "post_error": "Version argument is not valid.",
                "object_names": {"chip": "chip_substrate"},
            }})
            seen = {}

            def post_process(task, *args, **kwargs):
                seen.update(task)
                return {"rows": [], "row_count": 0}

            with mock.patch.object(postprocess, "postprocess_task", post_process), \
                    mock.patch.object(postprocess.queue_common, "read_task",
                                      side_effect=AssertionError("the queue has nothing to read")):
                postprocess.postprocess_run(run, queue=None, log=lambda _m: None,
                                            analysis_settings={"solution_type": "Eigenmode"})

            self.assertEqual(run.state["combos"]["c"]["post_status"], "done")
            self.assertIsNone(run.state["combos"]["c"]["post_error"],
                              "the error from before is history once the retry succeeds")
            self.assertEqual(seen["project_file"], project)
            self.assertEqual(seen["objects"], {"chip": "chip_substrate"})
            self.assertEqual(seen["solution_type"], "Eigenmode")
            self.assertTrue(seen["post_processing"], "the solver's own post-processing ops")

    def test_the_design_is_named_from_the_project(self):
        """Unnamed, PyAEDT misses a driven design, inserts an empty one,
        and the read fails with "no setup to post-process" -- which is
        what run_20260929_102232 did on its retry."""
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "c"))
            with open(os.path.join(root, "c", "r__c.aedt"), "w") as f:
                f.write("$begin 'AnsoftProject'\n\t$begin 'HFSSModel'\n\t\tName='HFSS_0CZ'\n")
            run = FakeRun(root, {"c": {
                "task_id": "t1", "queue_status": "done", "fetch_status": "done",
                "post_status": "failed",
            }})
            seen = {}
            with mock.patch.object(postprocess, "postprocess_task",
                                   lambda task, *a, **k: seen.update(task) or {"rows": [], "row_count": 0}):
                postprocess.postprocess_run(run, queue=None, log=lambda _m: None,
                                            analysis_settings={"solution_type": "DrivenModal"})
            self.assertEqual(seen["design_name"], "HFSS_0CZ")

    def test_from_wherever_the_run_recorded_them(self):
        with tempfile.TemporaryDirectory() as root:
            elsewhere = os.path.join(root, "c", "analysis")
            os.makedirs(elsewhere)
            open(os.path.join(elsewhere, "r__c.aedt"), "w").close()
            run = FakeRun(root, {"c": {
                "task_id": "t1", "queue_status": "done", "fetch_status": "done",
                "post_status": "failed", "analysis_dir": elsewhere,
            }})
            seen = {}
            with mock.patch.object(postprocess, "postprocess_task",
                                   lambda task, *a, **k: seen.update(task) or {"rows": [], "row_count": 0}):
                postprocess.postprocess_run(run, queue=None, log=lambda _m: None,
                                            analysis_settings={"solution_type": "Eigenmode"})
            self.assertEqual(seen["project_file"], os.path.join(elsewhere, "r__c.aedt"))

    def test_files_that_are_gone_fail_it_instead_of_waiting_forever(self):
        """A missing local folder is an OSError, which call_with_retry
        waits out as though the share were down -- forever."""
        with tempfile.TemporaryDirectory() as root:
            run = FakeRun(root, {"c": {
                "task_id": "t1", "queue_status": "done", "fetch_status": "done", "post_status": "failed",
            }})
            with mock.patch.object(postprocess, "postprocess_task",
                                   side_effect=AssertionError("nothing to open")):
                handled = postprocess.postprocess_run(run, queue=None, log=lambda _m: None,
                                                      analysis_settings={"solution_type": "Eigenmode"})
            self.assertEqual(handled, 1)
            self.assertEqual(run.state["combos"]["c"]["post_status"], "failed")
            self.assertIn("no longer at", run.state["combos"]["c"]["post_error"])

    def test_but_not_on_every_poll(self):
        """The poll comes round every few seconds while analyses are out,
        and a retry reopens the project in AEDT: one that fails every
        time must wait for the final pass."""
        run = FakeRun("unused", {"c": {
            "task_id": "t1", "queue_status": "done", "fetch_status": "done", "post_status": "failed",
        }})
        with mock.patch.object(postprocess, "postprocess_task",
                               side_effect=AssertionError("retried while polling")):
            handled = postprocess.postprocess_run(run, queue=None, log=lambda _m: None,
                                                  analysis_settings={"solution_type": "Eigenmode"},
                                                  announce_idle=False, retry_results=False)
        self.assertEqual(handled, 0)



class TestADoneCombinationShowsNoError(unittest.TestCase):
    """Its details used to show the error from before a retry that
    succeeded, right beside "Done"."""

    def setUp(self):
        import logging

        from microwave_package import paths, webapp
        from microwave_package.design.sweep import SweepParameter
        from microwave_package.workflow.run_manager import Run

        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)
        self.client = webapp.app.test_client()
        self.run = Run.create([SweepParameter(name="pedestal_depth", values=[0.0])],
                              dict(webapp.pipeline.DEFAULT_ANALYSIS_SETTINGS))
        self.combo_id = next(iter(self.run.state["combos"]))

    def error_shown(self):
        response = self.client.get(f"/runs/{self.run.run_id}/combos/{self.combo_id}/details.json")
        self.assertEqual(response.status_code, 200)
        return response.get_json()["error"]

    def test_an_error_from_before_a_successful_retry_is_not_shown(self):
        self.run.update_combo(self.combo_id, post_status="done",
                              post_error="Version argument is not valid.")
        self.assertIsNone(self.error_shown())

    def test_a_failed_one_still_says_why(self):
        self.run.update_combo(self.combo_id, post_status="failed", post_error="it broke")
        self.assertEqual(self.error_shown(), "it broke")


class TestLockFilesStayOnTheQueue(unittest.TestCase):
    def test_a_lock_file_is_neither_copied_nor_reported_missing(self):
        root = tempfile.mkdtemp(prefix="queue_lock_test_")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        source = os.path.join(root, "queue", "task")
        os.makedirs(os.path.join(source, "x.aedtresults"))
        for relative, text in (("x.aedt", "project"),
                               (os.path.join("x.aedtresults", "sol.dat"), "data"),
                               ("x.aedt.lock", "pid 1 ip 10.0.0.1 port 9")):
            with open(os.path.join(source, relative), "w") as handle:
                handle.write(text)
        destination = os.path.join(root, "local")

        missing = postprocess.copy_tree_verified(source, destination)

        self.assertEqual(missing, [])
        self.assertFalse(os.path.exists(os.path.join(destination, "x.aedt.lock")))
        self.assertTrue(os.path.exists(os.path.join(destination, "x.aedt")))
        self.assertTrue(os.path.exists(os.path.join(destination, "x.aedtresults", "sol.dat")))


if __name__ == "__main__":
    unittest.main()
