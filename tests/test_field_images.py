"""Tests for the field images of a driven run: the field image part that solves
one frequency for them, the worker's exports read back, the pictures drawn from
those, and what the run list and the run page say about all of it.

The worker is never called. Its output -- the fields/<name>.fld files and the
result.json beside them -- is written by hand, in the format its ARCHITECTURE.md
(section 3) describes: metres in the rows, z changing fastest, `Nan` where there
is no field. The pictures need matplotlib, and the export entries need
ansys-analyze-common 0.1.4 (worker v0.2.0): the tests that need either are
skipped, with the reason, when it is not installed.

Every run is written into a temporary runs folder.

Run: python -m unittest discover -s tests
"""
import json
import logging
import os
import shutil
import tempfile
import time
import types
import unittest
from unittest import mock

from microwave_package import paths, pipeline, webapp
from microwave_package.design.sweep import SweepParameter
from microwave_package.hfss.driven import field_exports as fields_plan
from microwave_package.hfss.driven import setup as driven_setup
from microwave_package.workflow import companion, field_images, postprocess
from microwave_package.workflow.run_manager import Run, part_kind, part_record

try:
    import matplotlib  # noqa: F401
    HAVE_MATPLOTLIB = True
except ImportError:
    HAVE_MATPLOTLIB = False

try:
    from ansys_analyze_common import queue_common
    HAVE_FIELD_EXPORTS = hasattr(queue_common, "FIELD_EXPORT_NAME_PATTERN")
except ImportError:  # pragma: no cover -- the common package is always installed for the suite
    queue_common = None
    HAVE_FIELD_EXPORTS = False

NEEDS_MATPLOTLIB = unittest.skipUnless(HAVE_MATPLOTLIB, "matplotlib is not installed")
NEEDS_FIELD_EXPORTS = unittest.skipUnless(
    HAVE_FIELD_EXPORTS, "ansys-analyze-common without field_exports (worker v0.2.0 is needed)")

SWEEP = [SweepParameter(name="pedestal_depth", values=[0.0, 1.0])]
FREQUENCY = 6.9003
# The 50 x 25 mm board of the worker's own fixture, 0.79 mm thick, in a box with a
# cavity 9 mm high. Millimetres, as the bounding boxes come out of the modeler.
BOARD = {
    "substrate": [-25.0, 0.0, -12.5, 25.0, 0.79, 12.5],
    "vacuum": [-27.0, -1.5, -14.0, 27.0, 9.0, 14.0],
    "box": [-27.5, -1.5, -14.5, 27.5, 9.5, 14.5],
}

FLD_HEADER = ("Grid Output Min: [-25mm 1.19mm -12.5mm] Max: [25mm 1.19mm 12.5mm] "
              "Grid Size: [0.25mm 1mm 0.25mm]\n"
              'X, Y, Z, Scalar data "Mag_E"\n')


def write_fld(path, rows_mm):
    """A field file as the worker writes it: rows of (x, y, z, value) given in
    millimetres, written in metres. A value of None is written `Nan`."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(FLD_HEADER)
        for x, y, z, value in rows_mm:
            text = "Nan" if value is None else repr(float(value))
            handle.write(f"{x / 1000:.7f} {y / 1000:.7f} {z / 1000:.7f} {text}\n")


def grid_rows(xs, ys, zs, value):
    """The rows of a grid in the worker's order: z fastest, then y, then x."""
    return [(x, y, z, value(x, y, z)) for x in xs for y in ys for z in zs]


def plane_rows():
    """A small plane through the board, y = 1.19 mm, with one point inside metal."""
    return grid_rows([-25.0, 0.0, 25.0], [1.19], [-12.5, 0.0, 12.5],
                     lambda x, y, z: None if (x, z) == (0.0, 0.0) else 1.0 + abs(x) + 10.0 * abs(z))


def worker_output(combo_dir, exports, *, status="success", with_block=True):
    """What the worker leaves in a combination's folder: fields/<name>.fld for each
    export that came out, and result.json. `exports` maps a name to
    {"ok": True, "rows": [...]} or {"ok": False, "error": ..., "aedt_messages": [...]}."""
    os.makedirs(os.path.join(combo_dir, "fields"), exist_ok=True)
    block = {}
    for name, outcome in exports.items():
        if outcome["ok"]:
            write_fld(os.path.join(combo_dir, "fields", f"{name}.fld"), outcome["rows"])
            block[name] = {"success": True, "file": f"fields/{name}.fld", "points": len(outcome["rows"])}
        else:
            block[name] = {"success": False, "error": outcome["error"],
                           "aedt_messages": outcome.get("aedt_messages", [])}
    result = {"status": status, "task_id": "task", "task_type": "run_setups",
              "result": {"Setup_1": {"success": True}}}
    if with_block:
        result["field_exports"] = block
    with open(os.path.join(combo_dir, "result.json"), "w", encoding="utf-8") as handle:
        json.dump(result, handle)


class RunsRoot(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(shutil.rmtree, paths.RUNS_ROOT, True)
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)

    def driven_run(self, finished=True, solution="DrivenModal"):
        run = Run.create(SWEEP, {**pipeline.DEFAULT_ANALYSIS_SETTINGS, "solution_type": solution,
                                 "f_stop_ghz": 20.0}, {})
        if finished:
            for combo in run.state["combos"]:
                run.update_combo(combo, post_status="done")
        return run


class FieldFileFormat(RunsRoot):
    """What read_fld makes of the worker's file."""

    def setUp(self):
        super().setUp()
        self.folder = tempfile.mkdtemp(prefix="fld_")
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_the_rows_come_back_in_millimetres_on_their_own_grid(self):
        path = os.path.join(self.folder, "plane.fld")
        write_fld(path, plane_rows())
        grid = field_images.read_fld(path)
        xs, ys, zs = grid.axes
        self.assertEqual(xs.tolist(), [-25.0, 0.0, 25.0])
        self.assertEqual(ys.tolist(), [1.19])
        self.assertEqual(zs.tolist(), [-12.5, 0.0, 12.5])
        self.assertEqual(grid.values.shape, (3, 1, 3))
        self.assertAlmostEqual(float(grid.values[2, 0, 2]), 1.0 + 25.0 + 125.0)
        self.assertAlmostEqual(float(grid.values[0, 0, 0]), 1.0 + 25.0 + 125.0)

    def test_a_nan_row_is_no_field_not_zero(self):
        import numpy as np
        path = os.path.join(self.folder, "plane.fld")
        write_fld(path, plane_rows())
        grid = field_images.read_fld(path)
        self.assertTrue(np.isnan(grid.values[1, 0, 1]))
        self.assertEqual(int(np.isnan(grid.values).sum()), 1)

    def test_the_order_of_the_rows_does_not_matter(self):
        import numpy as np
        forward = os.path.join(self.folder, "forward.fld")
        backward = os.path.join(self.folder, "backward.fld")
        rows = plane_rows()
        write_fld(forward, rows)
        write_fld(backward, list(reversed(rows)))
        a, b = field_images.read_fld(forward), field_images.read_fld(backward)
        np.testing.assert_array_equal(a.values, b.values)

    def test_rows_that_do_not_fill_a_grid_are_refused(self):
        path = os.path.join(self.folder, "short.fld")
        write_fld(path, plane_rows()[:-1])
        with self.assertRaisesRegex(ValueError, "do not fill a grid"):
            field_images.read_fld(path)

    def test_a_file_with_no_values_is_refused(self):
        path = os.path.join(self.folder, "empty.fld")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(FLD_HEADER)
        with self.assertRaisesRegex(ValueError, "no field values"):
            field_images.read_fld(path)


@NEEDS_MATPLOTLIB
class Pictures(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="png_")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.plane = os.path.join(self.folder, "plane.fld")
        write_fld(self.plane, plane_rows())

    def test_a_plane_is_drawn_as_a_png_on_two_colour_scales(self):
        png = os.path.join(self.folder, "plane.png")
        field_images.draw_png(field_images.read_fld(self.plane), png,
                              "|E| at 6.9003 GHz: middle of the board", "E")
        with open(png, "rb") as handle:
            self.assertEqual(handle.read(8), b"\x89PNG\r\n\x1a\n")
        self.assertGreater(os.path.getsize(png), 2000)

    def test_a_volume_is_drawn_as_its_largest_value_along_the_vertical(self):
        rows = grid_rows([0.0, 1.0, 2.0], [0.0, 1.0], [0.0, 1.0, 2.0],
                         lambda x, y, z: 0.0 if y == 0.0 and x == 1.0 else 3.0 + x + y + z)
        path = os.path.join(self.folder, "volume.fld")
        write_fld(path, rows)
        png = os.path.join(self.folder, "volume.png")
        field_images.draw_png(field_images.read_fld(path), png, "|H| at 6.9003 GHz", "H")
        self.assertTrue(os.path.getsize(png) > 2000)

    def test_a_grid_with_one_varying_axis_has_no_picture(self):
        rows = grid_rows([0.0], [0.0], [0.0, 1.0], lambda x, y, z: 1.0)
        path = os.path.join(self.folder, "line.fld")
        write_fld(path, rows)
        with self.assertRaisesRegex(ValueError, "fewer than two"):
            field_images.draw_png(field_images.read_fld(path), os.path.join(self.folder, "x.png"),
                                  "t", "E")


class ExportEntries(unittest.TestCase):
    """The field_exports the worker is asked for, from the model's boxes."""

    def entries(self, boxes=BOARD, quantities=("E", "H"), sources=None):
        return fields_plan.plan_exports(boxes, FREQUENCY, list(quantities), sources)

    def by_name(self, entries):
        return {entry["name"]: entry for entry in entries}

    def test_each_grid_is_written_for_each_quantity(self):
        entries = self.entries()
        names = [entry["name"] for entry in entries]
        self.assertEqual(len(names), 10)
        self.assertEqual(len(set(names)), 10)
        self.assertEqual(sorted(names)[0], "cavity_mid_E")
        self.assertIn("section_xy_H", names)
        self.assertIn("vacuum_volume_E", names)

    def test_every_entry_names_the_one_adaptive_solution_at_the_frequency(self):
        for entry in self.entries():
            self.assertEqual(entry["solution"], "Setup_1 : LastAdaptive")
            self.assertEqual(entry["intrinsics"], {"Freq": "6.9003GHz", "Phase": "0deg"})
            self.assertIn(entry["expression"], (["Mag_E"], ["Mag_H"]))
            self.assertNotIn("sources", entry)

    def test_the_planes_sit_on_the_board_and_above_it(self):
        planes = self.by_name(self.entries())
        mid = planes["substrate_mid_E"]["grid"]
        self.assertEqual(mid["start"], [-25.0, 0.395, -12.5])
        self.assertEqual(mid["stop"], [25.0, 0.395, 12.5])
        self.assertEqual(mid["step"], [0.25, 1.0, 0.25])
        self.assertEqual(mid["units"], "mm")
        self.assertEqual(planes["substrate_top_H"]["grid"]["start"][1], 1.29)
        self.assertEqual(planes["cavity_mid_E"]["grid"]["start"][1], 4.895)
        self.assertEqual(planes["cavity_mid_E"]["grid"]["start"][0], -27.0)

    def test_the_section_runs_along_the_line_at_the_middle_of_the_width(self):
        section = self.by_name(self.entries())["section_xy_E"]["grid"]
        self.assertEqual(section["start"], [-27.5, -1.5, 0.0])
        self.assertEqual(section["stop"], [27.5, 9.5, 0.0])
        self.assertEqual(section["step"], [0.25, 0.25, 1.0])

    def test_the_volume_covers_the_whole_cavity_on_a_coarse_grid(self):
        volume = self.by_name(self.entries())["vacuum_volume_E"]["grid"]
        self.assertEqual(volume["start"], [-27.0, -1.5, -14.0])
        self.assertEqual(volume["stop"], [27.0, 9.0, 14.0])
        self.assertEqual(volume["step"], [1.0, 1.0, 1.0])

    def test_the_planes_have_about_twenty_thousand_points_on_the_board(self):
        def points(grid):
            count = 1
            for lo, hi, step in zip(grid["start"], grid["stop"], grid["step"]):
                count *= int(round((hi - lo) / step)) + 1
            return count
        self.assertEqual(points(self.by_name(self.entries())["substrate_mid_E"]["grid"]), 201 * 1 * 101)

    def test_the_sources_go_in_when_they_are_given(self):
        sources = {"port_1:1": ["1W", "0deg"], "port_2:1": ["0W", "0deg"]}
        for entry in self.entries(sources=sources):
            self.assertEqual(entry["sources"], sources)

    def test_a_box_that_is_not_one_is_refused(self):
        bad = {**BOARD, "substrate": [-25.0, 0.0, -12.5, -25.0, 0.79, 12.5]}
        with self.assertRaisesRegex(ValueError, "not a box"):
            self.entries(boxes=bad)

    def test_a_vacuum_that_does_not_reach_above_the_board_is_refused(self):
        low = {**BOARD, "vacuum": [-27.0, -1.5, -14.0, 27.0, 0.5, 14.0]}
        with self.assertRaisesRegex(ValueError, "does not reach above the board"):
            self.entries(boxes=low)

    def test_only_e_and_h_are_asked_for(self):
        with self.assertRaisesRegex(ValueError, "E and/or H"):
            self.entries(quantities=("X",))
        with self.assertRaisesRegex(ValueError, "E and/or H"):
            self.entries(quantities=())

    @NEEDS_FIELD_EXPORTS
    def test_the_task_the_entries_make_passes_the_workers_own_validation(self):
        task = queue_common.build_task(
            task_type="run_setups", project_file="projects/t/p.aedt", output_dir="projects/t",
            field_exports=self.entries(sources={"port_1:1": ["1W", "0deg"], "port_2:1": ["0W", "0deg"]}),
        )
        self.assertEqual(len(task["field_exports"]), 10)
        broken = self.entries()
        broken[0]["grid"]["step"] = [0.0, 1.0, 0.25]
        with self.assertRaisesRegex(Exception, "greater than zero"):
            queue_common.build_task(task_type="run_setups", project_file="p", output_dir="o",
                                    field_exports=broken)

    def test_sources_name_port_one_driven_and_the_others_quiet(self):
        sources, missing = fields_plan.sources_for(["port_1", "port_2"],
                                                   ["port_1:1", "port_2:1"])
        self.assertEqual(missing, [])
        self.assertEqual(sources, {"port_1:1": ["1W", "0deg"], "port_2:1": ["0W", "0deg"]})

    def test_sources_are_left_out_when_the_project_does_not_have_the_excitation(self):
        sources, missing = fields_plan.sources_for(["port_1", "port_2"], ["port_1:1"])
        self.assertEqual(sources, {})
        self.assertEqual(missing, ["port_2"])


class FakeHfss:
    """The little of an open HFSS project that the field grids read."""

    def __init__(self, units="mm", excitations=("port_1:1", "port_2:1")):
        self.modeler = types.SimpleNamespace(model_units=units)
        self.excitation_names = list(excitations)


def fake_object(name, box):
    return types.SimpleNamespace(name=name, bounding_box=list(box))


class PlanForModel(unittest.TestCase):
    """The boxes are read off the open model, in its own units."""

    def objects(self, scale=1.0):
        return {key: fake_object(name, [v * scale for v in BOARD[role]])
                for key, name, role in (("pcb", "pcb_substrate", "substrate"),
                                        ("vacuum", "vacuum", "vacuum"), ("box", "box", "box"))}

    def test_the_grids_follow_the_boxes_the_model_has(self):
        entries = fields_plan.plan_for_model(FakeHfss(), self.objects(), ["port_1", "port_2"],
                                             FREQUENCY, ["E", "H"], log=lambda _m: None)
        mid = {entry["name"]: entry for entry in entries}["substrate_mid_H"]
        self.assertEqual(mid["grid"]["start"], [-25.0, 0.395, -12.5])
        self.assertEqual(mid["sources"], {"port_1:1": ["1W", "0deg"], "port_2:1": ["0W", "0deg"]})

    def test_a_model_in_metres_is_converted_to_millimetres(self):
        model = FakeHfss(units="meter")
        entries = fields_plan.plan_for_model(model, self.objects(scale=0.001), [], FREQUENCY, ["E"],
                                             log=lambda _m: None)
        self.assertEqual({e["name"]: e for e in entries}["substrate_mid_E"]["grid"]["start"],
                         [-25.0, 0.395, -12.5])

    def test_a_model_without_its_board_is_refused(self):
        objects = self.objects()
        del objects["pcb"]
        with self.assertRaisesRegex(ValueError, "'pcb'"):
            fields_plan.plan_for_model(FakeHfss(), objects, [], FREQUENCY, ["E"], log=lambda _m: None)

    def test_a_model_in_units_that_do_not_convert_is_refused(self):
        with self.assertRaisesRegex(ValueError, "cannot be converted"):
            fields_plan.plan_for_model(FakeHfss(units="furlong"), self.objects(), [], FREQUENCY, ["E"],
                                       log=lambda _m: None)


class FieldSetup(unittest.TestCase):
    """The setup of a field image part: one adaptive solution, no sweep."""

    class Setup:
        def __init__(self):
            self.props = {}

        def update(self):
            pass

    class Project:
        def __init__(self):
            self.setups, self.sweeps = [], []

        def create_setup(self, name):
            setup = FieldSetup.Setup()
            self.setups.append(name)
            return setup

        def create_linear_count_sweep(self, **kwargs):
            self.sweeps.append(kwargs)
            return True

    SETTINGS = {"max_delta_s": 0.02, "max_passes": 12, "min_passes": 3, "min_converged": 2}

    def test_one_adaptive_pass_at_the_frequency_and_no_sweep(self):
        project = self.Project()
        setup = driven_setup.create_setup(project, "", {**self.SETTINGS, "field_frequency_ghz": FREQUENCY},
                                          log=lambda _m: None)
        self.assertEqual(project.setups, ["Setup_1"])
        self.assertEqual(project.sweeps, [])
        self.assertEqual(setup.props["Frequency"], "6.9003GHz")
        self.assertEqual(setup.props["MaximumPasses"], 12)
        self.assertEqual(setup.props["MinimumConvergedPasses"], 2)

    def test_the_frequency_text_is_the_one_the_export_names(self):
        self.assertEqual(driven_setup.ghz_text(FREQUENCY), "6.9003GHz")
        self.assertEqual(driven_setup.ghz_text(7), "7.0GHz")


class FieldParts(RunsRoot):
    """A field image part made from a finished driven run, and what it is."""

    def setUp(self):
        super().setUp()
        self.parent = self.driven_run()
        self.combos = list(self.parent.state["combos"])

    def make(self, combos=None, frequency=str(FREQUENCY), quantities=("E", "H")):
        chosen = self.combos[:1] if combos is None else combos
        return companion.create_field_images(self.parent, frequency, chosen, list(quantities))

    def test_the_part_solves_one_frequency_with_the_runs_own_settings(self):
        child = self.make()
        settings = child.analysis_settings
        self.assertEqual(settings["field_frequency_ghz"], FREQUENCY)
        self.assertEqual(settings["field_quantities"], ["E", "H"])
        self.assertEqual(settings["coarse_sweep"], "off")
        self.assertEqual(settings["fine_windows_manual"], "")
        self.assertEqual((settings["f_start_ghz"], settings["f_stop_ghz"]), (FREQUENCY, FREQUENCY))
        self.assertEqual(settings["adapt_ghz"], FREQUENCY)
        self.assertEqual(settings["max_passes"], self.parent.analysis_settings["max_passes"])
        self.assertEqual(list(child.state["combos"]), self.combos[:1])

    def test_it_is_a_hidden_part_of_its_run_and_is_linked_both_ways(self):
        child = self.make()
        self.assertEqual(child.parent_id, self.parent.run_id)
        self.assertEqual(child.config["part_role"], "field1")
        reloaded = Run.load(self.parent.run_id)
        self.assertEqual([c.run_id for c in companion.field_image_parts(reloaded)], [child.run_id])
        self.assertEqual(companion.field_parent(Run.load(child.run_id)).run_id, self.parent.run_id)
        self.assertIn(child.run_id, [p.run_id for p in companion.parts_of(reloaded)])
        self.assertEqual(part_kind(child.config), "field")
        self.assertEqual(part_record(child.run_id, child.config, child.state)["frequency_ghz"], FREQUENCY)

    def test_a_second_part_is_numbered_after_the_first(self):
        self.make()
        second = self.make(frequency="6.5")
        self.assertEqual(second.config["part_role"], "field2")
        self.assertEqual(len(companion.field_image_parts(Run.load(self.parent.run_id))), 2)

    def test_what_cannot_be_drawn_says_why(self):
        cases = (
            (dict(frequency="0.5"), "inside the run's band"),
            (dict(frequency="soon"), "as a number"),
            (dict(quantities=()), "Tick at least one"),
            (dict(quantities=("X",)), "Tick at least one"),
            (dict(combos=[]), "Pick at least one"),
            (dict(combos=["nope"]), "no results yet"),
        )
        for kwargs, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.make(**kwargs)

    def test_only_a_driven_run_has_a_field_to_draw(self):
        eigen = self.driven_run(solution="Eigenmode")
        with self.assertRaisesRegex(ValueError, "Driven Modal"):
            companion.create_field_images(eigen, str(FREQUENCY), list(eigen.state["combos"]), ["E"])


class WorkerOutput(RunsRoot):
    """What the part makes of the worker's answer, and of the fields it sent back."""

    def setUp(self):
        super().setUp()
        self.parent = self.driven_run()
        self.combo = list(self.parent.state["combos"])[0]
        self.child = companion.create_field_images(self.parent, str(FREQUENCY), [self.combo], ["E", "H"])

    def combo_dir(self):
        return self.child.combo_dir(self.combo)

    def test_the_combination_is_done_once_the_worker_has_answered(self):
        worker_output(self.combo_dir(), {
            "substrate_mid_E": {"ok": True, "rows": plane_rows()},
            "substrate_mid_H": {"ok": False, "error": "Failed to execute gRPC AEDT command: ExportOnGrid",
                                "aedt_messages": ["[error] Script macro error"]},
        })
        field_images.record_outcome(self.child, self.combo, self.combo_dir())
        self.assertEqual(self.child.state["combos"][self.combo]["post_status"], "done")

    def test_no_answer_is_a_failed_combination(self):
        field_images.record_outcome(self.child, self.combo, self.combo_dir())
        combo = self.child.state["combos"][self.combo]
        self.assertEqual(combo["post_status"], "failed")
        self.assertIn("no readable result.json", combo["post_error"])

    def test_an_older_worker_without_field_exports_is_a_failure_that_says_so(self):
        worker_output(self.combo_dir(), {}, with_block=False)
        outcome = field_images.outcome_of(self.combo_dir())
        self.assertIn("older than version 0.2.0", outcome["problem"])

    def test_a_task_the_worker_did_not_report_as_done_is_a_failure(self):
        worker_output(self.combo_dir(), {}, status="failed")
        self.assertIn("did not report success", field_images.outcome_of(self.combo_dir())["problem"])

    @NEEDS_MATPLOTLIB
    def test_the_pictures_are_drawn_and_summarised_for_the_run_page(self):
        worker_output(self.combo_dir(), {
            "substrate_mid_E": {"ok": True, "rows": plane_rows()},
            "substrate_mid_H": {"ok": False, "error": "Failed to execute gRPC AEDT command: ExportOnGrid",
                                "aedt_messages": ["[error] Script macro error"]},
        })
        field_images.record_outcome(self.child, self.combo, self.combo_dir())
        outcome = field_images.draw_run(self.child, log=lambda _m: None)
        self.assertEqual(outcome["drawn"], 1)
        self.assertIsNone(outcome["first_error"])
        png = os.path.join(field_images.images_dir(self.child.run_dir), f"{self.combo}__substrate_mid_E.png")
        self.assertTrue(os.path.isfile(png))
        summary = field_images.read_summary(self.child.run_dir)
        exports = {e["name"]: e for e in summary["combos"][self.combo]["exports"]}
        self.assertEqual(exports["substrate_mid_E"]["image"], f"{self.combo}__substrate_mid_E.png")
        self.assertEqual(exports["substrate_mid_E"]["place"], "middle of the board")
        self.assertEqual(exports["substrate_mid_H"]["error"], "Failed to execute gRPC AEDT command: ExportOnGrid")
        self.assertEqual(exports["substrate_mid_H"]["aedt_messages"], ["[error] Script macro error"])
        self.assertEqual(field_images.verdict(self.child.run_dir), "warning")

    @NEEDS_MATPLOTLIB
    def test_a_part_whose_exports_all_failed_reads_as_failed(self):
        worker_output(self.combo_dir(), {
            "substrate_mid_E": {"ok": False, "error": "edit_sources failed"},
            "section_xy_E": {"ok": False, "error": "edit_sources failed"},
        })
        field_images.record_outcome(self.child, self.combo, self.combo_dir())
        outcome = field_images.draw_run(self.child, log=lambda _m: None)
        self.assertEqual(outcome["drawn"], 0)
        self.assertEqual(outcome["first_error"], "edit_sources failed")
        self.assertEqual(field_images.verdict(self.child.run_dir), "failed")

    @NEEDS_MATPLOTLIB
    def test_a_part_whose_exports_all_came_out_reads_as_ok(self):
        worker_output(self.combo_dir(), {"substrate_mid_E": {"ok": True, "rows": plane_rows()}})
        field_images.record_outcome(self.child, self.combo, self.combo_dir())
        field_images.draw_run(self.child, log=lambda _m: None)
        self.assertEqual(field_images.verdict(self.child.run_dir), "ok")

    def test_before_the_pictures_are_drawn_the_verdict_is_unknown(self):
        self.assertEqual(field_images.verdict(self.child.run_dir), "unknown")

    def test_the_copy_back_keeps_the_fields_folder_and_the_result(self):
        source = tempfile.mkdtemp(prefix="remote_")
        self.addCleanup(shutil.rmtree, source, True)
        worker_output(source, {"substrate_mid_E": {"ok": True, "rows": plane_rows()}})
        destination = tempfile.mkdtemp(prefix="local_")
        self.addCleanup(shutil.rmtree, destination, True)
        self.assertEqual(postprocess.copy_tree_verified(source, destination), [])
        self.assertTrue(os.path.isfile(os.path.join(destination, "fields", "substrate_mid_E.fld")))
        self.assertTrue(os.path.isfile(os.path.join(destination, "result.json")))

    def test_a_field_part_is_not_read_out_of_its_project(self):
        self.child.update_combo(self.combo, queue_status="done", task_id="driven_task")
        worker_output(self.combo_dir(), {"substrate_mid_E": {"ok": True, "rows": plane_rows()}})
        task = {"task_id": "driven_task", "project_file": "projects/driven_task/p.aedt",
                "output_dir": "projects/driven_task", "objects": {}, "post_processing": []}
        with mock.patch.object(postprocess.queue_common, "read_task", return_value=task), \
                mock.patch.object(postprocess.queue_common, "resolve_path", return_value=self.combo_dir()), \
                mock.patch.object(postprocess, "bring_task_local", return_value=self.combo_dir()), \
                mock.patch.object(postprocess, "postprocess_task",
                                  side_effect=AssertionError("no project may be opened for a field part")):
            handled = postprocess.postprocess_run(self.child, None, self.child.analysis_settings,
                                                  log=lambda _m: None)
        self.assertEqual(handled, 1)
        self.assertEqual(self.child.state["combos"][self.combo]["post_status"], "done")

    @NEEDS_MATPLOTLIB
    def test_the_part_completes_when_an_export_came_out(self):
        self.child.state["stage"] = "postprocessing"
        worker_output(self.combo_dir(), {"substrate_mid_E": {"ok": True, "rows": plane_rows()},
                                         "section_xy_E": {"ok": False, "error": "edit_sources failed"}})
        field_images.record_outcome(self.child, self.combo, self.combo_dir())
        pipeline._finish_field_part(self.child, log=lambda _m: None)
        self.assertEqual(Run.load(self.child.run_id).state["stage"], "complete")

    @NEEDS_MATPLOTLIB
    def test_the_part_fails_with_the_workers_error_when_no_export_came_out(self):
        worker_output(self.combo_dir(), {"substrate_mid_E": {"ok": False, "error": "edit_sources failed"}})
        field_images.record_outcome(self.child, self.combo, self.combo_dir())
        pipeline._finish_field_part(self.child, log=lambda _m: None)
        state = Run.load(self.child.run_id).state
        self.assertEqual(state["stage"], "error")
        self.assertEqual(state["error"], "edit_sources failed")

    def test_bringing_the_task_back_keeps_its_fields_and_its_result_in_the_part(self):
        root = tempfile.mkdtemp(prefix="queue_")
        self.addCleanup(shutil.rmtree, root, True)
        queue = queue_common.QueuePaths(root)
        task_id = "driven_20261030_000000_000001"
        remote = os.path.join(root, "projects", task_id)
        worker_output(remote, {"substrate_mid_E": {"ok": True, "rows": plane_rows()},
                               "section_xy_H": {"ok": False, "error": "edit_sources failed"}})
        local = postprocess.bring_task_local(self.child, queue, self.combo, task_id, remote,
                                             lambda _m: None)
        self.assertEqual(local, self.child.combo_path(self.combo))
        self.assertTrue(os.path.isfile(os.path.join(local, "fields", "substrate_mid_E.fld")))
        self.assertTrue(os.path.isfile(os.path.join(local, "result.json")))
        self.assertIsNone(field_images.outcome_of(local)["problem"])
        self.assertFalse(os.path.exists(remote), "the queue copy is cleared once the local one is good")

    def test_the_part_is_started_from_its_parents_geometry(self):
        with mock.patch.object(companion, "seed_geometry", return_value=0) as seed:
            self.assertTrue(pipeline._finish_prerequisite(Run.load(self.child.run_id), None,
                                                          log=lambda _m: None))
        seed.assert_called_once()
        self.assertEqual(seed.call_args[0][1].run_id, self.parent.run_id)


class FieldPartPages(RunsRoot):
    """The run list and the run page, over a parent with field image parts in each state."""

    def setUp(self):
        super().setUp()
        self.parent = self.driven_run()
        self.combo = list(self.parent.state["combos"])[0]
        self.client = webapp.app.test_client()

    def part(self, frequency=FREQUENCY, stage="created", **combo_fields):
        child = companion.create_field_images(self.parent, str(frequency), [self.combo], ["E", "H"])
        child.state["stage"] = stage
        child.update_combo(self.combo, **combo_fields)
        return child

    def going(self):
        return self.part(stage="postprocessing", model_status="built", handoff_status="done",
                         queue_status="in_progress")

    def page(self):
        return self.client.get(f"/runs/{self.parent.run_id}").get_data(as_text=True)

    def run_list(self):
        return self.client.get("/").get_data(as_text=True)

    def test_a_part_that_is_going_is_a_row_under_its_run_with_its_own_words(self):
        child = self.going()
        html = self.run_list()
        self.assertIn(f'data-part-id="{child.run_id}"', html)
        self.assertIn("↳ field image at 6.9003 GHz", html)
        self.assertIn(">field image solving on the worker</span>", html)

    def test_the_page_section_is_open_while_a_part_is_going(self):
        self.going()
        html = self.page()
        self.assertIn('id="field-images" open', html)
        self.assertIn("Field images at one frequency", html)
        self.assertIn(f'action="/runs/{self.parent.run_id}/field-images"', html)
        self.assertIn('name="quantity" value="E" checked', html)
        self.assertIn('name="quantity" value="H" checked', html)

    def test_a_finished_part_is_a_note_on_its_run_and_its_section_stays_closed_when_old(self):
        child = self.part(stage="complete", model_status="built", handoff_status="done",
                          queue_status="done", fetch_status="done", post_status="done")
        old = time.time() - 10 * 24 * 60 * 60
        os.utime(child.state_path, (old, old))
        html = self.run_list()
        self.assertNotIn(f'data-part-id="{child.run_id}"', html)
        self.assertIn('class="part-note field-note"', html)
        self.assertIn("+1 field image", html)
        self.assertNotIn('id="field-images" open', self.page())

    def test_a_part_that_failed_just_now_is_open_on_the_page_with_the_reason(self):
        child = self.part(stage="error")
        child.state["error"] = "every field export failed"
        child.save_state()
        html = self.page()
        self.assertIn('id="field-images" open', html)
        self.assertIn("every field export failed", html)
        self.assertIn(f'data-part-id="{child.run_id}"', self.run_list())

    @NEEDS_MATPLOTLIB
    def test_the_drawn_pictures_and_the_workers_errors_are_on_the_page(self):
        child = self.part(stage="complete", model_status="built", handoff_status="done",
                          queue_status="done", fetch_status="done", post_status="done")
        worker_output(child.combo_dir(self.combo), {
            "substrate_mid_E": {"ok": True, "rows": plane_rows()},
            "section_xy_H": {"ok": False, "error": "Failed to execute gRPC AEDT command: ExportOnGrid",
                             "aedt_messages": ["[error] Script macro error"]},
        })
        field_images.record_outcome(child, self.combo, child.combo_dir(self.combo))
        field_images.draw_run(child, log=lambda _m: None)
        html = self.page()
        self.assertIn(f'src="/runs/{child.run_id}/field-images/{self.combo}__substrate_mid_E.png"', html)
        self.assertIn("the worker could not export it. Failed to execute gRPC AEDT command: ExportOnGrid", html)
        self.assertIn("[error] Script macro error", html)
        response = self.client.get(f"/runs/{child.run_id}/field-images/{self.combo}__substrate_mid_E.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/png")
        response.close()

    def test_a_bad_frequency_is_sent_back_to_the_section_with_the_reason(self):
        response = self.client.post(f"/runs/{self.parent.run_id}/field-images", data={
            "frequency": "0.5", "combo": [self.combo], "quantity": ["E"]})
        self.assertEqual(response.status_code, 302)
        self.assertIn("#field-images", response.headers["Location"])
        self.assertIn("band", response.headers["Location"])
        self.assertEqual(companion.field_image_parts(Run.load(self.parent.run_id)), [])

    def test_posting_the_form_makes_the_part_and_starts_it(self):
        with mock.patch.object(webapp, "_start_pipeline_thread") as start:
            response = self.client.post(f"/runs/{self.parent.run_id}/field-images", data={
                "frequency": "6.9003", "combo": [self.combo], "quantity": ["E", "H"]})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("#field-images"))
        parts = companion.field_image_parts(Run.load(self.parent.run_id))
        self.assertEqual(len(parts), 1)
        start.assert_called_once()
        self.assertEqual(start.call_args[0][0].run_id, parts[0].run_id)

    def test_a_part_is_listed_on_the_run_page_as_part_of_it_not_as_a_run(self):
        child = self.going()
        listed = {entry["run_id"] for entry in self.client.get("/runs.json").get_json()}
        self.assertIn(self.parent.run_id, listed)
        self.assertNotIn(child.run_id, listed)
        redirect = self.client.get(f"/runs/{child.run_id}")
        self.assertEqual(redirect.status_code, 302)
        self.assertIn(self.parent.run_id, redirect.headers["Location"])


if __name__ == "__main__":
    unittest.main()
