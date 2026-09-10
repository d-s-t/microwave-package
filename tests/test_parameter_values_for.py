"""Tests for resolving what a parameter is actually worth in a run.

A run's combinations are layered -- the equations file underneath, the
run's pinned values over it, the swept values on top -- and anything
checked before the build has to use the same layering or it is answering
about a different geometry than the one that will be built.

Run: python -m unittest discover -s tests
"""
import unittest

from microwave_package.design.sweep import SweepParameter
from microwave_package.hfss import registry
from microwave_package.workflow import run_manager


class TestParameterValuesFor(unittest.TestCase):
    def test_a_parameter_nobody_typed_takes_the_equation_files_value(self):
        """The bug this exists for: an untouched port_count is not absent
        from the run, it is 2 -- and reporting it as absent produced a
        complaint about a geometry with 0 ports."""
        self.assertEqual(run_manager.parameter_values_for("port_count", [], {}), [2.0])

    def test_a_pinned_value_wins_over_the_file(self):
        self.assertEqual(
            run_manager.parameter_values_for("port_count", [], {"port_count": 1.0}), [1.0]
        )

    def test_a_swept_parameter_gives_every_value_it_sweeps(self):
        swept = [SweepParameter(name="port_count", values=[1, 2, 4])]
        self.assertEqual(run_manager.parameter_values_for("port_count", swept, {}), [1, 2, 4])

    def test_a_sweep_wins_over_a_pin(self):
        swept = [SweepParameter(name="port_count", values=[2])]
        self.assertEqual(
            run_manager.parameter_values_for("port_count", swept, {"port_count": 1.0}), [2]
        )

    def test_other_parameters_being_swept_changes_nothing(self):
        swept = [SweepParameter(name="pedestal_depth", values=[0, 2])]
        self.assertEqual(
            run_manager.parameter_values_for("port_count", swept, {"wall_width": 1.0}), [2.0]
        )

    def test_a_parameter_that_exists_nowhere_gives_nothing_to_check(self):
        """Rather than a made-up value: the build will say so with a
        better message than a guess here could."""
        self.assertEqual(run_manager.parameter_values_for("not_a_parameter", [], {}), [])


class TestWhatTheDrivenCheckMakesOfThat(unittest.TestCase):
    def setUp(self):
        self.driven = registry.get("DrivenModal")

    def test_a_default_geometry_passes(self):
        """The whole point: starting a driven run without touching
        port_count must not be refused."""
        self.assertIsNone(
            self.driven.validate(run_manager.parameter_values_for("port_count", [], {}))
        )

    def test_a_genuinely_wrong_port_count_is_still_refused(self):
        complaint = self.driven.validate(
            run_manager.parameter_values_for("port_count", [], {"port_count": 1.0})
        )
        self.assertIsNotNone(complaint)
        self.assertIn("port_count = 1", complaint)

    def test_nothing_to_check_against_is_not_treated_as_a_failure(self):
        self.assertIsNone(self.driven.validate([]))


if __name__ == "__main__":
    unittest.main()
