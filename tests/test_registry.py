"""Tests for the solver registry and what each solver declares.

The registry is what lets the pipeline carry two solution types without
branching on them, so what is tested here is mostly the contract: every
registered solver fills it in, and nothing upstream has to know which one
it is holding.

Run: python -m unittest discover -s tests
"""
import unittest

from microwave_package import pipeline
from microwave_package.hfss import registry
from microwave_package.hfss.driven import DRIVEN_MODAL
from microwave_package.hfss.eigenmode import EIGENMODE


def solvers():
    return [registry.get(t["value"]) for t in registry.solution_types()]


class TestWhatIsRegistered(unittest.TestCase):
    def test_every_solver_is_loaded_by_importing_the_package(self):
        """The registry is empty until the solvers import themselves into
        it, which importing microwave_package.hfss is what does."""
        self.assertEqual(
            [t["value"] for t in registry.solution_types()],
            [EIGENMODE, DRIVEN_MODAL, "LineImpedance"],
        )

    def test_eigenmode_is_the_default(self):
        """It was the only kind of run this pipeline had, so a run that
        does not say which it wants is still one. Registration order is
        what decides this."""
        self.assertEqual(registry.default_solution_type(), EIGENMODE)

    def test_registering_the_same_solution_type_twice_is_refused(self):
        """Two solvers claiming one name means the second silently
        replaces the first, or does not -- either way something is
        already wrong."""
        with self.assertRaises(ValueError):
            registry.register(registry.get(EIGENMODE))


class TestTheContract(unittest.TestCase):
    def test_every_solver_fills_the_contract_in(self):
        for solver in solvers():
            self.assertTrue(callable(solver.create_setup), solver.value)
            self.assertTrue(callable(solver.read_results), solver.value)
            self.assertTrue(callable(solver.place_ports), solver.value)
            self.assertTrue(callable(solver.post_processing_ops), solver.value)
            self.assertTrue(callable(solver.default_axes), solver.value)
            self.assertTrue(callable(solver.validate), solver.value)

    def test_every_field_has_what_the_form_needs_to_render_it(self):
        for solver in solvers():
            for field in solver.all_fields:
                self.assertIn(field["kind"], ("int", "float", "select", "text"), field["name"])
                self.assertTrue(field["label"], field["name"])
                self.assertIn("default", field, field["name"])
                if field["kind"] == "select":
                    allowed = [o["value"] for o in field["options"]]
                    self.assertIn(field["default"], allowed, field["name"])

    def test_no_two_solvers_claim_the_same_setting(self):
        """The form renders a panel per solver and hides the one that does
        not apply. A name appearing in both panels would submit twice, and
        the value read back would be whichever panel came first in the
        document rather than the one on screen."""
        seen = {}
        for solver in solvers():
            for field in solver.fields:
                self.assertNotIn(field["name"], seen)
                seen[field["name"]] = solver.value

    def test_shared_settings_are_in_no_solvers_own_fields(self):
        shared = {f["name"] for f in registry.SHARED}
        for solver in solvers():
            own = {f["name"] for f in solver.fields}
            self.assertEqual(shared & own, set(), solver.value)

    def test_all_fields_is_a_solvers_own_plus_the_shared_ones(self):
        for solver in solvers():
            names = [f["name"] for f in solver.all_fields]
            expected = [f["name"] for f in solver.fields] + [f["name"] for f in registry.SHARED]
            self.assertEqual(names, expected, solver.value)

    def test_result_columns_two_solvers_share_are_labelled_alike(self):
        """They are merged into one axis picker, so a clash would mean one
        solver's label silently describing the other's numbers. freq_ghz
        is legitimately shared -- a mode and a sweep point both have a
        frequency, and it means the same thing on an axis."""
        seen = {}
        for solver in solvers():
            for column, label in solver.result_fields.items():
                if column in seen:
                    self.assertEqual(label, seen[column], column)
                seen[column] = label

    def test_task_id_prefixes_are_distinct(self):
        """They name the queue folder a combination is copied into, which
        is the only place the kind of run still shows."""
        prefixes = [s.task_id_prefix for s in solvers()]
        self.assertEqual(len(prefixes), len(set(prefixes)))


class TestLookup(unittest.TestCase):
    def test_a_missing_solution_type_is_the_default(self):
        """Runs created before runs had a solution type were eigenmode
        runs, and still read back as ones."""
        self.assertEqual(registry.get(None).value, EIGENMODE)
        self.assertEqual(registry.solution_type_of({}), EIGENMODE)

    def test_a_run_is_looked_up_by_its_settings(self):
        self.assertEqual(
            registry.for_settings({"solution_type": DRIVEN_MODAL}).value, DRIVEN_MODAL
        )

    def test_is_known_says_whether_a_form_value_is_real(self):
        self.assertTrue(registry.is_known(DRIVEN_MODAL))
        self.assertFalse(registry.is_known("Bogus"))


class TestDefaults(unittest.TestCase):
    def test_a_runs_settings_carry_every_solvers_defaults(self):
        """The unused ones are kept rather than stripped: the web form
        copies settings forward from the last run, and switching solution
        type has to land on real numbers rather than on missing keys."""
        defaults = registry.all_defaults()
        for solver in solvers():
            for field in solver.all_fields:
                self.assertIn(field["name"], defaults)

    def test_the_pipeline_defaults_include_every_solve_setting(self):
        for name in registry.all_defaults():
            self.assertIn(name, pipeline.DEFAULT_ANALYSIS_SETTINGS)

    def test_a_new_run_is_an_eigenmode_run(self):
        self.assertEqual(pipeline.DEFAULT_ANALYSIS_SETTINGS["solution_type"], EIGENMODE)


class TestCoerce(unittest.TestCase):
    def _field(self, name):
        for solver in solvers():
            for field in solver.all_fields:
                if field["name"] == name:
                    return field
        raise AssertionError(name)

    def test_numbers_come_back_as_their_declared_type(self):
        self.assertEqual(registry.coerce(self._field("sweep_points"), "801"), 801)
        self.assertEqual(registry.coerce(self._field("f_stop_ghz"), "12.5"), 12.5)
        self.assertIsInstance(registry.coerce(self._field("max_passes"), "30"), int)

    def test_a_select_only_accepts_its_own_options(self):
        field = self._field("reference_plane")
        self.assertEqual(registry.coerce(field, "launch"), "launch")
        with self.assertRaises(ValueError):
            registry.coerce(field, "somewhere_else")


class TestValidate(unittest.TestCase):
    def test_eigenmode_does_not_care_how_many_ports_there_are(self):
        self.assertIsNone(registry.get(EIGENMODE).validate([0]))
        self.assertIsNone(registry.get(EIGENMODE).validate([7]))

    def test_a_driven_run_needs_exactly_two(self):
        driven = registry.get(DRIVEN_MODAL)
        self.assertIsNone(driven.validate([2]))
        self.assertIn("2 ports", driven.validate([1]))

    def test_a_swept_port_count_is_checked_in_full(self):
        """Only some combinations having two ports is still a run that
        cannot finish, so every value the sweep would build is checked --
        not just the first."""
        complaint = registry.get(DRIVEN_MODAL).validate([2, 1, 4])
        self.assertIsNotNone(complaint)
        self.assertIn("1, 4", complaint)


class TestPortsAndOpsAreSolverConcerns(unittest.TestCase):
    def test_eigenmode_builds_no_ports(self):
        """It is source-free, so it leaves place_ports at the registry's
        default rather than implementing an empty one."""
        self.assertEqual(registry.get(EIGENMODE).place_ports(None, None, {}, None), [])

    def test_eigenmode_asks_for_field_ops_and_driven_does_not(self):
        """Field statistics and plots are per-mode quantities, and a
        driven run has no modes to run them against."""
        ops = [o["op"] for o in registry.get(EIGENMODE).post_processing_ops({})]
        self.assertIn("field_statistics", ops)
        self.assertIn("field_plot_surface", ops)
        self.assertEqual(registry.get(DRIVEN_MODAL).post_processing_ops({}), [])


if __name__ == "__main__":
    unittest.main()
