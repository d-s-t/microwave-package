"""The controlled test lines: the geometry rules, TV0's own copper reproduced exactly, each
variant's copper checked against the generator's rules, and the predictions the report rests on.

Run: python -m unittest tests.test_test_lines
"""
import os
import shutil
import tempfile
import unittest

import numpy as np

from microwave_package.design import copper_geometry as cg
from microwave_package.design import test_lines as tl

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "test_lines", "tv0_copper.txt")
TV0_TRACK = "track|top|-13.5|0|13.5|0|0.48"


def _tv0_text() -> str:
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read()


def _vias_xy(text: str) -> np.ndarray:
    return np.array([(v.x, v.y) for v in cg.parse(text)["package"].vias])


def _is_section_via(line: str) -> bool:
    return line.startswith("via|") and abs(float(line.split("|")[1])) < tl.SPLICE_X - 1e-9


def _is_top_pour(line: str) -> bool:
    return line.startswith("region|top|") and len(line.split("|")[2].split(",")) == 164


class TestRules(unittest.TestCase):
    def test_fence_and_copper_clearance_are_the_generator_values(self):
        self.assertAlmostEqual(tl.GAP, 0.12291, places=5)
        self.assertAlmostEqual(tl.fence_y(0.48), 0.66291, places=5)
        self.assertAlmostEqual(tl.fence_copper(), 0.42291, places=5)

    def test_douville_james_mitre_for_the_trace(self):
        self.assertAlmostEqual(tl.mitre_fraction(0.48), 0.7015, places=3)
        self.assertAlmostEqual(tl.mitre_depth(0.48), 0.3367, places=3)

    def test_offset_of_a_straight_line_is_parallel(self):
        left = tl.offset_polyline([(0.0, 0.0), (10.0, 0.0)], [0.5], +1)
        right = tl.offset_polyline([(0.0, 0.0), (10.0, 0.0)], [0.5], -1)
        np.testing.assert_allclose(left, [[0.0, 0.5], [10.0, 0.5]])
        np.testing.assert_allclose(right, [[0.0, -0.5], [10.0, -0.5]])

    def test_mitre_cut_lies_on_the_outer_edges_at_the_optimum_depth(self):
        route = tl.build_route("corner", [(-10.0, 0.0, "end"), (0.0, 0.0, "mitre"), (0.0, -10.0, "end")],
                               [0.48, 0.48])
        o, a, b, _, _, _ = tl._mitre_points(route, 1, tl._outer_side(route, 1))
        h = 0.24
        self.assertAlmostEqual(o[0], h)
        self.assertAlmostEqual(o[1], h)
        self.assertAlmostEqual(a[1], h, msg="A is on the north (outer) edge of the eastbound leg")
        self.assertAlmostEqual(b[0], h, msg="B is on the east (outer) edge of the southbound leg")
        self.assertAlmostEqual(float(np.hypot(*(o - a))), tl.mitre_depth(0.48), places=9)
        self.assertAlmostEqual(float(np.hypot(*(o - b))), tl.mitre_depth(0.48), places=9)

    def test_polygon_simplicity(self):
        square = [(0, 0), (1, 0), (1, 1), (0, 1)]
        bowtie = [(0, 0), (1, 1), (1, 0), (0, 1)]
        self.assertTrue(tl.polygon_is_simple(square))
        self.assertFalse(tl.polygon_is_simple(bowtie))

    def test_halfplane_clip(self):
        square = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
        clipped = tl._clip_halfplane(square, (1.0, 0.0), 1.0)  # keep x <= 1
        self.assertEqual(sorted(set((round(x, 9), round(y, 9)) for x, y in clipped)),
                         [(0.0, 0.0), (0.0, 2.0), (1.0, 0.0), (1.0, 2.0)])


class TestRoutes(unittest.TestCase):
    def test_route_lengths(self):
        self.assertAlmostEqual(tl.tv1_step().length, 27.0, places=6)
        self.assertAlmostEqual(tl.tv2_taper().length, 27.0, places=6)
        self.assertAlmostEqual(tl.tv3_matched().length, 33.76, places=2)
        self.assertAlmostEqual(tl.tv4_unmatched().length, 34.59, places=2)
        self.assertAlmostEqual(tl.tv5_combined().length, 34.79, places=1)

    def test_features_keep_a_resolution_from_each_launch(self):
        resolution = 3.9  # mm: the 20 GHz resolution of the lidded line (see the report)
        for make in (tl.tv3_matched, tl.tv4_unmatched, tl.tv5_combined):
            route = make()
            positions = [s for s, _ in route.marks]
            self.assertGreaterEqual(min(positions), resolution, route.name)
            self.assertGreaterEqual(route.length - max(positions), resolution, route.name)

    def test_the_routed_lines_leave_the_straight_lid_channel(self):
        """Why TV3 to TV5 need a wider relief pocket: their signal is more than the channel's
        half-width (0.6 mm) from the centre line, under a perfect-E lid."""
        self.assertLessEqual(tl.tv1_step().lateral_extent(), tl.LID_CHANNEL / 2.0)
        self.assertLessEqual(tl.tv2_taper().lateral_extent(), tl.LID_CHANNEL / 2.0)
        for make in (tl.tv3_matched, tl.tv4_unmatched, tl.tv5_combined):
            self.assertGreater(make().lateral_extent(), tl.LID_CHANNEL / 2.0, make.__name__)

    def test_the_taper_is_smooth_and_the_step_is_eight_millimetres(self):
        taper = tl.tv2_taper()
        self.assertLess(float(np.max(np.abs(np.diff(taper.widths)))), 0.02)
        self.assertAlmostEqual(min(taper.widths), 0.30, places=2)
        secs = tl.section_truth(tl.tv5_combined())
        narrow = [s for s in secs if abs(s["width"] - 0.30) < 1e-9]
        self.assertEqual(len(narrow), 1)
        self.assertAlmostEqual(narrow[0]["s1"] - narrow[0]["s0"], 8.0, places=6)


class TestCopper(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = _tv0_text()
        cls.reference_text, cls.reference_report = tl.write_variant(cls.base, tl.straight_reference())

    def test_straight_reference_reproduces_tv0_exactly(self):
        """The lattice rule, the pour splice and the copy of every other line reproduce TV0: the same
        1698 vias (the regenerated ones in a different order), and every other line of the same 1750
        in the same order."""
        np.testing.assert_allclose(np.sort(_vias_xy(self.base), axis=0),
                                   np.sort(_vias_xy(self.reference_text), axis=0), atol=1e-9)
        non_via = lambda text: [line for line in text.splitlines() if not line.startswith("via|")]
        self.assertEqual(non_via(self.base), non_via(self.reference_text))
        self.assertEqual(len(self.reference_text.splitlines()), len(self.base.splitlines()))
        self.assertEqual(self.reference_report.vias_removed, 944)
        self.assertEqual(self.reference_report.vias_placed, 944)
        self.assertEqual(self.reference_report.vias_refused, 0)

    def test_lines_outside_the_section_are_copied_verbatim_in_order(self):
        """Every TV0 line outside the section (the launch lands and their regions, the pads, the
        bottom plane, the vias with |x| >= 12.6) appears in each variant, unchanged, in TV0's order."""
        expected = [line for line in self.base.splitlines()
                    if not _is_section_via(line) and line != TV0_TRACK and not _is_top_pour(line)]
        for make in tl.VARIANTS.values():
            text, _ = tl.write_variant(self.base, make())
            it = iter(text.splitlines())
            for line in expected:
                self.assertTrue(any(line == got for got in it),
                                f"{make.__name__}: {line[:70]} is missing or out of order")

    def test_pours_keep_their_vertices_outside_the_splice(self):
        """The top pours keep every vertex with |x| >= 12.6 exactly as TV0 has it; only the straight
        inner edge between -12.6 and +12.6 is replaced by the gap's offset of the signal."""
        base_pours = [r.points for r in cg.parse(self.base)["package"].regions if len(r.points) == 82]
        for make in tl.VARIANTS.values():
            text, report = tl.write_variant(self.base, make())
            self.assertEqual(report.pour_splices, 2)
            new_pours = [r.points for r in cg.parse(text)["package"].regions if len(r.points) > 82]
            self.assertEqual(len(new_pours), 2, make.__name__)
            for old, new in zip(base_pours, new_pours):
                outer_old = [p for p in old if abs(p[0]) >= tl.SPLICE_X - 1e-9]
                outer_new = [p for p in new if abs(p[0]) >= tl.SPLICE_X - 1e-9]
                self.assertEqual(outer_old, outer_new, make.__name__)
                self.assertIn((-tl.SPLICE_X, old[[i for i, p in enumerate(old) if abs(p[0] + tl.SPLICE_X) < 1e-9][0]][1]), new)

    def test_every_region_is_a_simple_polygon(self):
        for make in tl.VARIANTS.values():
            text, _ = tl.write_variant(self.base, make())
            for region in cg.parse(text)["package"].regions:
                self.assertTrue(tl.polygon_is_simple(region.points), make.__name__)

    def test_vias_keep_the_fence_clearance_from_the_signal_and_their_spacing(self):
        for make in tl.VARIANTS.values():
            route = make()
            text, _ = tl.write_variant(self.base, route)
            vias = _vias_xy(text)
            section = vias[np.abs(vias[:, 0]) < tl.SPLICE_X - 1e-9]
            polys = tl.piece_polygons(tl.signal_pieces(route))
            self.assertGreaterEqual(float(tl.copper_distance(section, polys).min()), tl.fence_copper() - 1e-6,
                                    f"{route.name}: a via is too close to the trace or the gap")
            d = np.hypot(vias[:, None, 0] - vias[None, :, 0], vias[:, None, 1] - vias[None, :, 1])
            np.fill_diagonal(d, np.inf)
            idx = np.where(np.abs(vias[:, 0]) < tl.SPLICE_X - 1e-9)[0]
            self.assertGreaterEqual(float(d[idx].min()), tl.VIA_DIA + tl.VIA_MIN - 1e-6, route.name)

    def test_the_gap_is_constant_along_a_straight_section(self):
        """The pour's edge sits one gap from the copper all along a straight section, the 0.30 mm
        step included: the boundary is sampled every 0.1 mm and each sample is measured."""
        route = tl.tv1_step()
        text, _ = tl.write_variant(self.base, route)
        polys = tl.piece_polygons(tl.signal_pieces(route))
        checked = 0
        for region in cg.parse(text)["package"].regions:
            if len(region.points) <= 82:
                continue
            pts = np.asarray(region.points, dtype=float)
            samples = []
            for a, b in zip(pts, np.roll(pts, -1, axis=0)):
                n = max(1, int(np.hypot(*(b - a)) / 0.1))
                samples.append(a + np.linspace(0.0, 1.0, n, endpoint=False)[:, None] * (b - a))
            samples = np.vstack(samples)
            on_straight = samples[(np.abs(samples[:, 0]) <= 3.5) & (np.abs(samples[:, 0]) >= 0.5)
                                  & (np.abs(samples[:, 1]) < 1.0)]
            gaps = tl.copper_distance(on_straight, polys)
            checked += len(gaps)
            self.assertAlmostEqual(float(gaps.min()), tl.GAP, places=6)
            self.assertAlmostEqual(float(gaps.max()), tl.GAP, places=6)
        self.assertGreater(checked, 100)

    def test_a_routed_variant_builds_every_via_it_lists(self):
        """Round trip: the copper parses, and the fake modeler builds one via per via in the file.
        The builder groups vias into blocks for AEDT to clone; an irregular fence is not a case that
        grouping was written for, so the count is checked, not assumed."""
        from tests.test_copper_geometry import FRAME, STACK, FakeModeler

        route = tl.tv3_matched()
        text, _ = tl.write_variant(self.base, route)
        board = cg.parse(text)["package"]
        modeler = FakeModeler()
        cg.build(modeler, board, FRAME, top_face=STACK.pcb_top, bottom_face=STACK.pcb_bottom,
                 thickness=STACK.copper_thickness, prefix="pcb", log=lambda _m: None)
        # one cylinder per via, and two rounded ends per track (TV0 shows the same 2 for its one track)
        self.assertEqual(len(modeler.cylinders) - 2 * len(board.tracks), len(board.vias))

    def test_the_command_line_writes_the_variant_folders(self):
        folder = tempfile.mkdtemp(prefix="test_lines_")
        self.addCleanup(shutil.rmtree, folder, True)
        src = os.path.join(folder, "copper.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write(self.base)
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tl._main([src, "--out", os.path.join(folder, "out"), "--only", "TV1"]), 0)
        self.assertTrue(os.path.isfile(os.path.join(folder, "out", "TV1", "copper.txt")))
        self.assertTrue(os.path.isfile(os.path.join(folder, "out", "TV1", "variant.json")))


class TestPredictions(unittest.TestCase):
    def test_lidded_line_eps_eff(self):
        self.assertAlmostEqual(tl.lid_eps_eff(), 1.78, places=2)
        self.assertNotAlmostEqual(tl.lid_eps_eff(), tl.EPS_EFF_DEFAULT, places=1)

    def test_corner_prediction_brackets_the_born_threshold(self):
        p = tl.corner_prediction()
        self.assertAlmostEqual(p["c_threshold_fF"], 9.55, places=1)
        lo, hi = p["c_range_fF"]
        self.assertLess(lo, p["c_threshold_fF"])
        self.assertGreater(hi, p["c_threshold_fF"], "the bracket straddles the threshold")
        self.assertAlmostEqual(p["born_area_ohm_mm"], 2.68, places=1)


if __name__ == "__main__":
    unittest.main()
