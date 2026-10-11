"""Manufacturing files: the catalog and what exists on disk, the file
writers that need no program (PDF, Gerber, Excellon, zips), and the web
routes that list, generate and serve them. No SolidWorks and no Altium:
the producers that drive them are replaced.

Run: python -m unittest discover -s tests
"""
import io
import logging
import os
import re
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock

from microwave_package import paths, pipeline, webapp
from microwave_package.design import fab_drawing, gerber
from microwave_package.design.sweep import SweepParameter
from microwave_package.workflow import manufacturing, simple_pdf
from microwave_package.workflow.run_manager import Run

COPPER = """# copper geometry
board=package
pad|top|18|0|0.48|1.4|90
pad|top|-18|0|0.48|1.4|270
track|top|-18|0|18|0|0.2
via|1|2|0.3|0.15
via|1|-2|0.3|0.15
via|3|2|0.4|0.2
region|bottom|-25,-12.5,25,-12.5,25,12.5,-25,12.5
region|top|-3,-3,3,-3,3,3,-3,3
board=chip
pad|top|0|0|0.1|0.1|0
via|9|9|0.3|0.15
"""

VALUES = {"pcb_length": 50.0, "pcb_side": 25.0, "pcb_width": 0.381, "copper_thickness": 0.035,
          "cpw_trace_width": 0.2, "no_chip": 1.0, "screws_on": 1.0, "screw_inset": 3.5,
          "screw_clearance_diameter": 2.3}


class TestPdf(unittest.TestCase):
    def test_document_is_a_well_formed_pdf(self):
        doc = simple_pdf.Document("t (1)")
        page = doc.add_page(simple_pdf.A4)
        page.text(10, 10, "Hello (world) Ø ±", 10)
        page.line(0, 0, 10, 10)
        page.circle(5, 5, 2)
        data = doc.to_bytes()
        self.assertTrue(data.startswith(b"%PDF-1.4"))
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))
        startxref = int(re.search(rb"startxref\n(\d+)", data).group(1))
        self.assertTrue(data[startxref:].startswith(b"xref"))
        self.assertEqual(data.count(b"/Type /Page "), 1)

    def test_text_width_grows_with_size_and_length(self):
        self.assertGreater(simple_pdf.text_width("MMMM", 10), simple_pdf.text_width("iiii", 10))
        self.assertAlmostEqual(simple_pdf.text_width("abc", 20), 2 * simple_pdf.text_width("abc", 10))


class TestFabDrawing(unittest.TestCase):
    def setUp(self):
        self.copper = fab_drawing.parse_package_copper(COPPER)

    def test_only_the_package_board_is_read(self):
        self.assertEqual(len(self.copper["via"]), 3)
        self.assertEqual(len(self.copper["pad"]), 2)
        self.assertEqual(len(self.copper["track"]), 1)

    def test_drill_table_separates_plated_vias_from_screw_holes(self):
        rows = fab_drawing.drill_table(VALUES, self.copper)
        plated = {r["diameter"]: r["count"] for r in rows if r["plated"]}
        self.assertEqual(plated, {0.15: 2, 0.2: 1})
        unplated = [r for r in rows if not r["plated"]]
        self.assertEqual([(r["diameter"], r["count"]) for r in unplated], [(2.3, 4)])

    def test_no_screws_no_unplated_holes(self):
        rows = fab_drawing.drill_table({**VALUES, "screws_on": 0.0}, self.copper)
        self.assertFalse([r for r in rows if not r["plated"]])

    def test_stack_names_the_material_and_thickness(self):
        rows = fab_drawing.stack_rows(VALUES, "Rogers TMM 10i (tm)")
        self.assertEqual(len(rows), 3)
        self.assertIn("Rogers TMM 10i (tm)", rows[1][0])
        self.assertIn("381 um", rows[1][1])
        self.assertIn("35 um", rows[0][1])

    def test_sheet_is_legible(self):
        doc = fab_drawing.build(VALUES, "Rogers TMM 10i (tm)", self.copper, "run_1", "default")
        page = doc.pages[0]
        sizes = [float(s) for s in re.findall(r"/F\d ([\d.]+) Tf", "\n".join(page._ops))]
        self.assertTrue(sizes)
        self.assertGreaterEqual(min(sizes), fab_drawing.MIN_TEXT_PT - 0.01)
        self.assertEqual((page.width, page.height), simple_pdf.A3_LANDSCAPE)
        data = doc.to_bytes()
        self.assertTrue(data.startswith(b"%PDF"))

    def test_all_text_stays_on_the_sheet(self):
        doc = fab_drawing.build(VALUES, "Rogers TMM 10i (tm)", self.copper, "run_1", "default")
        page = doc.pages[0]
        pt = simple_pdf.PT_PER_MM
        for op in page._ops:
            m = re.match(r"BT /F\d ([\d.]+) Tf ([\d.\-]+) ([\d.\-]+) Td", op)
            if m:
                self.assertTrue(0 <= float(m.group(2)) / pt <= page.width, op)
                self.assertTrue(0 <= float(m.group(3)) / pt <= page.height, op)


class TestGerber(unittest.TestCase):
    def setUp(self):
        self.copper = fab_drawing.parse_package_copper(COPPER)

    def test_set_has_copper_outline_and_both_drill_files(self):
        files = gerber.fab_set(VALUES, self.copper, "x")
        self.assertEqual(sorted(files), ["x_B_Cu.gbr", "x_Edge_Cuts.gbr", "x_F_Cu.gbr",
                                         "x_NPTH.drl", "x_PTH.drl"])

    def test_top_copper_has_pads_track_region_and_via_lands(self):
        text = gerber.copper_layer(self.copper, "top", "x")
        self.assertIn("%FSLAX46Y46*%", text)
        self.assertIn("%MOMM*%", text)
        self.assertIn("G36*", text)                    # the top region
        self.assertIn("%ADD", text)
        self.assertEqual(text.count("D03*"), 2 + 3)    # two pads, three via lands
        self.assertTrue(text.rstrip().endswith("M02*"))

    def test_rotated_pad_swaps_its_aperture(self):
        text = gerber.copper_layer(self.copper, "top", "x")
        self.assertIn("R,1.4X0.48", text)

    def test_bottom_copper_has_its_region_and_via_lands_only(self):
        text = gerber.copper_layer(self.copper, "bottom", "x")
        self.assertEqual(text.count("G36*"), 1)
        self.assertEqual(text.count("D03*"), 3)

    def test_outline_is_the_board_rectangle(self):
        text = gerber.outline_layer(VALUES, "x")
        self.assertIn("X-25000000Y-12500000D02*", text)
        self.assertIn("X25000000Y12500000D01*", text)

    def test_drill_files_split_plated_from_not_plated(self):
        files = gerber.drill_files(VALUES, self.copper)
        self.assertIn("T1C0.150", files["PTH"])
        self.assertIn("T2C0.200", files["PTH"])
        self.assertEqual(files["PTH"].count("\nX"), 3)
        self.assertIn("T1C2.300", files["NPTH"])
        self.assertEqual(files["NPTH"].count("\nX"), 4)
        self.assertIn("X21.5000Y9.0000", files["NPTH"])    # 25-3.5, 12.5-3.5
        self.assertTrue(files["PTH"].rstrip().endswith("M30"))

    def test_no_screws_means_no_npth_file(self):
        self.assertNotIn("NPTH", gerber.drill_files({**VALUES, "screws_on": 0.0}, self.copper))


class ManufacturingCase(unittest.TestCase):
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

    def build_geometry(self):
        with open(os.path.join(self.folder, "dimensions.txt"), "w", encoding="utf-8") as f:
            f.write('"pcb_length"= 50.0\n"pcb_side"= 25\n')
        box = os.path.join(self.folder, f"{self.combo}_box.STEP")
        lid = os.path.join(self.folder, f"{self.combo}_lid_inside.STEP")
        for path in (box, lid):
            with open(path, "w") as f:
                f.write("ISO-10303-21;")
        self.run.update_combo(self.combo, step_status="done",
                              step_paths={"box": box, "lid_inside": lid})


class TestListing(ManufacturingCase):
    def groups(self):
        return {g["group"]: g for g in manufacturing.listing(self.run, self.combo, manufacturing.Jobs())}

    def test_groups_are_box_lid_pcb_in_order(self):
        self.assertEqual([g["group"] for g in manufacturing.listing(self.run, self.combo)],
                         ["box", "lid", "pcb"])

    def test_every_catalog_file_is_listed_once_and_none_is_the_chip(self):
        keys = [f["key"] for g in manufacturing.listing(self.run, self.combo) for f in g["files"]]
        self.assertEqual(sorted(keys), sorted(s.key for s in manufacturing.CATALOG))
        self.assertFalse([k for k in keys if "chip" in k])

    def test_nothing_exists_before_the_geometry_is_built(self):
        groups = self.groups()
        self.assertTrue(all(not f["exists"] for g in groups.values() for f in g["files"]))
        self.assertFalse(groups["pcb"]["can_generate"])
        self.assertIn("not been built", groups["pcb"]["cannot_reason"])

    def test_the_runs_own_step_files_are_listed_with_their_size(self):
        self.build_geometry()
        groups = self.groups()
        steps = {f["key"]: f for g in groups.values() for f in g["files"] if f["key"].endswith("_step")}
        self.assertTrue(steps["box_step"]["exists"])
        self.assertEqual(steps["box_step"]["size"], len("ISO-10303-21;"))
        self.assertFalse(steps["pcb_step"]["exists"])
        self.assertEqual(groups["box"]["missing"], 3)
        self.assertTrue(groups["box"]["can_generate"])

    def test_a_generated_file_shows_up_with_its_size(self):
        self.build_geometry()
        path = manufacturing.target_path(self.run, self.combo, "pcb_fab_drawing")
        with open(path, "wb") as f:
            f.write(b"x" * 2048)
        entry = next(f for f in self.groups()["pcb"]["files"] if f["key"] == "pcb_fab_drawing")
        self.assertTrue(entry["exists"])
        self.assertEqual(entry["size_text"], "2.0 KB")

    def test_a_running_job_is_reported(self):
        self.build_geometry()
        jobs = manufacturing.Jobs()
        release = threading.Event()
        self.assertTrue(jobs.start(self.run.run_id, self.combo, "pcb", lambda log: release.wait(5)))
        self.assertFalse(jobs.start(self.run.run_id, self.combo, "pcb", lambda log: None))
        job = jobs.get(self.run.run_id, self.combo, "pcb")
        self.assertEqual(job["state"], "running")
        self.assertTrue(jobs.any_running())
        release.set()
        for _ in range(100):
            if jobs.get(self.run.run_id, self.combo, "pcb")["state"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual(jobs.get(self.run.run_id, self.combo, "pcb")["state"], "done")

    def test_a_failed_job_keeps_its_reason(self):
        jobs = manufacturing.Jobs()

        def boom(log):
            raise RuntimeError("Altium is on a dialog")
        jobs.start("r", "c", "pcb", boom)
        for _ in range(100):
            job = jobs.get("r", "c", "pcb")
            if job["state"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual(job["state"], "error")
        self.assertEqual(job["error"], "Altium is on a dialog")


class TestFiles(unittest.TestCase):
    def test_project_names_the_board(self):
        text = manufacturing.project_text("a_pcb.PcbDoc")
        self.assertIn("[Design]", text)
        self.assertIn("DocumentPath=a_pcb.PcbDoc", text)

    def test_write_zip_takes_paths_and_text_and_leaves_no_partial_file(self):
        folder = tempfile.mkdtemp()
        source = os.path.join(folder, "src.bin")
        with open(source, "wb") as f:
            f.write(b"\x00\x01")
        target = os.path.join(folder, "out.zip")
        manufacturing.write_zip(target, {"a/b.bin": source, "t.txt": "hello"})
        with zipfile.ZipFile(target) as z:
            self.assertEqual(sorted(z.namelist()), ["a/b.bin", "t.txt"])
            self.assertEqual(z.read("a/b.bin"), b"\x00\x01")
            self.assertEqual(z.read("t.txt"), b"hello")
        self.assertEqual(os.listdir(folder).count("out.zip.part"), 0)

    def test_every_spec_has_a_unique_suffix_and_a_mime_type(self):
        suffixes = [s.suffix for s in manufacturing.CATALOG]
        self.assertEqual(len(suffixes), len(set(suffixes)))
        self.assertTrue(all("/" in s.mime for s in manufacturing.CATALOG))

    def test_human_size(self):
        self.assertEqual(manufacturing.human_size(10), "10 B")
        self.assertEqual(manufacturing.human_size(1536), "1.5 KB")
        self.assertEqual(manufacturing.human_size(5 * 1024 * 1024), "5.0 MB")


class TestRoutes(ManufacturingCase):
    def setUp(self):
        super().setUp()
        self.client = webapp.app.test_client()
        self.base = f"/runs/{self.run.run_id}/combos/{self.combo}/manufacturing"

    def test_listing_json(self):
        self.build_geometry()
        reply = self.client.get(self.base + ".json")
        self.assertEqual(reply.status_code, 200)
        data = reply.get_json()
        self.assertEqual([g["group"] for g in data["groups"]], ["box", "lid", "pcb"])
        box_step = next(f for f in data["groups"][0]["files"] if f["key"] == "box_step")
        self.assertTrue(box_step["url"].endswith("/manufacturing/box_step"))
        sldprt = next(f for f in data["groups"][0]["files"] if f["key"] == "box_sldprt")
        self.assertIsNone(sldprt["url"])

    def test_unknown_run_and_combo_are_404(self):
        self.assertEqual(self.client.get("/runs/nope/combos/x/manufacturing.json").status_code, 404)
        self.assertEqual(
            self.client.get(f"/runs/{self.run.run_id}/combos/nope/manufacturing.json").status_code, 404)

    def test_download_serves_the_file_with_its_type_and_a_good_name(self):
        self.build_geometry()
        reply = self.client.get(self.base + "/box_step")
        self.addCleanup(reply.close)
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.mimetype, "model/step")
        self.assertIn("attachment", reply.headers["Content-Disposition"])
        self.assertIn(f"{self.run.run_id}__{self.combo}_box.STEP", reply.headers["Content-Disposition"])

    def test_download_types(self):
        self.build_geometry()
        for key, mime in (("pcb_fab_drawing", "application/pdf"), ("pcb_gerber", "application/zip"),
                          ("pcb_ipc2581", "application/xml"), ("box_sldprt", "application/octet-stream")):
            with open(manufacturing.target_path(self.run, self.combo, key), "wb") as f:
                f.write(b"data")
            reply = self.client.get(f"{self.base}/{key}")
            self.addCleanup(reply.close)
            self.assertEqual(reply.status_code, 200, key)
            self.assertEqual(reply.mimetype, mime, key)

    def test_a_file_not_made_yet_or_unknown_is_404(self):
        self.build_geometry()
        self.assertEqual(self.client.get(self.base + "/pcb_step").status_code, 404)
        self.assertEqual(self.client.get(self.base + "/passwd").status_code, 404)

    def test_generate_refuses_an_unknown_group(self):
        reply = self.client.post(self.base + "/generate", json={"group": "chip"})
        self.assertEqual(reply.status_code, 400)

    def test_generate_refuses_when_the_geometry_is_not_built(self):
        reply = self.client.post(self.base + "/generate", json={"group": "pcb"})
        self.assertEqual(reply.status_code, 409)
        self.assertIn("not been built", reply.get_json()["error"])

    def test_generate_refuses_while_a_run_is_using_the_programs(self):
        self.build_geometry()
        alive = mock.Mock()
        alive.is_alive.return_value = True
        with mock.patch.dict(webapp._active_threads, {"some_run": alive}):
            reply = self.client.post(self.base + "/generate", json={"group": "box"})
        self.assertEqual(reply.status_code, 409)
        self.assertIn("run is using", reply.get_json()["error"])

    def test_generate_starts_the_group_and_the_listing_shows_it_done(self):
        self.build_geometry()
        jobs = manufacturing.Jobs()
        made = []

        def fake(run, combo_id, group, log):
            log("working")
            made.append((run.run_id, combo_id, group))
            with open(manufacturing.target_path(run, combo_id, "pcb_fab_drawing"), "wb") as f:
                f.write(b"%PDF")

        with mock.patch.object(manufacturing, "JOBS", jobs), \
                mock.patch.object(manufacturing, "make_group", fake):
            reply = self.client.post(self.base + "/generate", json={"group": "pcb"})
            self.assertEqual(reply.status_code, 202)
            for _ in range(100):
                job = jobs.get(self.run.run_id, self.combo, "pcb")
                if job["state"] != "running":
                    break
                time.sleep(0.02)
            data = self.client.get(self.base + ".json").get_json()
        self.assertEqual(made, [(self.run.run_id, self.combo, "pcb")])
        pcb = next(g for g in data["groups"] if g["group"] == "pcb")
        self.assertEqual(pcb["job"]["state"], "done")
        self.assertTrue(next(f for f in pcb["files"] if f["key"] == "pcb_fab_drawing")["exists"])

    def test_a_second_generate_while_one_runs_is_refused(self):
        self.build_geometry()
        jobs = manufacturing.Jobs()
        release = threading.Event()
        with mock.patch.object(manufacturing, "JOBS", jobs), \
                mock.patch.object(manufacturing, "make_group", lambda *a: release.wait(5)):
            first = self.client.post(self.base + "/generate", json={"group": "box"})
            second = self.client.post(self.base + "/generate", json={"group": "box"})
            release.set()
        self.assertEqual((first.status_code, second.status_code), (202, 409))

    def test_run_page_has_the_panel_and_the_button(self):
        self.build_geometry()
        page = self.client.get(f"/runs/{self.run.run_id}").get_data(as_text=True)
        self.assertIn('id="mfg-window"', page)
        self.assertIn("mfg-open", page)


class TestAltiumOutputs(ManufacturingCase):
    """What the Output Job leaves behind, sorted into the named files."""

    def build_tree(self):
        from microwave_package.design import altium_generation  # noqa: F401
        work = tempfile.mkdtemp()
        root = os.path.join(work, "Project Outputs for export")
        files = {
            "ExportSTEP/package.step": b"ISO-10303-21;" + b"x" * 100,
            "Gerber/package.GTL": b"G04*", "Gerber/package.GBL": b"G04*",
            "Gerber/package.GTS": b"G04*",
            "NC Drill/package-Plated.TXT": b"M48", "NC Drill/package.DRR": b"report",
            "IPC2581/package.cvg": b"<IPC-2581 revision='B'/>",
            "ODB/odb/steps/package/layers/top/features": b"#", "ODB/odb/matrix/matrix": b"#",
        }
        for relative, data in files.items():
            path = os.path.join(root, *relative.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        return work

    def test_each_format_lands_in_its_own_named_file(self):
        from microwave_package.design import altium_generation
        out = tempfile.mkdtemp()
        results = altium_generation.collect_outputs(self.build_tree(), out, "n")
        self.assertEqual(results, {"step": "ok", "ipc2581": "ok", "odb": "ok", "fab": "ok"})
        self.assertEqual(sorted(os.listdir(out)), ["n_pcb.STEP", "n_pcb_altium_fab.zip",
                                                   "n_pcb_ipc2581.xml", "n_pcb_odb.zip"])
        with zipfile.ZipFile(os.path.join(out, "n_pcb_altium_fab.zip")) as z:
            self.assertEqual(sorted(z.namelist()), ["Gerber/package.GBL", "Gerber/package.GTL",
                                                    "Gerber/package.GTS", "NC Drill/package-Plated.TXT",
                                                    "NC Drill/package.DRR"])
        with zipfile.ZipFile(os.path.join(out, "n_pcb_odb.zip")) as z:
            self.assertIn("ODB/odb/matrix/matrix", z.namelist())

    def test_a_missing_format_is_reported_not_raised(self):
        import shutil
        from microwave_package.design import altium_generation
        work = self.build_tree()
        shutil.rmtree(os.path.join(work, "Project Outputs for export", "IPC2581"))
        out = tempfile.mkdtemp()
        results = altium_generation.collect_outputs(work, out, "n")
        self.assertEqual(results["ipc2581"], "not written")
        self.assertEqual(results["step"], "ok")
        self.assertNotIn("n_pcb_ipc2581.xml", os.listdir(out))

    def test_outline_and_screw_holes_are_added_to_the_altium_zip_with_their_origin_stated(self):
        self.build_geometry()
        zip_path = manufacturing.target_path(self.run, self.combo, "pcb_altium_fab")
        manufacturing.write_zip(zip_path, {"Gerber/package.GTL": "G04*"})
        manufacturing.add_outline_supplement(self.run, self.combo, VALUES, "n")
        manufacturing.add_outline_supplement(self.run, self.combo, VALUES, "n")   # idempotent
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            self.assertEqual(len(names), len(set(names)))
            self.assertIn("supplement_from_parameters/n_Edge_Cuts.gbr", names)
            self.assertIn("supplement_from_parameters/n_NPTH.drl", names)
            self.assertIn("not from the .PcbDoc", z.read("supplement_from_parameters/README.txt").decode())
            self.assertIn("Gerber/package.GTL", names)     # Altium's own files untouched

    def test_a_complete_altium_set_gets_no_supplement(self):
        self.build_geometry()
        zip_path = manufacturing.target_path(self.run, self.combo, "pcb_altium_fab")
        manufacturing.write_zip(zip_path, {
            "Gerber/package.GTL": "G04*", "Gerber/package.GM2": "G04*",
            "NC Drill/package-Plated.TXT": "M48", "NC Drill/package-NonPlated.TXT": "M48"})
        self.assertEqual(manufacturing.altium_fab_gaps(zip_path), {"outline": False, "npth": False})
        self.assertEqual(manufacturing.add_outline_supplement(self.run, self.combo, VALUES, "n"), [])
        with zipfile.ZipFile(zip_path) as z:
            self.assertFalse([n for n in z.namelist() if n.startswith(manufacturing.SUPPLEMENT_FOLDER)])

    def test_only_what_altium_lacks_is_supplemented(self):
        self.build_geometry()
        zip_path = manufacturing.target_path(self.run, self.combo, "pcb_altium_fab")
        manufacturing.write_zip(zip_path, {"Gerber/package.GTL": "G04*", "Gerber/package.GM2": "G04*",
                                           "NC Drill/package-Plated.TXT": "M48"})
        added = manufacturing.add_outline_supplement(self.run, self.combo, VALUES, "n")
        self.assertEqual(added, ["n_NPTH.drl"])
        with zipfile.ZipFile(zip_path) as z:
            note = z.read("supplement_from_parameters/README.txt").decode()
        self.assertIn("NPTH", note)
        self.assertNotIn("Edge_Cuts", note)

    def test_the_outjob_plots_the_outline_layer_the_generator_draws(self):
        from microwave_package.design import outjob
        self.assertEqual(outjob.LAYER_IDS["outline"], 16908288 + 2)      # Mechanical 2
        self.assertIn("16908290~1", outjob.text("C:\\out"))

    def test_the_generator_draws_the_outline_and_the_mount_holes(self):
        with open(os.path.join(os.path.dirname(paths.__file__), "..", "base_parts", "altium",
                               "GenerateFromParams.pas"), encoding="utf-8") as f:
            script = f.read()
        self.assertIn("AddMountHole", script)
        self.assertIn("Pd.Plated := False", script)
        self.assertIn("AddOutlineBox(Board, HalfL, HalfS)", script)
        self.assertIn("eMechanical2", script)

    def test_no_altium_zip_means_no_supplement(self):
        self.build_geometry()
        manufacturing.add_outline_supplement(self.run, self.combo, VALUES, "n")   # no exception


class TestProducers(ManufacturingCase):
    def test_pcb_producer_writes_the_python_files_and_asks_altium_for_the_rest(self):
        self.build_geometry()
        with open(os.path.join(self.folder, "package.PcbDoc"), "wb") as f:
            f.write(b"PCBDOC")
        with open(os.path.join(self.folder, "copper.txt"), "w", encoding="utf-8") as f:
            f.write(COPPER)
        calls = []
        from microwave_package.design import altium_generation
        with mock.patch.object(altium_generation, "export_manufacturing",
                               lambda *a, **k: calls.append(a) or {}):
            manufacturing.make_pcb(self.run, self.combo, lambda m: None)
        self.assertEqual(len(calls), 1)
        for key in ("pcb_project", "pcb_gerber", "pcb_drill", "pcb_fab_drawing"):
            self.assertIsNotNone(manufacturing.path_of(self.run, self.combo, key), key)
        with zipfile.ZipFile(manufacturing.path_of(self.run, self.combo, "pcb_project")) as z:
            names = z.namelist()
        self.assertTrue(any(n.endswith(".PcbDoc") for n in names))
        self.assertTrue(any(n.endswith(".PrjPcb") for n in names))
        with open(manufacturing.path_of(self.run, self.combo, "pcb_fab_drawing"), "rb") as f:
            self.assertTrue(f.read(8).startswith(b"%PDF"))

    def test_cad_producer_only_asks_for_what_is_missing(self):
        self.build_geometry()
        from microwave_package.design import solidworks_manufacturing
        seen = []
        with open(manufacturing.target_path(self.run, self.combo, "box_sldprt"), "wb") as f:
            f.write(b"x")
        with mock.patch.object(solidworks_manufacturing, "export_part",
                               lambda **kw: seen.append(kw)):
            manufacturing.make_cad_group(self.run, self.combo, "box", lambda m: None)
        self.assertEqual(sorted(seen[0]["outputs"]), ["drawing", "slddrw"])    # the PDF and its drawing are made together
        self.assertEqual(seen[0]["part"], "box")
        self.assertTrue(seen[0]["equations_path"].endswith("dimensions.txt"))


if __name__ == "__main__":
    unittest.main()
