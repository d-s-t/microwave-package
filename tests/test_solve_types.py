"""Tests for which analysis settings belong to which solution type.

The point of solve_types is that the form asks for exactly the settings
that apply and the server reads back exactly those -- so what is tested
here is mostly the separation itself, not any one number.

Run: python -m unittest discover -s tests
"""
import unittest

from eigen_mode_analyze import pipeline, solve_types


class TestTheSplit(unittest.TestCase):
    def test_the_two_types_share_no_specific_fields(self):
        """The form renders a panel per solution type and hides the one
        that does not apply. A name appearing in both panels would submit
        twice, and the value read back would be whichever panel came
        first in the document rather than the one on screen."""
        eigen = {f["name"] for f in solve_types.specific_fields(solve_types.EIGENMODE)}
        driven = {f["name"] for f in solve_types.specific_fields(solve_types.DRIVEN_MODAL)}
        self.assertEqual(eigen & driven, set())

    def test_shared_fields_are_in_neither_panel(self):
        shared = {f["name"] for f in solve_types.SHARED}
        for solution_type in (solve_types.EIGENMODE, solve_types.DRIVEN_MODAL):
            specific = {f["name"] for f in solve_types.specific_fields(solution_type)}
            self.assertEqual(shared & specific, set(), solution_type)

    def test_fields_for_is_the_specific_ones_plus_the_shared_ones(self):
        for solution_type in (solve_types.EIGENMODE, solve_types.DRIVEN_MODAL):
            names = [f["name"] for f in solve_types.fields_for(solution_type)]
            expected = (
                [f["name"] for f in solve_types.specific_fields(solution_type)]
                + [f["name"] for f in solve_types.SHARED]
            )
            self.assertEqual(names, expected)

    def test_every_field_has_what_the_form_needs_to_render_it(self):
        for field in solve_types.EIGENMODE_ONLY + solve_types.DRIVEN_ONLY + solve_types.SHARED:
            self.assertIn(field["kind"], ("int", "float", "select"), field["name"])
            self.assertTrue(field["label"], field["name"])
            self.assertIn("default", field, field["name"])
            if field["kind"] == "select":
                allowed = [o["value"] for o in field["options"]]
                self.assertIn(field["default"], allowed, field["name"])


class TestDefaults(unittest.TestCase):
    def test_a_runs_settings_carry_both_halves(self):
        """The unused half is kept rather than stripped: the web form
        copies settings forward from the last run, and switching solution
        type has to land on real numbers rather than on missing keys."""
        defaults = solve_types.all_defaults()
        for solution_type in (solve_types.EIGENMODE, solve_types.DRIVEN_MODAL):
            for field in solve_types.fields_for(solution_type):
                self.assertIn(field["name"], defaults)

    def test_the_pipeline_defaults_include_every_solve_setting(self):
        for name in solve_types.all_defaults():
            self.assertIn(name, pipeline.DEFAULT_ANALYSIS_SETTINGS)

    def test_a_new_run_is_an_eigenmode_run(self):
        """Eigenmode was the only kind of run this pipeline had, so it
        stays the default -- nobody gets a driven run by not choosing."""
        self.assertEqual(
            pipeline.DEFAULT_ANALYSIS_SETTINGS["solution_type"], solve_types.EIGENMODE
        )


class TestIsDriven(unittest.TestCase):
    def test_eigenmode_is_not_driven(self):
        self.assertFalse(solve_types.is_driven(solve_types.EIGENMODE))

    def test_driven_modal_is(self):
        self.assertTrue(solve_types.is_driven(solve_types.DRIVEN_MODAL))

    def test_a_missing_solution_type_is_eigenmode(self):
        """Runs created before driven runs existed have no solution_type
        at all, and they were eigenmode runs."""
        self.assertFalse(solve_types.is_driven(None))
        self.assertEqual(solve_types.solution_type_of({}), solve_types.EIGENMODE)


class TestCoerce(unittest.TestCase):
    def _field(self, name):
        for field in solve_types.EIGENMODE_ONLY + solve_types.DRIVEN_ONLY + solve_types.SHARED:
            if field["name"] == name:
                return field
        raise AssertionError(name)

    def test_numbers_come_back_as_their_declared_type(self):
        self.assertEqual(solve_types.coerce(self._field("sweep_points"), "801"), 801)
        self.assertEqual(solve_types.coerce(self._field("f_stop_ghz"), "12.5"), 12.5)
        self.assertIsInstance(solve_types.coerce(self._field("max_passes"), "30"), int)

    def test_a_select_only_accepts_its_own_options(self):
        field = self._field("reference_plane")
        self.assertEqual(solve_types.coerce(field, "launch"), "launch")
        with self.assertRaises(ValueError):
            solve_types.coerce(field, "somewhere_else")


class TestValidate(unittest.TestCase):
    def test_eigenmode_does_not_care_how_many_ports_there_are(self):
        self.assertIsNone(solve_types.validate(solve_types.EIGENMODE, [0]))
        self.assertIsNone(solve_types.validate(solve_types.EIGENMODE, [7]))

    def test_a_driven_run_needs_exactly_two(self):
        self.assertIsNone(solve_types.validate(solve_types.DRIVEN_MODAL, [2]))
        self.assertIn("2 ports", solve_types.validate(solve_types.DRIVEN_MODAL, [1]))

    def test_a_swept_port_count_is_checked_in_full(self):
        """Only some combinations having two ports is still a run that
        cannot finish, so every value the sweep would build is checked --
        not just the first."""
        complaint = solve_types.validate(solve_types.DRIVEN_MODAL, [2, 1, 4])
        self.assertIsNotNone(complaint)
        self.assertIn("1, 4", complaint)


if __name__ == "__main__":
    unittest.main()
