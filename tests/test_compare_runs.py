"""Tests for comparing runs: several runs' results on one page, what their
records say differs between them, and their numbers lined up point by
point.

The cases are the ones comparing is for: a pair of runs identical but
for one setting (the wire bonds on and off), two sweeps of the same
parameters on different grids, and a mix of the two -- where a run has
nothing on another's axis and must still not silently vanish.

Run: python -m unittest discover -s tests
"""
import copy
import json
import logging
import os
import tempfile
import unittest

import pandas as pd

from microwave_package import paths, pipeline, webapp
from microwave_package.design import equations as eq
from microwave_package.workflow import compare, plotting
from microwave_package.workflow.run_manager import Run

WITH_BONDS = "run_20260922_123524"
WITHOUT_BONDS = "run_20260922_124101"
SWEEP_ONE = "run_20260901_210825"
SWEEP_TWO = "run_20260902_123231"


def settings(**materials):
    result = copy.deepcopy(pipeline.DEFAULT_ANALYSIS_SETTINGS)
    result["materials"].update(materials)
    return result


def modes(freqs, **columns):
    """Result rows for one combination, one per mode."""
    return [{"mode": i + 1, "freq_ghz": f, "q": 100.0 * (i + 1), "solution_type": "Eigenmode",
             "combo_id": "default", **columns} for i, f in enumerate(freqs)]


def make_run(run_id, *, sweep=(), pinned=None, analysis=None, results=None,
             dimensions=None, combos=None, label=""):
    """A run on disk as the pipeline leaves one, with a chosen id --
    `Run.create` names runs by the second, so two made in one test would
    otherwise be the same run."""
    run = Run(run_id)
    run.config = {
        "sweep_params": [{"name": name, "values": list(values)} for name, values in sweep],
        "parameter_defaults": dict(pinned or {}),
        "analysis_settings": analysis if analysis is not None else settings(),
        "created_at": "2026-09-22T12:00:00",
        "label": label,
    }
    run.state = {"stage": "complete",
                 "combos": combos or {"default": {"params": {}, "post_status": "done"}}}
    run.save()
    if results is not None:
        os.makedirs(run.plots_dir, exist_ok=True)
        pd.DataFrame(results).to_csv(os.path.join(run.plots_dir, plotting.RESULTS_FILENAME), index=False)
    if dimensions is not None:
        eq.write_file(os.path.join(run.run_dir, compare.COMMON_DIMENSIONS),
                      "\n".join(f'"{name}"= {value}' for name, value in dimensions.items()) + "\n")
    return Run.load(run_id)


def entries_for(*runs):
    frames = [webapp._results_frame(run) for run in runs]
    return compare.compared(runs, [run.label or run.run_id for run in runs], frames)


class RunsOnDisk(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)

    def wire_bond_pair(self):
        """Two single-combination runs, identical but for the wire bonds."""
        shared = {"pcb_length": 26.0, "bonding_clearence": 0.6}
        return (
            make_run(WITH_BONDS, analysis=settings(include_wirebonds=True),
                     results=modes([5.8235, 5.8272, 14.15]), dimensions=shared, label="with bonds"),
            make_run(WITHOUT_BONDS, analysis=settings(include_wirebonds=False),
                     results=modes([5.8984, 5.9076, 14.24]), dimensions=shared, label="without bonds"),
        )

    def sweep_pair(self):
        """Two sweeps of the same parameter on grids that meet at one point."""
        def sweep_rows(depths, offset):
            return [{"mode": 1, "freq_ghz": 4.0 + d + offset, "q": 500.0, "pedestal_depth": d,
                     "combo_id": f"pedestal_depth-{d:g}"} for d in depths]
        return (
            make_run(SWEEP_ONE, sweep=[("pedestal_depth", [0.0, 1.0, 2.0])],
                     results=sweep_rows([0.0, 1.0, 2.0], 0.0), dimensions={"bonding_clearence": 1.0}),
            make_run(SWEEP_TWO, sweep=[("pedestal_depth", [2.0, 2.5])],
                     results=sweep_rows([2.0, 2.5], 0.1), dimensions={"bonding_clearence": 1.0}),
        )


class CombiningResultsTests(RunsOnDisk):
    def test_every_row_says_which_run_it_came_from(self):
        entries = entries_for(*self.wire_bond_pair())
        df = compare.combined_results(entries)
        self.assertEqual(sorted(df[plotting.RUN_ID_COLUMN].unique()), [WITH_BONDS, WITHOUT_BONDS])
        self.assertEqual(sorted(df[plotting.RUN_COLUMN].unique()),
                         ["A · with bonds", "B · without bonds"])

    def test_a_value_one_run_held_is_where_its_points_go_on_anothers_sweep(self):
        swept = make_run(SWEEP_ONE, sweep=[("pedestal_depth", [0.0, 1.0])],
                         results=[{"mode": 1, "freq_ghz": 4.0, "pedestal_depth": d, "combo_id": f"d{d}"}
                                  for d in (0.0, 1.0)])
        pinned = make_run(SWEEP_TWO, pinned={"pedestal_depth": 1.0},
                          results=[{"mode": 1, "freq_ghz": 4.2, "combo_id": "default"}])
        df = compare.combined_results(entries_for(swept, pinned))
        held = df[df[plotting.RUN_ID_COLUMN] == SWEEP_TWO]
        self.assertEqual(held["pedestal_depth"].tolist(), [1.0])

class RetiredParametersTests(RunsOnDisk):
    """Runs from before the distortion option was removed still hold its
    parameters. They are dropped on read, and their files are left alone."""

    def old_pair(self):
        distorted = make_run(SWEEP_ONE, pinned={"no_distortion": 0, "pcb_length": 26.0},
                             sweep=[("distortion_radius", [0.5, 1.0]), ("pedestal_depth", [0.0, 1.0])],
                             results=[{"mode": 1, "freq_ghz": 10.0, "distortion_radius": r,
                                       "no_distortion": 0, "pedestal_depth": 0.0, "combo_id": f"r{r}"}
                                      for r in (0.5, 1.0)])
        clean = make_run(WITH_BONDS, pinned={"no_distortion": 1}, results=modes([5.8]),
                         dimensions={"no_distortion": 1, "distortion_radius": 0.6})
        return distorted, clean

    def test_loading_an_old_run_drops_its_retired_settings(self):
        self.old_pair()
        run = Run.load(SWEEP_ONE)
        self.assertEqual(run.parameter_defaults, {"pcb_length": 26.0})
        self.assertEqual(run.sweep_param_names, ["pedestal_depth"])
        self.assertNotIn("no_distortion", run.starting_values())
        self.assertNotIn("distortion_radius", run.starting_values())

    def test_the_run_on_disk_keeps_its_retired_settings(self):
        self.old_pair()
        with open(os.path.join(Run(SWEEP_ONE).run_dir, "run_config.json")) as f:
            stored = json.load(f)
        self.assertEqual(stored["parameter_defaults"]["no_distortion"], 0)
        self.assertIn("distortion_radius", [p["name"] for p in stored["sweep_params"]])

    def test_comparing_old_runs_leaves_their_retired_columns_out(self):
        distorted, clean = self.old_pair()
        df = compare.combined_results(entries_for(distorted, clean))
        for column in ("no_distortion", "distortion_radius"):
            self.assertNotIn(column, df.columns)
        self.assertEqual(compare.swept_names([distorted]), ["pedestal_depth"])
        labels = [row.label for row in sum(compare.setting_rows([distorted, clean]), [])]
        self.assertFalse([label for label in labels if "istortion" in label])

    def test_the_form_opens_on_an_old_run_without_them(self):
        self.old_pair()
        settings_, values, loaded_from = webapp._form_starting_point(SWEEP_ONE, None)
        self.assertEqual(loaded_from, SWEEP_ONE)
        self.assertEqual(values["pcb_length"], 26.0)
        self.assertNotIn("no_distortion", values)
        self.assertNotIn("distortion_radius", values)


class WhatDiffersTests(RunsOnDisk):
    def test_the_one_setting_that_differs_is_the_one_listed(self):
        differing, shared = compare.setting_rows(self.wire_bond_pair())
        self.assertEqual([(row.label, row.values) for row in differing],
                         [("Include wire bonds", ["yes", "no"])])
        self.assertTrue(shared)

    def test_a_setting_a_run_never_recorded_is_not_a_difference(self):
        old = settings()
        del old["materials"]["include_wirebonds"]
        make_run(WITH_BONDS, analysis=old, results=modes([5.8]))
        make_run(WITHOUT_BONDS, analysis=settings(include_wirebonds=True), results=modes([5.8]))
        differing, shared = compare.setting_rows([Run.load(WITH_BONDS), Run.load(WITHOUT_BONDS)])
        self.assertNotIn("Include wire bonds", [row.label for row in differing])
        row = next(row for row in shared if row.label == "Include wire bonds")
        self.assertEqual(row.values, [compare.NOT_RECORDED, "yes"])

    def test_geometry_nobody_typed_in_is_still_compared(self):
        """common_dimensions.txt is the record of the equations file as it
        stood -- a changed value there is a difference even unpinned."""
        one = make_run(SWEEP_ONE, results=modes([5.8]), dimensions={"bonding_clearence": 1.0})
        two = make_run(SWEEP_TWO, results=modes([5.8]), dimensions={"bonding_clearence": 0.6})
        differing, _ = compare.setting_rows([one, two])
        row = next(row for row in differing if row.label == "Wire-bond clearance")
        self.assertEqual(row.values, ["1 mm", "0.6 mm"])

    def test_the_oldest_runs_pinned_values_inside_every_combination(self):
        """Before parameter_defaults existed a pinned value was written
        into every combination -- that is still what the run held."""
        old = make_run(SWEEP_ONE, sweep=[("pedestal_depth", [0.0, 1.0])], results=modes([3.2]), combos={
            f"pcb_side-20_pedestal_depth-{d:g}": {"params": {"pcb_side": 20.0, "pedestal_depth": d}}
            for d in (0.0, 1.0)})
        new = make_run(SWEEP_TWO, sweep=[("pedestal_depth", [0.0, 1.0])], pinned={"pcb_side": 15.0},
                       results=modes([4.1]))
        differing, _ = compare.setting_rows([old, new])
        row = next(row for row in differing if row.label == "PCB width (across feedline)")
        self.assertEqual(row.values, ["20 mm", "15 mm"])

    def test_a_one_value_sweep_is_the_value_it_held(self):
        """The oldest runs stored a pinned value as a sweep of one."""
        old = make_run(SWEEP_ONE, sweep=[("pedestal_depth", [0.0])], results=modes([3.2]),
                       combos={"pedestal_depth-0": {"params": {"pedestal_depth": 0.0}}})
        new = make_run(SWEEP_TWO, results=modes([3.2]), dimensions={"pedestal_depth": 0.0})
        differing, shared = compare.setting_rows([old, new])
        self.assertNotIn("Pedestal depth", [row.label for row in differing])
        row = next(row for row in shared if row.label == "Pedestal depth")
        self.assertEqual(row.values, ["0 mm", "0 mm"])

class LiningUpTests(RunsOnDisk):
    def test_modes_line_up_across_runs(self):
        entries = entries_for(*self.wire_bond_pair())
        table, why = compare.side_by_side(compare.combined_results(entries), entries, [])
        self.assertIsNone(why)
        first = table["rows"][0]
        self.assertEqual(first["key"], ["1"])
        self.assertEqual(first["values"]["freq_ghz"], {WITH_BONDS: 5.8235, WITHOUT_BONDS: 5.8984})

    def test_sweeps_line_up_by_value_not_by_combination_name(self):
        """A shared point is found by its swept values; the other points
        are each listed once, with the run that has them."""
        entries = entries_for(*self.sweep_pair())
        df = compare.combined_results(entries)
        df.loc[df[plotting.RUN_ID_COLUMN] == SWEEP_TWO, "combo_id"] = "named-differently"
        table, _ = compare.side_by_side(df, entries, ["pedestal_depth"])
        by_key = {tuple(row["key"]): row["values"]["freq_ghz"] for row in table["rows"]}
        self.assertEqual(set(by_key[("2", "1")]), {SWEEP_ONE, SWEEP_TWO})
        self.assertEqual(set(by_key[("0", "1")]), {SWEEP_ONE})
        self.assertEqual(len(table["rows"]), 4)

    def test_a_micrometre_sweep_from_before_its_column_had_a_unit_lines_up(self):
        """Old results hold a micrometre parameter in millimetres under its
        bare name; it still lines up with a run that wrote `_um`."""
        old = make_run(SWEEP_ONE, sweep=[("pcb_width", [0.381, 0.5])], results=[
            {"mode": 1, "freq_ghz": 4.0 + w, "pcb_width": w, "combo_id": f"w{w}"} for w in (0.381, 0.5)])
        new = make_run(SWEEP_TWO, sweep=[("pcb_width", [0.381, 0.635])], results=[
            {"mode": 1, "freq_ghz": 4.1 + w / 1000, "pcb_width_um": w, "combo_id": f"w{w}"} for w in (381.0, 635.0)])
        entries = entries_for(old, new)
        table, why = compare.side_by_side(compare.combined_results(entries), entries, ["pcb_width"])
        self.assertIsNone(why)
        by_key = {tuple(row["key"]): row["values"]["freq_ghz"] for row in table["rows"]}
        self.assertEqual(set(by_key[("381", "1")]), {SWEEP_ONE, SWEEP_TWO})

    def test_results_with_nothing_to_line_up_by_are_not_a_table(self):
        rows = [{"freq_ghz": 5.0, "q": 100.0}]
        entries = entries_for(make_run(SWEEP_ONE, results=rows), make_run(SWEEP_TWO, results=rows))
        self.assertEqual(compare.side_by_side(compare.combined_results(entries), entries, []), (None, None))

    def test_curves_are_not_lined_up_as_points(self):
        rows = [{"freq_ghz": f, "s11_db": -10.0, "combo_id": "default"} for f in (1.0, 2.0, 3.0)]
        one = make_run(SWEEP_ONE, results=rows)
        two = make_run(SWEEP_TWO, results=rows)
        entries = entries_for(one, two)
        table, why = compare.side_by_side(compare.combined_results(entries), entries, [])
        self.assertIsNone(table)
        self.assertIn("curves", why)


class DefaultAxesTests(RunsOnDisk):
    def test_runs_with_nothing_swept_are_spread_out_by_mode(self):
        entries = entries_for(*self.wire_bond_pair())
        axes = compare.default_axes(compare.combined_results(entries), [], "Eigenmode")
        self.assertEqual((axes["x"], axes["y"], axes["color"]), ("mode", "freq_ghz", plotting.RUN_COLUMN))

    def test_sweeps_open_on_their_swept_parameter(self):
        entries = entries_for(*self.sweep_pair())
        axes = compare.default_axes(compare.combined_results(entries), ["pedestal_depth"], "Eigenmode")
        self.assertEqual(axes["x"], "pedestal_depth")

class PlottingSeveralRunsTests(RunsOnDisk):
    def setUp(self):
        super().setUp()
        self.entries = entries_for(*self.wire_bond_pair())
        self.df = compare.combined_results(self.entries)

    def test_the_run_is_an_axis_and_a_filter(self):
        self.assertIn(plotting.RUN_COLUMN, [o["value"] for o in plotting.axis_options(self.df, [])])
        first = plotting.filter_options(self.df, [])[0]
        self.assertEqual(first["column"], plotting.RUN_ID_COLUMN)
        self.assertEqual([(v["key"], v["label"]) for v in first["values"]],
                         [(WITH_BONDS, "A · with bonds"), (WITHOUT_BONDS, "B · without bonds")])

    def test_filtering_by_run_keeps_only_its_rows(self):
        kept = plotting.apply_filters(self.df, {plotting.RUN_ID_COLUMN: [WITHOUT_BONDS]})
        self.assertEqual(kept[plotting.RUN_ID_COLUMN].unique().tolist(), [WITHOUT_BONDS])

    def test_no_run_is_left_off_the_plot_it_opens_on(self):
        """A single run has no value on another run's sweep, so the plot
        opens on the runs themselves rather than drop it."""
        swept = make_run(SWEEP_ONE, sweep=[("pedestal_depth", [0.0, 1.0])],
                         results=[{"mode": 1, "freq_ghz": 10.0, "pedestal_depth": d, "combo_id": f"d{d}"}
                                  for d in (0.0, 1.0)])
        df = compare.combined_results(entries_for(swept, Run.load(WITH_BONDS)))
        axes = compare.default_axes(df, ["pedestal_depth"], "Eigenmode")
        self.assertEqual(axes["x"], plotting.RUN_COLUMN)
        self.assertEqual(compare.runs_off_axes(df, axes["x"], axes["y"]), [])
        self.assertEqual(compare.runs_off_axes(df, "pedestal_depth", "freq_ghz"), ["B · with bonds"])

    def test_filtering_a_parameter_keeps_the_runs_that_never_had_it(self):
        """Unticking one value of a swept parameter hides the points at that
        value -- not a compared run that never swept it."""
        swept = make_run(SWEEP_ONE, sweep=[("pedestal_depth", [0.0, 1.0])],
                         results=[{"mode": 1, "freq_ghz": 10.0, "pedestal_depth": d, "combo_id": f"d{d}"}
                                  for d in (0.0, 1.0)])
        df = compare.combined_results(entries_for(swept, Run.load(WITH_BONDS)))
        kept = plotting.apply_filters(df, {"pedestal_depth": ["1"]})
        self.assertEqual(sorted(kept[plotting.RUN_ID_COLUMN].unique()), [SWEEP_ONE, WITH_BONDS])
        self.assertEqual(kept.loc[kept[plotting.RUN_ID_COLUMN] == SWEEP_ONE, "pedestal_depth"].tolist(), [1.0])

    def test_a_run_keeps_its_colour_when_the_others_are_filtered_out(self):
        colours = plotting.run_colours([entry.name for entry in self.entries])
        only_b = plotting.apply_filters(self.df, {plotting.RUN_ID_COLUMN: [WITHOUT_BONDS]})
        html = plotting.figure_html(only_b, [], x="mode", y="freq_ghz", color=plotting.RUN_COLUMN,
                                    plotlyjs_src="/plotly.js", run_colours=colours)
        self.assertIn(plotting.RUN_COLOURS["light"][1], html)
        self.assertNotIn(plotting.RUN_COLOURS["light"][0], html)

    def test_a_run_axis_is_labelled_by_letter(self):
        html = plotting.figure_html(self.df, [], x=plotting.RUN_COLUMN, y="freq_ghz",
                                    color=plotting.RUN_COLUMN, plotlyjs_src="/plotly.js")
        self.assertIn('"ticktext":["A","B"]', html)


class ComparePageTests(RunsOnDisk):
    def setUp(self):
        super().setUp()
        self.client = webapp.app.test_client()

    def page(self, query):
        return self.client.get("/compare?" + query).get_data(as_text=True)

    def test_the_page_lists_both_runs_and_what_differs(self):
        self.wire_bond_pair()
        html = self.page(f"runs={WITHOUT_BONDS}&runs={WITH_BONDS}")
        self.assertIn("with bonds", html)
        self.assertIn("without bonds", html)
        self.assertIn("Include wire bonds", html)

    def test_oldest_first_whatever_order_the_url_names_them_in(self):
        self.wire_bond_pair()
        with webapp.app.test_request_context(f"/compare?runs={WITHOUT_BONDS},{WITH_BONDS}"):
            entries, _ = webapp._compared_runs(webapp.request.args)
        self.assertEqual([entry.run_id for entry in entries], [WITH_BONDS, WITHOUT_BONDS])

    def test_anything_but_a_run_id_is_dropped_and_a_missing_run_is_said(self):
        self.wire_bond_pair()
        html = self.page(f"runs=../../etc,{WITH_BONDS},run_20200101_000000")
        self.assertNotIn("../../etc", html)
        self.assertIn("run_20200101_000000 could not be read", html)

    def test_the_plot_draws_both_runs_in_their_own_colours(self):
        """With no axes in the URL, the plot opens where the page does:
        coloured by run, each run in its own colour."""
        self.wire_bond_pair()
        html = self.client.get(f"/compare/plot?runs={WITH_BONDS},{WITHOUT_BONDS}").get_data(as_text=True)
        for name in ("A · with bonds", "B · without bonds"):
            self.assertIn(json.dumps(name)[1:-1], html)  # as the figure's JSON spells it
        for colour in plotting.RUN_COLOURS["light"][:2]:
            self.assertIn(colour, html)

    def test_an_empty_colour_is_no_colour(self):
        self.wire_bond_pair()
        html = self.client.get(f"/compare/plot?runs={WITH_BONDS},{WITHOUT_BONDS}&x=mode&y=freq_ghz&color=")
        self.assertNotIn(plotting.RUN_COLOURS["light"][1], html.get_data(as_text=True))

    def test_the_csv_holds_every_run_and_says_which(self):
        self.wire_bond_pair()
        response = self.client.get(f"/compare/results.csv?runs={WITH_BONDS},{WITHOUT_BONDS}")
        text = response.get_data(as_text=True)
        # marked as UTF-8 for Excel, which otherwise garbles the names' "·"
        self.assertTrue(text.startswith("﻿"))
        lines = text.lstrip("﻿").splitlines()
        self.assertTrue(lines[0].startswith("run_id,run,"))
        self.assertEqual(len(lines), 1 + 6)

    def test_a_lone_run_with_results_is_still_drawn_in_its_own_colour(self):
        make_run(SWEEP_ONE)  # A: no results yet
        make_run(WITHOUT_BONDS, results=modes([5.8984, 5.9076]), label="without bonds")
        page = self.page(f"runs={SWEEP_ONE},{WITHOUT_BONDS}")
        self.assertRegex(page, rf'<option value="{plotting.RUN_COLUMN}"\s+selected')
        plot = self.client.get(f"/compare/plot?runs={SWEEP_ONE},{WITHOUT_BONDS}&x=mode&y=freq_ghz"
                               f"&color={plotting.RUN_COLUMN}").get_data(as_text=True)
        self.assertIn(plotting.RUN_COLOURS["light"][1], plot)

    def test_a_run_without_results_cannot_be_ticked(self):
        self.wire_bond_pair()
        make_run(SWEEP_ONE)  # never got as far as results
        html = self.client.get("/").get_data(as_text=True)
        self.assertRegex(html, rf'value="{SWEEP_ONE}"[^>]*\s+disabled')
        self.assertNotRegex(html, rf'value="{WITH_BONDS}"[^>]*\s+disabled')

    def test_changing_the_selection_comes_back_with_it_ticked(self):
        self.wire_bond_pair()
        html = self.client.get(f"/?compare={WITH_BONDS},{WITHOUT_BONDS}").get_data(as_text=True)
        self.assertRegex(html, rf'value="{WITH_BONDS}"[^>]*\s+checked')
        self.assertRegex(html, rf'value="{WITHOUT_BONDS}"[^>]*\s+checked')


if __name__ == "__main__":
    unittest.main()
