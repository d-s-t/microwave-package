"""Tests for how the per-mode field work copes with a mesh the student
licence will not read.

A big board is solved fine on the worker and then opened here on the
student licence, where AEDT refuses every field export because the solved
mesh is over the licence's element limit. PyAEDT is left half reset by the
first failure, so every later export died with an unrelated-looking
"no attribute 'default_solution'", and each mode's row carried a pile of
them. What is pinned here: the limit is recognised, said once in words that
name the cause, and not retried for every op of every mode.

Nothing opens AEDT: a stand-in answers the one question asked of it.

Run: python -m unittest discover -s tests
"""
import unittest
from unittest import mock

from microwave_package.hfss.eigenmode import fields


LIMIT = ("Project: p, Design: d (Eigenmode), [error]  Setup_1: Mesh size (1038598 volume "
         "elements) is greater than the limit for simulation in Ansys Electronics Desktop "
         "Student. (02:27:58 PM  Oct 09, 2026)")
OTHER = "Project: p, Design: d (Eigenmode), [error]  Script macro error: Abnormal script termination."


class FakeDesktop:
    def __init__(self, messages):
        self.messages = messages
        self.asked = 0

    def GetMessages(self, project, design, severity):
        self.asked += 1
        return self.messages


class FakeHfss:
    project_name = "p"
    design_name = "d"

    def __init__(self, messages=()):
        self.odesktop = FakeDesktop(list(messages))


def failing(calls):
    def op(hfss, task, mode_context, params, out_dir):
        calls.append(params.get("key"))
        raise RuntimeError("Failed to execute gRPC AEDT command: CalculatorWrite")
    return op


def working(hfss, task, mode_context, params, out_dir):
    return {"chip_mag_E_max": 1.0}


TASK = {"post_processing": [
    {"op": "boom", "params": {"key": "a"}},
    {"op": "boom", "params": {"key": "b"}},
]}


class TestTheMeshLimit(unittest.TestCase):
    def test_it_is_recognised_and_the_size_is_named(self):
        reason = fields.mesh_limit_reason(FakeHfss([OTHER, LIMIT]))
        self.assertIn("1038598 volume elements", reason)
        self.assertIn("student licence", reason)
        self.assertIn("frequencies and Q are unaffected", reason)

    def test_another_error_is_not_mistaken_for_it(self):
        self.assertIsNone(fields.mesh_limit_reason(FakeHfss([OTHER])))
        self.assertIsNone(fields.mesh_limit_reason(FakeHfss([])))

    def test_a_desktop_that_cannot_be_asked_is_not_an_answer(self):
        hfss = FakeHfss()
        hfss.odesktop = None
        self.assertIsNone(fields.mesh_limit_reason(hfss))


class TestRunningTheOps(unittest.TestCase):
    def run_ops(self, hfss, calls):
        with mock.patch.dict(fields.POST_PROCESSING_OPS, {"boom": failing(calls)}):
            return fields.run_post_processing(hfss, TASK, {"setup_name": "S", "mode": 1}, ".")

    def test_over_the_limit_the_row_says_why_instead_of_listing_errors(self):
        calls = []
        row = self.run_ops(FakeHfss([LIMIT]), calls)
        self.assertTrue(row["skipped"])
        self.assertIn("student licence", row["reason"])
        self.assertNotIn("post_processing_errors", row)

    def test_the_remaining_ops_of_that_mode_are_not_tried(self):
        calls = []
        self.run_ops(FakeHfss([LIMIT]), calls)
        self.assertEqual(calls, ["a"])

    def test_the_next_modes_are_not_tried_at_all(self):
        calls = []
        hfss = FakeHfss([LIMIT])
        self.run_ops(hfss, calls)
        row = self.run_ops(hfss, calls)
        self.assertEqual(calls, ["a"])
        self.assertTrue(row["skipped"])

    def test_any_other_failure_is_reported_as_before(self):
        calls = []
        row = self.run_ops(FakeHfss([OTHER]), calls)
        self.assertEqual(calls, ["a", "b"])
        self.assertEqual(len(row["post_processing_errors"]), 2)
        self.assertNotIn("skipped", row)

    def test_a_project_under_the_limit_is_untouched(self):
        with mock.patch.dict(fields.POST_PROCESSING_OPS, {"boom": working}):
            row = fields.run_post_processing(FakeHfss(), TASK, {"setup_name": "S", "mode": 1}, ".")
        self.assertEqual(row, {"chip_mag_E_max": 1.0})


if __name__ == "__main__":
    unittest.main()