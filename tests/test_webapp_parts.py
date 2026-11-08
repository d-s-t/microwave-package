"""The routes behind the 3D view: a combination's STEP files and its parts.json.

What the view draws is covered in test_viewer_parts. These check what the
routes serve: each part's own file, the board worked out from dimensions.txt,
and what an old run with some of its files missing gets instead.

Run: python -m pytest tests/test_webapp_parts.py
"""
import logging
import os
import tempfile
import unittest
from unittest import mock

from microwave_package import paths, pipeline, webapp
from microwave_package.design.sweep import SweepParameter
from microwave_package.workflow.run_manager import Run

# The equations a combination's dimensions.txt carries, as the model reads
# them: a numeric line per value, and formulas (ignored by the model's own
# parser for the purpose of numbers) such as floor_width.
DIMENSIONS = """"pcb_length"= 50.0
"pcb_side"= 25
"pcb_width"= 0.381
"wall_width"= 1
"chip_length"= 6.3
"chip_side"= 6.3
"chip_width"= 0.381
"cavity_clearance"= 0.1
"pedestal_depth"= 0.5
"screws_on"= 1.0
"screw_engagement"= 3
"copper_thickness"= 0.035
"chip_metal_thickness"= 0.0002
"pcb_corner_radius"= 1.0
"cavity_corner_radius"= 1.0
"no_chip"= 0.0
"floor_width" = IIF ( "screws_on" > 0.5 , 2.5 , 1 )
"""


class PartsCase(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)
        self.run = Run.create([SweepParameter(name="pedestal_depth", values=[0.0])],
                              dict(pipeline.DEFAULT_ANALYSIS_SETTINGS), {})
        self.combo = next(iter(self.run.state["combos"]))
        self.folder = self.run.combo_dir(self.combo)
        self.base = f"/runs/{self.run.run_id}/combos/{self.combo}"
        self.client = webapp.app.test_client()

    def write(self, name, text):
        path = os.path.join(self.folder, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def build(self, box=True, lid=True, dimensions=True):
        """Write one combination's files the way a finished build leaves them,
        and record them in its state."""
        paths_by_part = {}
        if box:
            paths_by_part["box"] = self.write(f"{self.combo}_box.STEP", "ISO-10303-21; BOX")
        if lid:
            paths_by_part["lid_inside"] = self.write(f"{self.combo}_lid_inside.STEP",
                                                     "ISO-10303-21; LID")
        if dimensions:
            self.write("dimensions.txt", DIMENSIONS)
        self.run.update_combo(self.combo, step_status="done", step_paths=paths_by_part)


class TestPartsJson(PartsCase):
    def test_a_finished_combination_lists_the_box_the_lid_and_the_board(self):
        self.build()
        reply = self.client.get(self.base + "/parts.json")
        self.assertEqual(reply.status_code, 200)
        data = reply.get_json()
        self.assertTrue(data["box_url"].endswith("/step"))
        self.assertTrue(data["lid_url"].endswith("/step?part=lid_inside"))
        self.assertEqual(data["notes"], [])
        board = data["board"]
        # The board sits on the floor and the pedestal (2.5 + 0.5), and its
        # opening is the die's 6.3 mm plus the 0.1 mm clearance a side.
        self.assertAlmostEqual(board["pcb"]["bottom"], 3.0)
        self.assertAlmostEqual(board["lid_bottom"], 3.416)
        self.assertAlmostEqual(board["pcb"]["opening"][0][0], 3.25)
        self.assertIsNone(board["pcb"]["floor_top"])
        self.assertAlmostEqual(board["die"]["top"], board["pcb"]["top"])

    def test_the_files_it_points_at_are_served(self):
        self.build()
        data = self.client.get(self.base + "/parts.json").get_json()
        box = self.client.get(data["box_url"])
        lid = self.client.get(data["lid_url"])
        self.assertEqual(box.get_data(), b"ISO-10303-21; BOX")
        self.assertEqual(lid.get_data(), b"ISO-10303-21; LID")
        self.assertEqual(lid.mimetype, "model/step")

    def test_a_run_with_no_lid_or_dimensions_still_gives_the_box(self):
        self.build(lid=False, dimensions=False)
        reply = self.client.get(self.base + "/parts.json")
        self.assertEqual(reply.status_code, 200)
        data = reply.get_json()
        self.assertIsNotNone(data["box_url"])
        self.assertIsNone(data["lid_url"])
        self.assertIsNone(data["board"])
        self.assertTrue(any("No dimensions.txt" in note for note in data["notes"]))

    def test_a_run_with_no_box_file_still_gives_the_board(self):
        self.build(box=False)
        data = self.client.get(self.base + "/parts.json").get_json()
        self.assertIsNone(data["box_url"])
        self.assertIsNotNone(data["lid_url"])
        self.assertIsNotNone(data["board"])

    def test_a_run_exported_as_one_assembly_is_shown_alone(self):
        # Before the parts were split the combination had one STEP for the
        # whole assembly, which may already hold the board and the die.
        self.write(f"{self.combo}.STEP", "ISO-10303-21; ASSEMBLY")
        self.write("dimensions.txt", DIMENSIONS)
        self.run.update_combo(self.combo, step_status="done", step_paths=None)
        data = self.client.get(self.base + "/parts.json").get_json()
        self.assertIsNotNone(data["box_url"])
        self.assertIsNone(data["lid_url"])
        self.assertIsNone(data["board"])
        self.assertTrue(any("predates the split" in note for note in data["notes"]))
        self.assertEqual(self.client.get(data["box_url"]).get_data(), b"ISO-10303-21; ASSEMBLY")

    def test_a_run_moved_to_another_disk_finds_its_files_in_its_own_folder(self):
        self.build()
        gone = os.path.join(tempfile.gettempdir(), "no_such_disk", f"{self.combo}_box.STEP")
        self.run.update_combo(self.combo, step_paths={"box": gone, "lid_inside": gone})
        data = self.client.get(self.base + "/parts.json").get_json()
        self.assertEqual(self.client.get(data["box_url"]).get_data(), b"ISO-10303-21; BOX")
        self.assertEqual(self.client.get(data["lid_url"]).get_data(), b"ISO-10303-21; LID")

    def test_an_unknown_run_or_combination_is_a_json_404(self):
        reply = self.client.get("/runs/no_such_run/combos/x/parts.json")
        self.assertEqual(reply.status_code, 404)
        self.assertEqual(reply.get_json()["error"], "run not found")
        reply = self.client.get(f"/runs/{self.run.run_id}/combos/no_such_combo/parts.json")
        self.assertEqual(reply.status_code, 404)
        self.assertEqual(reply.get_json()["error"], "unknown combination")


class TestStepFiles(PartsCase):
    def test_the_box_is_the_default_part(self):
        self.build()
        self.assertEqual(self.client.get(self.base + "/step").get_data(), b"ISO-10303-21; BOX")

    def test_the_box_route_never_serves_the_lid_file(self):
        # With the box file gone, the box must not fall back to the lid's file.
        self.build(box=False)
        self.assertEqual(self.client.get(self.base + "/step").status_code, 404)

    def test_a_missing_lid_is_a_404(self):
        self.build(lid=False)
        self.assertEqual(self.client.get(self.base + "/step?part=lid_inside").status_code, 404)

    def test_an_unknown_part_is_refused(self):
        self.build()
        self.assertEqual(self.client.get(self.base + "/step?part=../state").status_code, 404)
        self.assertEqual(self.client.get(self.base + "/step?part=chip").status_code, 404)


class TestCombinationWindow(PartsCase):
    def test_the_combination_window_is_given_the_parts_url_when_the_viewer_is_there(self):
        self.build()
        with mock.patch.object(webapp, "_step_viewer_available", lambda: True):
            data = self.client.get(self.base + "/details.json").get_json()
        self.assertTrue(data["actions"]["parts_url"].endswith("/parts.json"))
        self.assertNotIn("step_url", data["actions"])


if __name__ == "__main__":
    unittest.main()
