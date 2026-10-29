"""Tests for whether HFSS converged, per combination and per run (workflow/solver_status.py).

The profile texts are written the way HFSS writes them: a footnote carries its
message inside escaped quotes, and each basis element is a ProfileItem line whose
S-matrix error may be followed by a note. Every folder is made in a temporary
folder, and the module's caches are cleared before and after each test, so no
test reads another's files.

Run: python -m unittest discover -s tests
"""
import csv
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from microwave_package.workflow import solver_status as solver


def footnote(message, level=0):
    """A ProfileFootnote line, with its message in escaped quotes."""
    return "\t\t\tProfileFootnote('I(1, 0, \\'%s\\')', %d)" % (message, level)


def status(state, level=0):
    """The Stop Time line that ends a solve, with the status it ended with."""
    return ("\t\tProfileFootnote('I(2, 1, \\'Stop Time\\', \\'10/26/2026 16:19:17\\', 1, \\'Status\\', "
            "\\'%s\\')', %d)" % (state, level))


def basis(number, frequency, error=None, note=""):
    """A basis element's ProfileItem line: its S-matrix error, if it has one, and a note."""
    text = f"Basis Element # {number}, Frequency: {frequency}GHz"
    if error is not None:
        text += f"; S Matrix Error = {error:>8.3f}%"
    if note:
        text += f"; {note}"
    return "\t\t\t\tProfileItem('', 0, 0, 0, 0, 0, 'I(1, 0, \\'%s\\')', false, true)" % text


def profile(*lines):
    return "\n".join(lines) + "\n"


OK_PROFILE = profile(
    footnote("Adaptive Passes converged"),
    basis(1, "2"),
    basis(2, "10.4375", 34.071),
    basis(3, "18", 0.072, note="Scattering matrix quantities converged; Passive within tolerance"),
    footnote("Interpolating sweep converged and is passive"),
    footnote("HFSS: Distributed Interpolating sweep"),
    status("Normal Completion"),
)

WARNING_PROFILE = profile(
    footnote("Adaptive Passes did not converge", level=1),
    basis(1, "4", 0.403, note="Secondary solver criterion is not converged"),
    basis(2, "9", 0.18, note="Secondary solver criterion is not converged"),
    footnote("Interpolating sweep converged and is passive"),
    footnote("HFSS: Distributed Interpolating sweep"),
    status("Normal Completion"),
)

# The profile of a run whose sweep failed: the basis elements and the error of
# that sweep are the ones the page reports.
FAILED_PROFILE = profile(
    footnote("Adaptive Passes converged"),
    basis(3, "11", 91.501),
    basis(17, "10.4375", 100.918),
    footnote("Interpolating sweep did NOT converge"),
    footnote("HFSS: Distributed Interpolating sweep"),
    footnote("Sweep Simulation FAILED"),
    status("Engine Detected Error", level=2),
)

# Three sweeps, as a run with fine windows writes them: basis numbering starts
# again in each one. The second did not converge, so it is the one reported.
THREE_SWEEPS_PROFILE = profile(
    footnote("Adaptive Passes converged"),
    basis(1, "5", 3.1),
    basis(2, "6", 2.0),
    footnote("Interpolating sweep converged and is passive"),
    footnote("HFSS: Distributed Interpolating sweep"),
    basis(1, "12", 60.5),
    basis(2, "13", 0.9),
    basis(3, "14", 7.7),
    footnote("Interpolating sweep did NOT converge"),
    footnote("HFSS: Distributed Interpolating sweep"),
    basis(1, "16", 0.2),
    footnote("Interpolating sweep converged and is passive"),
    footnote("HFSS: Distributed Interpolating sweep"),
    status("Normal Completion"),
)

ABORTED_PROFILE = profile(
    footnote("Adaptive Passes converged"),
    status("Aborted", level=1),
)

NON_PASSIVE_PROFILE = profile(
    footnote("Adaptive Passes converged"),
    basis(1, "3", 0.5),
    footnote("Interpolating sweep converged but has at least one non-passive basis solution"),
    footnote("HFSS: Distributed Interpolating sweep"),
    status("Normal Completion"),
)

CONSECUTIVE_PROFILE = profile(
    footnote("The last 1 adaptive passes converged, but 2 consecutive converged passes were requested.", level=1),
    footnote("Interpolating sweep converged and is passive"),
    status("Normal Completion"),
)

STILL_GOING_PROFILE = profile(
    footnote("Adaptive Passes converged"),
    footnote("Interpolating sweep converged and is passive"),
)


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def profile_path(combo_dir, name="DV1_S1_V0.profile"):
    """Where HFSS puts a profile: beside the project, in the design's results folder."""
    return os.path.join(combo_dir, "run__default.aedtresults", "HFSS_ABC.results", name)


def write_results(combo_dir, rows, columns=("freq_ghz", "s11_db", "s21_db", "s12_db", "s22_db")):
    os.makedirs(combo_dir, exist_ok=True)
    with open(os.path.join(combo_dir, "results.csv"), "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        for index, row in enumerate(rows):
            writer.writerow({"freq_ghz": 2 + index, **row})


def make_run(root, run_id, combos):
    """A run folder with each combination's profiles and results. `combos` maps a
    combination's name to {"profiles": {name: text}, "results": [rows] or None}."""
    run_dir = os.path.join(root, run_id)
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "state.json"), "w", encoding="utf-8") as handle:
        json.dump({"stage": "complete", "combos": {name: {} for name in combos}}, handle)
    for name, parts in combos.items():
        combo_dir = os.path.join(run_dir, "models", name)
        os.makedirs(combo_dir, exist_ok=True)
        for profile_name, text in (parts.get("profiles") or {}).items():
            write_text(profile_path(combo_dir, profile_name), text)
        if parts.get("results") is not None:
            write_results(combo_dir, parts["results"])
    return run_dir


GOOD_ROWS = [{"s11_db": -10.0, "s21_db": -1.0, "s12_db": -1.0, "s22_db": -10.0},
             {"s11_db": -10.0, "s21_db": -3.0, "s12_db": -3.0, "s22_db": -10.0}]
BAD_ROWS = [{"s11_db": 0.5, "s21_db": -1.0, "s12_db": -1.0, "s22_db": -10.0},
            {"s11_db": -0.5, "s21_db": -0.5, "s12_db": -0.5, "s22_db": -0.5},
            {"s11_db": -20.0, "s21_db": -0.1, "s12_db": -0.1, "s22_db": -20.0}]


class TheCaches(unittest.TestCase):
    def setUp(self):
        solver.clear_caches()
        self.addCleanup(solver.clear_caches)
        self.root = tempfile.mkdtemp(prefix="solver_")
        self.addCleanup(shutil.rmtree, self.root, True)

    def combo(self, text=OK_PROFILE):
        combo_dir = os.path.join(self.root, "run_x", "models", "default")
        write_text(profile_path(combo_dir), text)
        return combo_dir


class ReadingAProfile(unittest.TestCase):
    def test_a_converged_profile_is_ok_and_reports_its_last_basis_element(self):
        reading = solver.read_profile(OK_PROFILE)
        self.assertEqual(reading.verdict, "ok")
        self.assertIn(solver.ADAPTIVE_CONVERGED, reading.messages)
        self.assertIn(solver.SWEEP_PASSIVE, reading.messages)
        self.assertEqual(reading.basis_elements, 3)
        self.assertAlmostEqual(reading.last_s_matrix_error, 0.072)
        self.assertEqual(reading.sweeps, 1)

    def test_the_adaptive_and_secondary_notes_make_a_warning(self):
        reading = solver.read_profile(WARNING_PROFILE)
        self.assertEqual(reading.verdict, "warning")
        self.assertIn(solver.ADAPTIVE_NOT_CONVERGED, reading.messages)
        self.assertIn(solver.SECONDARY_NOT_CONVERGED, reading.messages)
        self.assertNotIn(solver.DID_NOT_CONVERGE, reading.messages)

    def test_a_sweep_that_did_not_converge_is_failed_and_reports_its_last_error(self):
        reading = solver.read_profile(FAILED_PROFILE)
        self.assertEqual(reading.verdict, "failed")
        for message in (solver.DID_NOT_CONVERGE, solver.SIMULATION_FAILED, solver.ENGINE_ERROR):
            self.assertIn(message, reading.messages)
        self.assertEqual(reading.basis_elements, 2)
        self.assertAlmostEqual(reading.last_s_matrix_error, 100.918)

    def test_with_several_sweeps_the_one_that_did_not_converge_is_reported(self):
        reading = solver.read_profile(THREE_SWEEPS_PROFILE)
        self.assertEqual(reading.verdict, "failed")
        self.assertEqual(reading.sweeps, 3)
        self.assertEqual(reading.basis_elements, 3)
        self.assertAlmostEqual(reading.last_s_matrix_error, 7.7)

    def test_an_aborted_solve_is_failed(self):
        reading = solver.read_profile(ABORTED_PROFILE)
        self.assertEqual(reading.verdict, "failed")
        self.assertEqual(reading.messages, (solver.ADAPTIVE_CONVERGED, solver.DID_NOT_FINISH))

    def test_a_converged_sweep_with_a_basis_solution_that_is_not_passive_is_a_warning(self):
        reading = solver.read_profile(NON_PASSIVE_PROFILE)
        self.assertEqual(reading.verdict, "warning")
        self.assertIn(solver.NOT_PASSIVE, reading.messages)

    def test_the_consecutive_passes_note_is_a_warning_and_not_read_as_converged(self):
        reading = solver.read_profile(CONSECUTIVE_PROFILE)
        self.assertEqual(reading.verdict, "warning")
        self.assertIn(solver.ADAPTIVE_NOT_CONSECUTIVE, reading.messages)
        self.assertNotIn(solver.ADAPTIVE_CONVERGED, reading.messages)

    def test_a_solve_with_no_status_line_yet_is_unknown(self):
        self.assertEqual(solver.read_profile(STILL_GOING_PROFILE).verdict, "unknown")
        self.assertEqual(solver.read_profile("").verdict, "unknown")


class ReadingAResultsTable(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="solver_csv_")
        self.addCleanup(shutil.rmtree, self.root, True)

    def table(self, rows, columns=("freq_ghz", "s11_db", "s21_db", "s12_db", "s22_db")):
        write_results(self.root, rows, columns)
        return os.path.join(self.root, "results.csv")

    def test_points_above_0_db_and_with_more_power_out_than_in_are_counted(self):
        counts = solver.read_results(self.table([
            {"s11_db": -10.0, "s21_db": -1.0, "s12_db": -1.0, "s22_db": -10.0},   # fine
            {"s11_db": 0.5, "s21_db": -1.0, "s12_db": -1.0, "s22_db": -10.0},     # above 0 dB, and power 1.9
            {"s11_db": -0.5, "s21_db": -0.5, "s12_db": -0.5, "s22_db": -0.5},     # power 1.78, not above 0 dB
        ]))
        self.assertEqual(counts["points"], 3)
        self.assertEqual(counts["above_0db"], 1)
        self.assertEqual(counts["more_power_out"], 2)
        self.assertEqual(counts["bad"], 2)

    def test_a_table_with_only_port_one_counts_only_what_it_has(self):
        counts = solver.read_results(self.table(
            [{"s11_db": 0.2}, {"s11_db": -3.0}], columns=("freq_ghz", "s11_db")))
        self.assertEqual(counts, {"points": 2, "above_0db": 1, "more_power_out": None, "bad": 1})

    def test_a_point_that_is_not_a_number_is_left_out(self):
        counts = solver.read_results(self.table([{"s11_db": "nan", "s21_db": -1.0}], columns=("freq_ghz", "s11_db", "s21_db")))
        self.assertEqual(counts["points"], 1)
        self.assertEqual(counts["bad"], 0)

    def test_a_table_that_cannot_be_read_is_none(self):
        self.assertIsNone(solver.read_results(os.path.join(self.root, "missing.csv")))


class ACombination(TheCaches):
    def test_no_profile_and_no_results_is_unknown(self):
        status = solver.combo_status(os.path.join(self.root, "nothing_here"))
        self.assertEqual(status["verdict"], "unknown")
        self.assertEqual(status["profiles"], 0)

    def test_the_worst_of_its_profiles_is_its_verdict(self):
        combo_dir = os.path.join(self.root, "run_x", "models", "default")
        write_text(profile_path(combo_dir, "DV1_S1_V0.profile"), OK_PROFILE)
        write_text(profile_path(combo_dir, "DV2_S2_V0.profile"), WARNING_PROFILE)
        write_text(profile_path(combo_dir, "DV3_S3_V0.profile"), FAILED_PROFILE)
        status = solver.combo_status(combo_dir)
        self.assertEqual(status["verdict"], "failed")
        self.assertEqual(status["profiles"], 3)
        self.assertAlmostEqual(status["last_s_matrix_error"], 100.918)

    def test_points_above_0_db_make_a_converged_sweep_failed(self):
        combo_dir = os.path.join(self.root, "run_x", "models", "default")
        write_text(profile_path(combo_dir), OK_PROFILE)
        write_results(combo_dir, BAD_ROWS)
        status = solver.combo_status(combo_dir)
        self.assertEqual(status["verdict"], "failed")
        self.assertIn(solver.POINTS_BAD, status["messages"])
        self.assertEqual(status["points_bad"], 2)

    def test_a_clean_table_leaves_the_profile_verdict_alone(self):
        combo_dir = os.path.join(self.root, "run_x", "models", "default")
        write_text(profile_path(combo_dir), OK_PROFILE)
        write_results(combo_dir, GOOD_ROWS)
        status = solver.combo_status(combo_dir)
        self.assertEqual(status["verdict"], "ok")
        self.assertEqual(status["points"], 2)
        self.assertEqual(status["points_bad"], 0)

    def test_the_saved_result_is_written_only_when_asked(self):
        combo_dir = self.combo()
        solver.combo_status(combo_dir, may_write=False)
        self.assertFalse(os.path.exists(os.path.join(combo_dir, solver.CACHE_NAME)))
        solver.combo_status(combo_dir, may_write=True)
        with open(os.path.join(combo_dir, solver.CACHE_NAME), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["rules"], solver.RULES)


class ARunsVerdict(TheCaches):
    def test_a_run_takes_the_worst_of_its_combinations(self):
        run_dir = make_run(self.root, "run_20261025_142208", {
            "a": {"profiles": {"DV1_S1_V0.profile": OK_PROFILE}, "results": GOOD_ROWS},
            "b": {"profiles": {"DV1_S1_V0.profile": WARNING_PROFILE}, "results": GOOD_ROWS},
        })
        status = solver.run_status(run_dir)
        self.assertEqual(status["verdict"], "warning")
        self.assertEqual(sorted(status["combos"]), ["a", "b"])

    def test_a_run_with_no_profile_at_all_is_unknown(self):
        run_dir = make_run(self.root, "run_20261001_120000", {"default": {"results": None}})
        self.assertEqual(solver.run_status(run_dir)["verdict"], "unknown")

    def test_a_run_with_no_models_folder_is_unknown(self):
        run_dir = os.path.join(self.root, "run_20261001_120001")
        os.makedirs(run_dir)
        self.assertEqual(solver.run_status(run_dir)["verdict"], "unknown")

    def test_one_combination_with_a_verdict_is_enough_for_the_run(self):
        run_dir = make_run(self.root, "run_20261001_120002", {
            "a": {"profiles": {"DV1_S1_V0.profile": OK_PROFILE}},
            "b": {"results": None},
        })
        self.assertEqual(solver.run_status(run_dir)["verdict"], "ok")


class TheCacheLayers(TheCaches):
    def test_a_second_call_does_not_read_the_profile_again(self):
        combo_dir = self.combo()
        solver.combo_status(combo_dir)
        with mock.patch.object(solver, "read_profile_file", wraps=solver.read_profile_file) as reader:
            solver.combo_status(combo_dir)
        reader.assert_not_called()

    def test_a_profile_with_a_new_modification_time_is_read_again(self):
        combo_dir = self.combo()
        solver.combo_status(combo_dir)
        path = profile_path(combo_dir)
        info = os.stat(path)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 10 ** 9))
        with mock.patch.object(solver, "read_profile_file", wraps=solver.read_profile_file) as reader:
            solver.combo_status(combo_dir)
        self.assertEqual(reader.call_count, 1)

    def test_after_a_restart_the_saved_result_is_used_and_the_profile_is_not_parsed(self):
        combo_dir = self.combo()
        first = solver.combo_status(combo_dir, may_write=True)
        solver.clear_caches()
        with mock.patch.object(solver, "read_profile_file", wraps=solver.read_profile_file) as reader:
            again = solver.combo_status(combo_dir)
        reader.assert_not_called()
        self.assertEqual(again, first)

    def test_a_saved_result_for_a_profile_that_changed_is_not_used(self):
        combo_dir = self.combo()
        solver.combo_status(combo_dir, may_write=True)
        solver.clear_caches()
        path = profile_path(combo_dir)
        write_text(path, FAILED_PROFILE)
        os.utime(path, ns=(1, 2 * 10 ** 9))
        status = solver.combo_status(combo_dir)
        self.assertEqual(status["verdict"], "failed")

    def test_a_run_is_not_looked_at_again_until_its_state_changes(self):
        run_dir = make_run(self.root, "run_20261001_120003", {"default": {"profiles": {"p.profile": OK_PROFILE}}})
        solver.run_status(run_dir)
        with mock.patch.object(solver, "combo_status", wraps=solver.combo_status) as combo:
            solver.run_status(run_dir)
            combo.assert_not_called()
            info = os.stat(os.path.join(run_dir, "state.json"))
            os.utime(os.path.join(run_dir, "state.json"), ns=(info.st_atime_ns, info.st_mtime_ns + 10 ** 9))
            solver.run_status(run_dir)
        self.assertEqual(combo.call_count, 1)


if __name__ == "__main__":
    unittest.main()
