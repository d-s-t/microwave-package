"""Tests for how the pipeline shares the one AEDT it may run.

The web app drives several runs at once, each in a thread of its own,
and the Student licence runs a single AEDT. On 2026-09-29 two runs
post-processed at the same moment and one of them failed with PyAEDT's
"Version argument is not valid".

Run: python -m unittest discover -s tests
"""
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from microwave_package.hfss import model_builder
from microwave_package.workflow import postprocess


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
                "post_status": "failed", "object_names": {"chip": "chip_substrate"},
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
            self.assertEqual(seen["project_file"], project)
            self.assertEqual(seen["objects"], {"chip": "chip_substrate"})
            self.assertEqual(seen["solution_type"], "Eigenmode")
            self.assertTrue(seen["post_processing"], "the solver's own post-processing ops")

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


if __name__ == "__main__":
    unittest.main()
