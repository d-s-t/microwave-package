"""Tests for what a run reports about where it is.

Three programs work on three different combinations at once, so "the run
is in stage 2" stopped meaning anything -- progress is now counted per
combination and per phase. What has to hold: the phases are read from
the fields the stages actually record, a phase nobody has reached is not
quietly reported as progress, and a run recorded before a phase had a
field of its own still reads right.

Run: python -m unittest discover -s tests
"""
import unittest

from microwave_package.workflow.run_manager import (
    PHASE_KEYS, combo_state, phase_states, progress_of)


def combo(**fields):
    return dict(fields)


def state(*combos):
    return {"combos": {f"c{i}": c for i, c in enumerate(combos)}}


class TestOneCombinationsPhases(unittest.TestCase):
    def test_a_fresh_combination_has_not_started_anything(self):
        self.assertEqual(
            set(phase_states(combo()).values()), {"pending"}
        )
        self.assertEqual(combo_state(phase_states(combo())), "pending")

    def test_a_layout_being_drawn_shows_only_that(self):
        states = phase_states(combo(altium_status="running"))
        self.assertEqual(states["layout"], "running")
        self.assertEqual(states["geometry"], "pending")
        self.assertEqual(states["model"], "pending")

    def test_the_layout_and_the_geometry_are_independent(self):
        """They run at the same time on different combinations, so one
        being finished says nothing about the other."""
        states = phase_states(combo(altium_status="done", step_status="running"))
        self.assertEqual(states["layout"], "done")
        self.assertEqual(states["geometry"], "running")
        states = phase_states(combo(altium_status="running", step_status="done"))
        self.assertEqual(states["layout"], "running")
        self.assertEqual(states["geometry"], "done")

    def test_a_model_being_built_is_its_own_phase(self):
        states = phase_states(combo(altium_status="done", step_status="done",
                                    model_status="building"))
        self.assertEqual(states["model"], "running")
        self.assertEqual(states["upload"], "pending")

    def test_a_queued_task_is_waiting_rather_than_running(self):
        """Handed over and out of this machine's hands, but nobody is
        solving it yet -- which is not the same as being solved."""
        states = phase_states(combo(model_status="built", handoff_status="done",
                                    task_id="t1", queue_status="pending"))
        self.assertEqual(states["upload"], "done")
        self.assertEqual(states["analysis"], "waiting")
        self.assertEqual(combo_state(states), "running")

    def test_a_deferred_handoff_shows_at_the_copy(self):
        """Built but the share was down: the model is finished and the
        copy is what is outstanding."""
        states = phase_states(combo(model_status="built", handoff_status="deferred"))
        self.assertEqual(states["model"], "done")
        self.assertEqual(states["upload"], "waiting")
        self.assertEqual(states["analysis"], "pending")

    def test_the_analysed_files_coming_back_is_its_own_phase(self):
        states = phase_states(combo(queue_status="done", fetch_status="running"))
        self.assertEqual(states["analysis"], "done")
        self.assertEqual(states["download"], "running")
        self.assertEqual(states["results"], "pending")

    def test_a_finished_combination_is_done_everywhere(self):
        states = phase_states(combo(
            altium_status="done", step_status="done", model_status="built",
            handoff_status="done", task_id="t1", queue_status="done",
            fetch_status="done", post_status="done",
        ))
        self.assertEqual(set(states.values()), {"done"})
        self.assertEqual(combo_state(states), "done")

    def test_a_skipped_combination_skips_every_phase(self):
        states = phase_states(combo(skip_reason="the layout does not fit"))
        self.assertEqual(set(states.values()), {"skipped"})
        self.assertEqual(combo_state(states), "skipped")

    def test_a_run_with_altium_off_has_no_layout_phase(self):
        states = phase_states(combo(step_status="done"), use_altium=False)
        self.assertEqual(states["layout"], "skipped")
        self.assertEqual(combo_state(states), "running")


class TestFailureAnywhereCounts(unittest.TestCase):
    """A combination that failed early used to sit in "pending" forever,
    because only the analysis and the post-processing were looked at."""

    def test_a_failed_layout_is_a_failed_combination(self):
        self.assertEqual(combo_state(phase_states(combo(altium_status="failed"))), "failed")

    def test_a_failed_step_is_a_failed_combination(self):
        self.assertEqual(combo_state(phase_states(combo(step_status="failed"))), "failed")

    def test_a_failed_model_is_a_failed_combination(self):
        self.assertEqual(combo_state(phase_states(combo(model_status="failed"))), "failed")

    def test_a_failed_analysis_is_still_a_failed_combination(self):
        self.assertEqual(combo_state(phase_states(combo(queue_status="failed"))), "failed")


class TestRunsFromBeforeThePhasesExisted(unittest.TestCase):
    """Nothing re-records an old run, so the phases have to be readable
    from what it does have."""

    def test_a_queued_combination_counts_its_earlier_phases_as_done(self):
        states = phase_states(combo(step_status="done", task_id="t1",
                                    queue_status="in_progress"))
        self.assertEqual(states["layout"], "done")
        self.assertEqual(states["model"], "done")
        self.assertEqual(states["upload"], "done")
        self.assertEqual(states["analysis"], "running")

    def test_a_post_processed_combination_reads_as_finished(self):
        states = phase_states(combo(step_status="done", task_id="t1",
                                    queue_status="done", post_status="done"))
        self.assertEqual(set(states.values()), {"done"})

    def test_nothing_is_invented_for_a_combination_that_never_started(self):
        states = phase_states(combo(step_status="done"))
        self.assertEqual(states["model"], "pending")
        self.assertEqual(states["analysis"], "pending")


class TestRunProgress(unittest.TestCase):
    def test_the_totals_are_counted_over_buildable_combinations(self):
        progress = progress_of(state(
            combo(post_status="done", queue_status="done"),
            combo(step_status="failed"),
            combo(altium_status="running"),
            combo(skip_reason="does not fit"),
        ))
        self.assertEqual(progress["grand_total"], 4)
        self.assertEqual(progress["skipped"], 1)
        self.assertEqual(progress["total"], 3)
        self.assertEqual(progress["done"], 1)
        self.assertEqual(progress["failed"], 1)
        self.assertEqual(progress["pending"], 1)

    def test_each_phase_is_counted_on_its_own(self):
        progress = progress_of(state(
            combo(altium_status="done", step_status="done", model_status="building"),
            combo(altium_status="done", step_status="running"),
            combo(altium_status="running"),
        ))
        phases = {phase["key"]: phase for phase in progress["phases"]}
        self.assertEqual(phases["layout"]["done"], 2)
        self.assertEqual(phases["layout"]["running"], 1)
        self.assertEqual(phases["geometry"]["done"], 1)
        self.assertEqual(phases["geometry"]["running"], 1)
        self.assertEqual(phases["geometry"]["pending"], 1)
        self.assertEqual(phases["model"]["running"], 1)
        self.assertEqual(phases["model"]["pending"], 2)

    def test_every_phase_adds_up_to_the_buildable_total(self):
        progress = progress_of(state(
            combo(post_status="done", queue_status="done"),
            combo(model_status="failed"),
            combo(task_id="t1", queue_status="pending"),
            combo(),
            combo(skip_reason="does not fit"),
        ))
        for phase in progress["phases"]:
            counted = phase["done"] + phase["running"] + phase["waiting"] \
                + phase["failed"] + phase["pending"]
            self.assertEqual(counted, progress["total"], phase["key"])

    def test_the_phases_come_back_in_the_order_they_happen(self):
        progress = progress_of(state(combo()))
        self.assertEqual([phase["key"] for phase in progress["phases"]], PHASE_KEYS)

    def test_the_two_that_run_together_are_marked_as_such(self):
        progress = progress_of(state(combo()))
        parallel = [phase["key"] for phase in progress["phases"] if phase["parallel"]]
        self.assertEqual(parallel, ["layout", "geometry"])

    def test_a_run_with_altium_off_does_not_show_a_layout_phase(self):
        progress = progress_of(state(combo(step_status="done")), {"use_altium": False})
        self.assertNotIn("layout", [phase["key"] for phase in progress["phases"]])


if __name__ == "__main__":
    unittest.main()
