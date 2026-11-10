"""The layout picture: signal copper by connectivity, the outline, the route's
colouring by a value, and the PNG written from a run's own files.

Run: python -m unittest tests.test_layout_image   (or: python -m unittest discover -s tests)
"""
import json
import math
import os
import shutil
import tempfile
import unittest

import numpy as np
from matplotlib import colormaps
from matplotlib.colors import TwoSlopeNorm

from microwave_package.design import copper_geometry as cg
from microwave_package.workflow import layout_image as li

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "layout_image")

BARE_COPPER = """# bare board: one straight 0.48 mm track between two launch lands
board=package
pad|top|14.4|-2|0.5|0.5|90
pad|top|13.5|0|0.48|1.4|270
pad|top|-13.5|0|0.48|1.4|90
pad|top|-14.4|2|0.5|0.5|270
track|top|-13.5|0|13.5|0|0.48
region|top|-25,0.36291,25,0.36291,25,12.5,-25,12.5
region|top|-25,-0.36291,25,-0.36291,25,-12.5,-25,-12.5
via|-20|0.663|0.3|0.15
"""

BARE_DIMENSIONS = '"pcb_length"= 50.0\n"pcb_side"= 25\n"pcb_corner_radius"= 1.0\n'


def _route_equal(test, got, want):
    test.assertEqual(len(got), len(want), f"{got} != {want}")
    for (gx, gy), (wx, wy) in zip(got, want):
        test.assertAlmostEqual(gx, wx, places=9)
        test.assertAlmostEqual(gy, wy, places=9)


class TestSignalCopper(unittest.TestCase):
    def test_bare_board_route_is_the_straight_track_port_to_port(self):
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "copper.txt"), "w", encoding="utf-8") as f:
                f.write(BARE_COPPER)
            with open(os.path.join(folder, "dimensions.txt"), "w", encoding="utf-8") as f:
                f.write(BARE_DIMENSIONS)
            layout = li.read_layout(folder)
        _route_equal(self, layout.route, [(-13.5, 0.0), (13.5, 0.0)])
        self.assertAlmostEqual(layout.length_mm, 27.0)
        self.assertEqual(layout.port1, (-13.5, 0.0))
        self.assertEqual(layout.port2, (13.5, 0.0))
        self.assertIsNone(layout.die_half, "no chip board, so no die")
        self.assertEqual(sorted(p.x for _, p in layout.signal_pads), [-13.5, 13.5],
                         "only the two launch lands are signal pads; the SMP ground lands are not")

    def test_package_route_runs_through_the_bond_fingers_and_the_die(self):
        layout = li.read_layout(FIXTURE)
        _route_equal(self, layout.route, [(-13.5, 0.0), (-3.25, 0.0), (-3.0, 0.0),
                                          (3.0, 0.0), (3.25, 0.0), (13.5, 0.0)])
        self.assertAlmostEqual(layout.length_mm, 27.0)
        self.assertEqual(len(layout.signal_tracks), 3, "two package tracks and the die's track, not the floating one")
        self.assertEqual(len(layout.signal_pads), 10, "2 launch lands, 4 bond fingers, 4 die signal pads")
        labels = [(lm.label, round(lm.x, 2)) for lm in layout.landmarks]
        self.assertIn(("bond fingers", -3.5), labels)
        self.assertIn(("bond fingers", 3.5), labels)
        self.assertIn(("die edge", -3.15), labels)
        self.assertIn(("die edge", 3.15), labels)
        self.assertEqual(layout.die_half, 3.15)

    def test_ground_pads_and_ground_bonds_are_not_signal(self):
        layout = li.read_layout(FIXTURE)
        signal_at = {(round(p.x, 3), round(p.y, 3)) for _, p in layout.signal_pads}
        self.assertNotIn((14.4, 2.0), signal_at, "an SMP ground land touches no track")
        self.assertNotIn((3.5, 0.588), signal_at, "a ground bond pad is not linked to the signal")
        self.assertNotIn((-3.5, 0.588), signal_at)

    def test_a_floating_track_is_not_signal(self):
        package = cg.BoardCopper(
            "package",
            tracks=[cg.Track("top", -13.5, 0.0, 13.5, 0.0, 0.48),
                    cg.Track("top", 1.0, 5.0, 2.0, 5.0, 0.48)],  # touches nothing
            pads=[cg.Pad("top", -13.5, 0.0, 0.48, 1.4, 90.0), cg.Pad("top", 13.5, 0.0, 0.48, 1.4, 270.0)])
        tracks_out, _, _, route, p1, p2 = li.signal_copper(package, None, [])
        self.assertEqual(len(tracks_out), 1)
        self.assertEqual(p1, (-13.5, 0.0))
        self.assertEqual(p2, (13.5, 0.0))
        _route_equal(self, route, [(-13.5, 0.0), (13.5, 0.0)])

    def test_a_signal_track_reaching_only_one_pad_is_refused(self):
        package = cg.BoardCopper("package", tracks=[cg.Track("top", -13.5, 0.0, 0.0, 0.0, 0.48)],
                                 pads=[cg.Pad("top", -13.5, 0.0, 0.48, 1.4, 90.0)])
        with self.assertRaises(ValueError):
            li.signal_copper(package, None, [])

    def test_ports_must_be_on_a_package_track_end(self):
        package = cg.BoardCopper("package", tracks=[], pads=[cg.Pad("top", 0.0, 0.0, 1.0, 1.0)])
        with self.assertRaises(ValueError):
            li.signal_copper(package, None, [])


class TestOutline(unittest.TestCase):
    def test_outline_is_the_rounded_board_from_the_dimensions(self):
        layout = li.read_layout(FIXTURE)
        xs = [p[0] for p in layout.outline]
        ys = [p[1] for p in layout.outline]
        self.assertAlmostEqual(min(xs), -25.0)
        self.assertAlmostEqual(max(xs), 25.0)
        self.assertAlmostEqual(min(ys), -12.5)
        self.assertAlmostEqual(max(ys), 12.5)

    def test_outline_falls_back_to_the_copper_extent_without_a_dimensions_file(self):
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "copper.txt"), "w", encoding="utf-8") as f:
                f.write(BARE_COPPER)
            layout = li.read_layout(folder)
        xs = [p[0] for p in layout.outline]
        self.assertAlmostEqual(min(xs), -25.0)
        self.assertAlmostEqual(max(xs), 25.0)

    def test_expressions_in_the_dimensions_file_are_not_read_as_numbers(self):
        dims = li._dimensions(FIXTURE)
        self.assertNotIn("floor_width", dims)
        self.assertEqual(dims["chip_length"], 6.3)


class TestPaintRoute(unittest.TestCase):
    def _colours(self, values, cmap="RdBu_r", vmin=30.0, vmax=70.0):
        from matplotlib.figure import Figure

        figure = Figure(figsize=(2, 2))
        ax = figure.add_subplot(111)
        route = np.column_stack([np.arange(len(values), dtype=float), np.zeros(len(values))])
        norm = TwoSlopeNorm(vmin=vmin, vcenter=50.0, vmax=vmax)
        collection = li.paint_route(ax, route, values, cmap, norm)
        return collection, collection.to_rgba(collection.get_array())

    def test_colour_follows_the_norm_centred_on_50_ohm(self):
        # A segment takes the mean of its two end values; the norm maps 50 to the colour map's centre.
        cmap = colormaps["RdBu_r"]
        _, rgba = self._colours([40.0, 60.0])  # mean 50
        self.assertTrue(np.allclose(rgba[0], cmap(0.5)), "a segment averaging 50 ohm is the colour map's centre")
        _, rgba = self._colours([30.0, 30.0])  # the low end of the scale
        self.assertTrue(np.allclose(rgba[0], cmap(0.0)))
        _, rgba = self._colours([70.0, 70.0])  # the high end of the scale
        self.assertTrue(np.allclose(rgba[0], cmap(1.0)))

    def test_high_and_low_impedance_are_on_opposite_sides_of_the_centre(self):
        _, rgba = self._colours([20.0, 20.0, 80.0, 80.0], vmin=20.0, vmax=80.0)
        self.assertGreater(rgba[0][2], rgba[0][0], "low impedance is blue")
        self.assertGreater(rgba[2][0], rgba[2][2], "high impedance is red")

    def test_a_missing_value_leaves_its_segments_unpainted(self):
        _, rgba = self._colours([50.0, float("nan"), 50.0, 50.0])
        self.assertEqual(rgba[0][3], 0.0, "a segment touching a NaN is not painted")
        self.assertEqual(rgba[1][3], 0.0)
        self.assertGreater(rgba[2][3], 0.0)

    def test_bad_shapes_are_refused(self):
        from matplotlib.figure import Figure

        ax = Figure().add_subplot(111)
        with self.assertRaises(ValueError):
            li.paint_route(ax, np.zeros((1, 2)), [1.0], "RdBu_r", TwoSlopeNorm(40, 50, 60))
        with self.assertRaises(ValueError):
            li.paint_route(ax, np.zeros((3, 2)), [1.0, 2.0], "RdBu_r", TwoSlopeNorm(40, 50, 60))

    def test_a_profile_is_mapped_onto_the_route_by_arc_length(self):
        route = [(0.0, 0.0), (10.0, 0.0), (10.0, 5.0)]  # arc lengths 0, 10, 15
        positions = [0.0, 5.0, 10.0, 15.0]
        values = [50.0, 60.0, 70.0, 80.0]
        np.testing.assert_allclose(li.values_along_route(route, positions, values), [50.0, 70.0, 80.0])
        # The profile's reference plane sits 5 mm before port 1: arc s is at profile position s + 5.
        np.testing.assert_allclose(li.values_along_route(route, positions, values, offset_mm=5.0),
                                   [60.0, 80.0, 80.0])


class TestRender(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="layout_image_")
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def _is_png(self, path):
        with open(path, "rb") as f:
            return f.read(8) == b"\x89PNG\r\n\x1a\n" and os.path.getsize(path) > 10_000

    def test_writes_a_png_of_the_package_layout(self):
        layout = li.read_layout(FIXTURE)
        out = li.render(layout, os.path.join(self.folder, "package.png"), dpi=60)
        self.assertTrue(self._is_png(out))

    def test_writes_a_png_of_the_bare_board_layout_with_a_painted_profile(self):
        with open(os.path.join(self.folder, "copper.txt"), "w", encoding="utf-8") as f:
            f.write(BARE_COPPER)
        layout = li.read_layout(self.folder)
        positions = np.linspace(0.0, 30.0, 61)
        z = 50.0 + 15.0 * np.exp(-((positions - 14.0) / 0.6) ** 2)  # a fake 65 ohm feature
        out = li.render(layout, os.path.join(self.folder, "bare.png"), profile=(positions, z, 0.0),
                        dpi=60)
        self.assertTrue(self._is_png(out))

    def test_a_run_folder_and_its_copper_file_give_the_same_layout(self):
        run = os.path.join(self.folder, "run_x", "models", "default")
        os.makedirs(run)
        for name in ("copper.txt", "bonds.txt", "dimensions.txt"):
            shutil.copy(os.path.join(FIXTURE, name), os.path.join(run, name))
        from_folder = li.read_layout(os.path.join(self.folder, "run_x"))
        from_file = li.read_layout(os.path.join(run, "copper.txt"))
        self.assertEqual(from_folder.name, "run_x")
        _route_equal(self, from_folder.route, from_file.route)

    def test_several_combinations_need_the_one_to_draw_to_be_named(self):
        for combo in ("a", "b"):
            os.makedirs(os.path.join(self.folder, "models", combo))
            shutil.copy(os.path.join(FIXTURE, "copper.txt"), os.path.join(self.folder, "models", combo))
        with self.assertRaises(FileNotFoundError):
            li.read_layout(self.folder)

    def test_the_command_line_writes_the_picture(self):
        import contextlib
        import io

        out = os.path.join(self.folder, "cli.png")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(li.main([FIXTURE, "--out", out]), 0)
        self.assertTrue(self._is_png(out))

    def test_the_command_line_reports_a_bad_path_without_a_traceback(self):
        import contextlib
        import io

        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(li.main([os.path.join(self.folder, "missing")]), 2)
        self.assertIn("no copper.txt", err.getvalue())


class TestVariantRegions(unittest.TestCase):
    """A mitred variant's signal is regions, and the renderer must find it through them. Without the
    variant's own centre line (variant.json) the route comes from the copper, and must still reach
    both ports."""

    def test_a_mitred_route_is_signal_through_its_regions(self):
        from microwave_package.design import test_lines as tl

        folder = tempfile.mkdtemp(prefix="layout_variant_")
        self.addCleanup(shutil.rmtree, folder, True)
        with open(os.path.join(FIXTURE, "..", "test_lines", "tv0_copper.txt"), encoding="utf-8") as f:
            base = f.read()
        tl.write_variant(base, tl.tv3_matched(), os.path.join(folder, "copper.txt"))
        layout = li.read_layout(folder)  # no variant.json beside it: the route comes from the copper
        self.assertEqual(len(layout.signal_regions), 4, "the two mitres, each leg a region")
        self.assertGreater(len(layout.signal_tracks), 80, "the bends are short tracks")
        self.assertEqual(layout.port1, (-13.5, 0.0))
        self.assertEqual(layout.port2, (13.5, 0.0))
        self.assertAlmostEqual(layout.route[-1][0], 13.5)
        with open(os.path.join(folder, "variant.json"), "w", encoding="utf-8") as f:
            json.dump({"vertices": [list(p) for p in tl.tv3_matched().vertices]}, f)
        own = li.read_layout(folder)  # with the generator's centre line: the true length
        self.assertAlmostEqual(own.length_mm, 33.76, places=2)


if __name__ == "__main__":
    unittest.main()
