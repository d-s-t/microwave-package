"""Tests for building the AEDT model separately from handing it over.

The two halves need different things: the build needs AEDT and no
network, the handoff needs the share and no AEDT. Keeping them apart is
what lets a whole run be prepared with the queue disconnected and handed
over later without rebuilding anything.

AEDT is never started here. The decision that matters -- whether an
existing model can be handed over as-is -- lives in `reusable_model`,
which is tested directly, so a wrong answer cannot be mistaken for AEDT
failing to launch.

Run: python -m unittest discover -s tests
"""
import os
import tempfile
import unittest

from eigen_mode_analyze import model_builder


class FakeRun:
    """Just enough of Run for the guards and the bookkeeping."""

    def __init__(self, combo_id="c1", **combo):
        self.combo_id = combo_id
        self.state = {"combos": {combo_id: dict(combo)}}
        self._dir = tempfile.mkdtemp(prefix="combo_")

    def combo_dir(self, combo_id):
        return self._dir

    def update_combo(self, combo_id, **fields):
        self.state["combos"][combo_id].update(fields)


class TestReusableModel(unittest.TestCase):
    """Whether an existing model can be handed over as-is.

    This is the decision that makes resuming an offline-prepared run
    cheap, and it is tested directly rather than through `build_model`
    so that a wrong answer cannot be mistaken for AEDT failing to start.
    """

    def test_an_already_queued_combination_is_not_rebuilt(self):
        run = FakeRun(task_id="task_1", object_names={"vacuum": "vacuum"})
        self.assertEqual(
            model_builder.reusable_model(run, "c1", log=lambda _m: None),
            {"vacuum": "vacuum"},
        )

    def test_an_already_built_model_is_reused(self):
        """The point of the split: resuming must copy the project across,
        not build it again."""
        run = FakeRun()
        project = os.path.join(run.combo_dir("c1"), "m.aedt")
        open(project, "w").close()
        run.update_combo("c1", model_status="built", project_path=project,
                         object_names={"pcb": "pcb_substrate"})
        self.assertEqual(
            model_builder.reusable_model(run, "c1", log=lambda _m: None),
            {"pcb": "pcb_substrate"},
        )

    def test_a_fresh_combination_has_nothing_to_reuse(self):
        run = FakeRun(step_status="done")
        self.assertIsNone(model_builder.reusable_model(run, "c1", log=lambda _m: None))

    def test_a_missing_project_file_forces_a_rebuild(self):
        """A deleted project makes the record stale, whatever it says."""
        run = FakeRun(model_status="built", project_path=r"C:\gone\m.aedt",
                      object_names={"pcb": "pcb_substrate"})
        self.assertIsNone(model_builder.reusable_model(run, "c1", log=lambda _m: None))

    def test_built_without_body_names_forces_a_rebuild(self):
        """The names are what the task is made from; without them there
        is nothing to hand over."""
        run = FakeRun()
        project = os.path.join(run.combo_dir("c1"), "m.aedt")
        open(project, "w").close()
        run.update_combo("c1", model_status="built", project_path=project)
        self.assertIsNone(model_builder.reusable_model(run, "c1", log=lambda _m: None))

    def test_a_stale_record_says_why_it_is_rebuilding(self):
        run = FakeRun(model_status="built", project_path=r"C:\gone\m.aedt",
                      object_names={"pcb": "pcb_substrate"})
        messages = []
        model_builder.reusable_model(run, "c1", log=messages.append)
        self.assertTrue(any("rebuilding" in m for m in messages))


class TestRememberBuiltModel(unittest.TestCase):
    def test_records_what_a_later_handoff_will_need(self):
        """Body names are only known while the model is being built. If
        they were not stored, a deferred handoff would have to reopen the
        project just to rediscover them."""
        run = FakeRun()
        names = model_builder._remember_built_model(
            run, "c1", r"C:\runs\c1\m.aedt", {"vacuum": "vacuum"},
            ["port_1", "port_2"], lambda _m: None,
        )
        combo = run.state["combos"]["c1"]
        self.assertEqual(combo["model_status"], "built")
        self.assertEqual(combo["project_path"], r"C:\runs\c1\m.aedt")
        self.assertEqual(combo["object_names"], {"vacuum": "vacuum"})
        self.assertEqual(combo["ports"], ["port_1", "port_2"])
        self.assertEqual(names, {"vacuum": "vacuum"})


class TestQueueModelSkips(unittest.TestCase):
    def test_an_already_queued_combination_is_not_queued_twice(self):
        run = FakeRun(task_id="task_1")
        messages = []
        model_builder.queue_model("c1", "x.STEP", run, object_names={}, queue=None,
                                  log=messages.append)
        self.assertTrue(any("already queued" in m for m in messages))


class TestTheSplitItself(unittest.TestCase):
    def test_build_model_cannot_touch_the_queue(self):
        """Structural, and the whole point: if a queue argument ever
        reappears here, building offline stops being possible."""
        import inspect

        self.assertNotIn("queue", inspect.signature(model_builder.build_model).parameters)

    def test_queue_model_does_not_need_aedt_settings(self):
        import inspect

        params = inspect.signature(model_builder.queue_model).parameters
        self.assertIn("queue", params)
        self.assertNotIn("analysis_settings", params)
        self.assertNotIn("aedt_version", params)

    def test_build_and_queue_still_exists_for_the_online_case(self):
        import inspect

        params = inspect.signature(model_builder.build_and_queue).parameters
        self.assertIn("queue", params)
        self.assertIn("analysis_settings", params)


if __name__ == "__main__":
    unittest.main()
