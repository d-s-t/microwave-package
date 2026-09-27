"""Tests for reading the solution type and its settings off the form.

The form renders both solution types' controls and hides the one that
does not apply, which means every submission carries values for settings
the run will never use. What is tested here is that they stay unused.

Run: python -m unittest discover -s tests
"""
import logging
import unittest

from werkzeug.datastructures import MultiDict

from microwave_package import webapp
from microwave_package.hfss import registry
from microwave_package.hfss.driven import ports


def form(**fields):
    return MultiDict(list(fields.items()))


class TestReadingTheChosenType(unittest.TestCase):
    def setUp(self):
        # These paths log a warning for every value they reject, which is
        # the point of them -- but not something the test output needs.
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_a_driven_submission_reads_the_driven_fields(self):
        settings = webapp._solve_settings_from_form(form(
            solution_type="DrivenModal", f_start_ghz="2", f_stop_ghz="12",
            sweep_points="801", max_delta_s="0.01", port_impedance_ohm="75",
            reference_plane="launch", max_passes="30",
        ))
        self.assertEqual(settings["solution_type"], "DrivenModal")
        self.assertEqual(settings["f_start_ghz"], 2.0)
        self.assertEqual(settings["f_stop_ghz"], 12.0)
        self.assertEqual(settings["sweep_points"], 801)
        self.assertEqual(settings["port_impedance_ohm"], 75.0)
        self.assertEqual(settings["reference_plane"], "launch")
        self.assertEqual(settings["max_passes"], 30)

    def test_the_hidden_panels_values_do_not_reach_the_run(self):
        """The eigenmode controls are still in the document and still
        submit. A driven run must not pick anything up from them."""
        settings = webapp._solve_settings_from_form(form(
            solution_type="DrivenModal", modes="7", max_delta_f="9.5",
            min_freq_safety_factor="0.9",
        ))
        defaults = registry.all_defaults()
        self.assertEqual(settings["modes"], defaults["modes"])
        self.assertEqual(settings["max_delta_f"], defaults["max_delta_f"])
        self.assertEqual(settings["min_freq_safety_factor"], defaults["min_freq_safety_factor"])

    def test_the_same_holds_the_other_way_round(self):
        settings = webapp._solve_settings_from_form(form(
            solution_type="Eigenmode", modes="7", f_stop_ghz="999", reference_plane="launch",
        ))
        defaults = registry.all_defaults()
        self.assertEqual(settings["modes"], 7)
        self.assertEqual(settings["f_stop_ghz"], defaults["f_stop_ghz"])
        self.assertEqual(settings["reference_plane"], defaults["reference_plane"])

    def test_both_halves_are_always_present(self):
        """Switching an existing run's solution type has to land on real
        numbers, not on missing keys."""
        settings = webapp._solve_settings_from_form(form(solution_type="Eigenmode"))
        for name in registry.all_defaults():
            self.assertIn(name, settings)


class TestBadInput(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_an_unparseable_number_falls_back_to_its_default(self):
        """A run is recoverable from a wrong number and not from a form
        that will not submit."""
        settings = webapp._solve_settings_from_form(form(
            solution_type="DrivenModal", sweep_points="not a number",
        ))
        self.assertEqual(settings["sweep_points"], registry.all_defaults()["sweep_points"])

    def test_a_select_outside_its_options_falls_back_too(self):
        settings = webapp._solve_settings_from_form(form(
            solution_type="DrivenModal", reference_plane="somewhere_else",
        ))
        self.assertEqual(settings["reference_plane"], ports.DEFAULT_REFERENCE_PLANE)

    def test_an_empty_box_leaves_the_default(self):
        settings = webapp._solve_settings_from_form(form(
            solution_type="DrivenModal", f_stop_ghz="",
        ))
        self.assertEqual(settings["f_stop_ghz"], registry.all_defaults()["f_stop_ghz"])

    def test_an_unknown_solution_type_falls_back_to_the_default(self):
        settings = webapp._solve_settings_from_form(form(solution_type="Bogus"))
        self.assertEqual(settings["solution_type"], registry.default_solution_type())

    def test_a_submission_with_no_solution_type_is_an_eigenmode_run(self):
        self.assertEqual(
            webapp._solve_settings_from_form(form())["solution_type"], "Eigenmode"
        )


class TestTheFormItself(unittest.TestCase):
    def setUp(self):
        self.client = webapp.app.test_client()

    def test_both_panels_render_and_only_one_is_shown(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-solution-type="Eigenmode"', html)
        self.assertIn('data-solution-type="DrivenModal"', html)
        # The default is an eigenmode run, so the driven panel is the
        # hidden one.
        self.assertRegex(html, r'data-solution-type="DrivenModal"\s+hidden')

    def test_no_setting_name_appears_twice(self):
        """A duplicated name would submit twice, and the value read back
        would be whichever came first in the document."""
        import collections
        import re

        html = self.client.get("/").get_data(as_text=True)
        # The run list's compare tick boxes share one name on purpose --
        # a checkbox group, submitted by a form of their own -- so they
        # are not this form's settings and are not counted.
        tags = [tag for tag in re.findall(r"<(?:input|select)[^>]*>", html)
                if 'form="compare-form"' not in tag]
        names = [m.group(1) for m in (re.search(r'name="([a-z_0-9]+)"', tag) for tag in tags) if m]
        counts = collections.Counter(names)
        # Radio groups legitimately share a name; nothing else may.
        radios = {"solution_type", "distortion_mode"}
        repeated = {n for n, k in counts.items() if k > 1} - radios
        self.assertEqual(repeated, set())


if __name__ == "__main__":
    unittest.main()
