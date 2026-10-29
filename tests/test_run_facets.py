"""Tests for what a run was built with, as the run list filters on it and the
run page shows it (microwave_package/workflow/run_facets.py).

The cases: each setting's fallback chain -- swept, pinned, the run's own
common_dimensions.txt, then the committed defaults -- a sweep that is mixed
and one that is not, a solver that builds its own model, a run record with
keys missing, and the cache over the equation files.

Every run folder and equation file here is a temporary one; nothing reads
the real runs/ folder.

Run: python -m unittest discover -s tests
"""
import logging
import os
import shutil
import tempfile
import unittest
from unittest import mock

from microwave_package import paths
from microwave_package.design import equations as eq
from microwave_package.workflow import compare, run_facets
from microwave_package.workflow.run_manager import facet_inputs

DEFAULTS = """\
"no_chip" = 0 ' (boolean) when set to 1, the bare board
"pedestal_depth"= 0.5\t' depth of the pedestal under the chip
"pedestal_shape"= 0\t' shape of the pedestal recess
"vents_on" = 1 ' (boolean)
"screws_on" = 1 ' (boolean)
"floor_width" = IIF ( "screws_on" > 0.5 , "wall_width" , "wall_width" ) ' a formula, not a number
"""


def facets_of(config, run_dir=None, **kwargs):
    return run_facets.run_facets(facet_inputs(config), run_dir, **kwargs)


def settings_of(config, run_dir=None):
    return run_facets.settings_of(facet_inputs(config), run_dir)


def analysis(**materials):
    return {"solution_type": "DrivenModal", "materials": {"mat_pcb": "Rogers TMM 10i (tm)", **materials}}


def write_common(run_dir, values):
    os.makedirs(run_dir, exist_ok=True)
    lines = [f'"{name}" = {value} \' from the build' for name, value in values.items()]
    eq.write_file(os.path.join(run_dir, compare.COMMON_DIMENSIONS), "\n".join(lines) + "\n")


class EquationFilesOnDisk(unittest.TestCase):
    """A temporary committed-defaults file, and a run folder, for each test."""

    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = tempfile.mkdtemp(prefix="facets_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.defaults = os.path.join(self.tmp, "default.txt")
        eq.write_file(self.defaults, DEFAULTS)
        patcher = mock.patch.object(paths, "DEFAULT_EQUATIONS_FILE", self.defaults)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.run_dir = os.path.join(self.tmp, "run_20261029_120000")
        os.makedirs(self.run_dir)


class SubstrateNames(unittest.TestCase):
    def test_the_library_names_are_shortened(self):
        self.assertEqual(run_facets.short_substrate("Rogers TMM 10i (tm)"), "TMM 10i")
        self.assertEqual(run_facets.short_substrate("Rogers RT/duroid 5880 (tm)"), "RT/duroid 5880")
        self.assertEqual(run_facets.short_substrate("Rogers RO3003 (tm)"), "RO3003")

    def test_other_names_are_kept_as_they_are(self):
        self.assertEqual(run_facets.short_substrate("FR4_epoxy"), "FR4_epoxy")
        self.assertEqual(run_facets.short_substrate("sapphire"), "sapphire")

    def test_a_run_without_a_recorded_substrate_is_built_on_the_default(self):
        self.assertEqual(facets_of({})["substrate"], "TMM 10i")
        self.assertEqual(facets_of({"analysis_settings": {"materials": {"mat_pcb": ""}}})["substrate"], "TMM 10i")

    def test_solution_labels_are_the_form_names(self):
        self.assertEqual(run_facets.solution_label("DrivenModal"), "Driven Modal")
        self.assertEqual(run_facets.solution_label("Eigenmode"), "Eigenmode")
        self.assertEqual(run_facets.solution_label("LaunchStudy"), "Launch study")
        self.assertEqual(run_facets.solution_label("LineImpedance"), "Line impedance")
        self.assertEqual(run_facets.solution_label("Unheard-of"), "Unheard-of")


class PinnedValues(EquationFilesOnDisk):
    def test_a_pinned_switch_and_depth_say_what_the_board_is(self):
        facets = facets_of({"parameter_defaults": {"no_chip": 1.0, "pedestal_depth": 0.5},
                            "analysis_settings": analysis()})
        self.assertEqual(facets["chip"], "bare")
        self.assertEqual(facets["pedestal"], "recess")
        self.assertEqual(facets["shape"], "square")  # the default shape, the board had a recess

    def test_a_pinned_flat_floor_and_a_chip(self):
        facets = facets_of({"parameter_defaults": {"no_chip": 0.0, "pedestal_depth": 0.0},
                            "analysis_settings": analysis()})
        self.assertEqual(facets["chip"], "chip")
        self.assertEqual(facets["pedestal"], "flat")
        self.assertEqual(facets["shape"], "")  # no recess, so no shape to speak of

    def test_a_pinned_circle_is_the_circle_shape(self):
        facets = facets_of({"parameter_defaults": {"pedestal_depth": 0.5, "pedestal_shape": 1.0},
                            "analysis_settings": analysis()})
        self.assertEqual(facets["shape"], "circle")

    def test_the_pin_wins_over_the_equation_files(self):
        write_common(self.run_dir, {"no_chip": 1.0})
        facets = facets_of({"parameter_defaults": {"no_chip": 0.0}, "analysis_settings": analysis()},
                           self.run_dir)
        self.assertEqual(facets["chip"], "chip")


class SweptValues(EquationFilesOnDisk):
    def test_a_sweep_over_both_kinds_of_value_is_mixed(self):
        """A run whose pedestal depth goes from 0 to a recess matches either
        filter: it has both kinds of board."""
        config = {"sweep_params": [{"name": "pedestal_depth", "values": [0.0, 0.25, 1.0]}],
                  "analysis_settings": analysis()}
        self.assertEqual(facets_of(config)["pedestal"], "flat recess")

    def test_a_sweep_that_is_all_recess_is_not_mixed(self):
        """Not every swept parameter is mixed: depths of 0.25 and 0.5 are both
        recesses, so a flat-floor filter must not find the run."""
        config = {"sweep_params": [{"name": "pedestal_depth", "values": [0.25, 0.5]}],
                  "analysis_settings": analysis()}
        self.assertEqual(facets_of(config)["pedestal"], "recess")

    def test_a_one_value_sweep_is_that_value(self):
        """The oldest runs wrote a pinned value as a one-value sweep."""
        config = {"sweep_params": [{"name": "no_chip", "values": [1.0]}],
                  "analysis_settings": analysis()}
        self.assertEqual(facets_of(config)["chip"], "bare")

    def test_a_swept_chip_is_mixed(self):
        config = {"sweep_params": [{"name": "no_chip", "values": [0.0, 1.0]}],
                  "analysis_settings": analysis()}
        self.assertEqual(facets_of(config)["chip"], "bare chip")

    def test_the_sweep_wins_over_a_pin(self):
        config = {"parameter_defaults": {"no_chip": 0.0},
                  "sweep_params": [{"name": "no_chip", "values": [1.0]}],
                  "analysis_settings": analysis()}
        self.assertEqual(facets_of(config)["chip"], "bare")

    def test_the_settings_say_so_on_the_run_page(self):
        config = {"sweep_params": [{"name": "pedestal_depth", "values": [0.0, 1.0]}],
                  "analysis_settings": analysis()}
        depth = settings_of(config)["pedestal_depth"]
        self.assertEqual(depth.source, run_facets.SWEPT)
        self.assertEqual(depth.values, (0.0, 1.0))


class EquationFileFallbacks(EquationFilesOnDisk):
    def test_a_run_that_pinned_nothing_takes_its_common_dimensions(self):
        """A run that did not pin the switch or the depth still says what it
        built: the equation lines every combination shared."""
        write_common(self.run_dir, {"no_chip": 1.0, "pedestal_depth": 0.5, "pedestal_shape": 0.0})
        facets = facets_of({"analysis_settings": analysis()}, self.run_dir)
        self.assertEqual(facets["chip"], "bare")
        self.assertEqual(facets["pedestal"], "recess")
        self.assertEqual(facets["shape"], "square")
        self.assertEqual(settings_of({"analysis_settings": analysis()}, self.run_dir)["no_chip"].source,
                         run_facets.COMMON)

    def test_a_run_with_no_common_dimensions_takes_the_committed_defaults(self):
        """A run stopped before its model was built has no common_dimensions.txt."""
        facets = facets_of({"analysis_settings": analysis()}, self.run_dir)
        self.assertEqual(facets["chip"], "chip")       # no_chip = 0 in the defaults
        self.assertEqual(facets["pedestal"], "recess")  # pedestal_depth = 0.5 in the defaults
        self.assertEqual(settings_of({"analysis_settings": analysis()}, self.run_dir)["no_chip"].source,
                         run_facets.DEFAULT)

    def test_the_switches_come_from_the_same_chain(self):
        settings = settings_of({"analysis_settings": analysis()}, self.run_dir)
        self.assertEqual(settings["vents_on"].values, (1.0,))
        self.assertEqual(settings["screws_on"].source, run_facets.DEFAULT)

    def test_a_formula_in_the_defaults_is_not_taken_as_a_value(self):
        self.assertEqual(settings_of({"analysis_settings": analysis()}, self.run_dir)["pedestal_depth"].values,
                         (0.5,))

    def test_a_missing_defaults_file_leaves_the_setting_unsaid(self):
        with mock.patch.object(paths, "DEFAULT_EQUATIONS_FILE", os.path.join(self.tmp, "absent.txt")):
            facets = facets_of({"analysis_settings": analysis()}, self.run_dir)
        self.assertEqual(facets["chip"], "")
        self.assertEqual(facets["pedestal"], "")

    def test_the_equation_files_are_read_again_when_they_change(self):
        """The helper caches the files, keyed by their version: a rewritten
        common_dimensions.txt is read, not the stale copy."""
        write_common(self.run_dir, {"no_chip": 1.0})
        config = {"analysis_settings": analysis()}
        self.assertEqual(facets_of(config, self.run_dir)["chip"], "bare")
        write_common(self.run_dir, {"no_chip": 0.0})
        later = os.stat(os.path.join(self.run_dir, compare.COMMON_DIMENSIONS)).st_mtime_ns + 10**9
        os.utime(os.path.join(self.run_dir, compare.COMMON_DIMENSIONS), ns=(later, later))
        self.assertEqual(facets_of(config, self.run_dir)["chip"], "chip")


class SolversThatDrawTheirOwnModel(EquationFilesOnDisk):
    def test_an_unpinned_launch_study_takes_no_board_defaults(self):
        """The launch study builds one connector on its own board, so the
        board's defaults (a chip, a recess) do not describe it: they are n/a."""
        config = {"analysis_settings": {"solution_type": "LaunchStudy", "materials": {}}}
        facets = facets_of(config, self.run_dir)
        self.assertEqual(facets["chip"], "")
        self.assertEqual(facets["pedestal"], "")
        rows = run_facets.geometry_rows(facet_inputs(config), self.run_dir)
        chip = next(row for row in rows if row["label"] == "Chip")
        self.assertEqual(chip["text"], run_facets.NOT_APPLICABLE_TEXT)

    def test_a_pinned_setting_of_such_a_solver_still_counts(self):
        config = {"parameter_defaults": {"no_chip": 1.0, "pedestal_depth": 0.0},
                  "analysis_settings": {"solution_type": "LaunchStudy"}}
        facets = facets_of(config, self.run_dir)
        self.assertEqual(facets["chip"], "bare")
        self.assertEqual(facets["pedestal"], "flat")

    def test_a_line_impedance_check_is_n_a_too(self):
        config = {"analysis_settings": {"solution_type": "LineImpedance"}}
        self.assertEqual(facets_of(config, self.run_dir)["chip"], "")


class RunStages(unittest.TestCase):
    def test_each_stage_has_its_word(self):
        self.assertEqual(facets_of({}, stage="complete")["stage"], "complete")
        self.assertEqual(facets_of({}, stage="stopped")["stage"], "stopped")
        self.assertEqual(facets_of({}, stage="created")["stage"], "created")
        self.assertEqual(facets_of({}, stage="monitoring")["stage"], "running")
        self.assertEqual(facets_of({}, stage="geometry_ready")["stage"], "running")

    def test_a_failed_run_and_a_complete_run_with_failures(self):
        self.assertEqual(facets_of({}, stage="error")["stage"], "failed")
        self.assertEqual(facets_of({}, stage="complete", failed=2)["stage"], "complete failed")
        self.assertEqual(facets_of({}, stage="monitoring", failed=1)["stage"], "failed running")

    def test_no_stage_says_nothing(self):
        self.assertEqual(facets_of({})["stage"], "")


class PairedRuns(unittest.TestCase):
    def test_a_pair_matches_both_solution_types(self):
        config = {"eigen_run_id": "run_x_eigen",
                  "analysis_settings": {"solution_type": "DrivenModal", "materials": {}}}
        self.assertEqual(facets_of(config)["solution"], "DrivenModal Eigenmode")

    def test_a_standalone_run_matches_its_own_type_only(self):
        self.assertEqual(facets_of({"analysis_settings": {"solution_type": "DrivenModal"}})["solution"],
                         "DrivenModal")


class MissingKeys(EquationFilesOnDisk):
    def test_an_empty_record_does_not_raise(self):
        facets = facets_of({}, self.run_dir, stage="complete")
        self.assertEqual(facets["substrate"], "TMM 10i")
        self.assertEqual(facets["solution"], "Eigenmode")  # the default solver
        rows = run_facets.geometry_rows(facet_inputs({}), self.run_dir)
        self.assertTrue(all(row["text"] for row in rows))
        self.assertEqual(run_facets.title_chips(facet_inputs({}), self.run_dir)[0], "TMM 10i")

    def test_a_setting_nothing_records_says_not_recorded(self):
        """With no record and no equation file to say, the chip and pedestal
        are stated as not recorded -- and no word is claimed for them."""
        absent = os.path.join(self.tmp, "absent.txt")
        with mock.patch.object(paths, "DEFAULT_EQUATIONS_FILE", absent):
            rows = {row["label"]: row for row in
                    run_facets.geometry_rows(facet_inputs({}), os.path.join(self.tmp, "no_run"))}
            facets = facets_of({}, os.path.join(self.tmp, "no_run"))
        self.assertEqual(rows["Chip"]["text"], "not recorded")
        self.assertEqual(rows["Pedestal"]["text"], "not recorded")
        self.assertEqual(facets["chip"], "")
        self.assertEqual(facets["pedestal"], "")


class TitleChips(EquationFilesOnDisk):
    def test_the_chips_say_substrate_chip_and_pedestal(self):
        config = {"parameter_defaults": {"no_chip": 1.0, "pedestal_depth": 0.5},
                  "analysis_settings": analysis()}
        self.assertEqual(run_facets.title_chips(facet_inputs(config), self.run_dir),
                         ["TMM 10i", "No chip", "Recess 0.5 mm"])

    def test_a_flat_floor_with_a_chip(self):
        config = {"parameter_defaults": {"no_chip": 0.0, "pedestal_depth": 0.0},
                  "analysis_settings": analysis(mat_pcb="Rogers RO3003 (tm)")}
        self.assertEqual(run_facets.title_chips(facet_inputs(config), self.run_dir),
                         ["RO3003", "With chip", "No pedestal"])

    def test_a_swept_pedestal_is_called_swept(self):
        config = {"sweep_params": [{"name": "pedestal_depth", "values": [0.0, 0.5]}],
                  "analysis_settings": analysis()}
        self.assertEqual(run_facets.title_chips(facet_inputs(config), self.run_dir)[-1], "Pedestal swept")


class GeometryRows(EquationFilesOnDisk):
    def test_the_rows_say_what_each_setting_was_and_where_it_came_from(self):
        config = {"parameter_defaults": {"no_chip": 1.0, "pedestal_depth": 0.5, "screws_on": 0.0},
                  "sweep_params": [{"name": "pcb_length", "values": [40.0, 60.0]}],
                  "analysis_settings": analysis()}
        rows = {row["label"]: row for row in run_facets.geometry_rows(facet_inputs(config), self.run_dir)}
        self.assertEqual(rows["Chip"]["text"], "no chip (bare board)")
        self.assertEqual(rows["Chip"]["source"], "pinned for this run")
        self.assertEqual(rows["Pedestal"]["text"], "recess 0.5 mm, square")
        self.assertEqual(rows["Corner screws"]["text"], "off")
        self.assertEqual(rows["Vent grooves"]["source"], "from the equation file")
        self.assertEqual(rows["PCB length (along feedline)"]["text"], "40, 60 mm")
        self.assertEqual(rows["PCB length (along feedline)"]["source"], "swept")


class EquationLineFormats(unittest.TestCase):
    """The lines common_dimensions.txt and the defaults use -- with and without
    a space before the equals sign, a comment, a tab -- read as numbers."""

    def test_both_spellings_of_a_line_are_read(self):
        tmp = tempfile.mkdtemp(prefix="facets_fmt_")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, compare.COMMON_DIMENSIONS)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('"no_chip" = 1.0 \' (boolean) when set to 1\n"pedestal_depth"= 0.5\t\' depth\n')
        self.assertEqual(run_facets._numbers_in(path), {"no_chip": 1.0, "pedestal_depth": 0.5})

    def test_a_missing_file_is_no_numbers(self):
        self.assertEqual(run_facets._numbers_in(os.path.join(tempfile.gettempdir(), "no_such_facets.txt")), {})


if __name__ == "__main__":
    unittest.main()
