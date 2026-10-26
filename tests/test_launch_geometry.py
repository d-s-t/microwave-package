"""The launch study's copper: the baseline against a real board, and the levers.

No AEDT. The reference is `fixtures/launch_copper_tmm.txt`, the -x launch of a
finished bare-board run (TMM 10i), cut from its copper.txt.

Run: python -m unittest discover -s tests
"""
import math
import os
import unittest

from microwave_package.design import copper_geometry as cg, smp
from microwave_package.hfss.launch import geometry as geo

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "launch_copper_tmm.txt")
# The solved gap of that board: the ground starts 0.34737 mm off the axis.
GAP = 0.34737 - 0.1
VALUES = {"cpw_trace_width": 0.2, "launch_clearance": 0.2, "port_space": 6.0, "pcb_length": 50.0}


def real_board():
    return cg.read_file(FIXTURE)["package"]


def baseline(**changes):
    return geo.from_values({**VALUES, **changes}, GAP)


class BaselineMatchesTheBoard(unittest.TestCase):
    def setUp(self):
        self.launch = baseline()
        self.mine = geo.board(self.launch)
        self.real = real_board()
        # The short board's u maps to the full board's by this shift.
        self.shift = self.launch.half_length - 25.0

    def test_the_geometry_is_valid(self):
        self.assertEqual(self.launch.problems(), [])

    def test_the_connector_stands_where_the_real_one_does(self):
        self.assertAlmostEqual(self.launch.pin_u + self.shift, -13.5, places=6)
        self.assertAlmostEqual(self.launch.origin_u + self.shift, -16.4, places=6)

    def test_top_copper_agrees_point_by_point(self):
        """A grid over the launch and the line behind it: metal in one is metal
        in the other, except for the facets of the void and a hair at edges."""
        step = 0.05
        wrong = total = 0
        x = -self.launch.half_length + 0.02
        while x < self.launch.half_length - 0.5:
            y = -3.45
            while y < 3.45:
                mine = geo.covers(self.mine, cg.TOP, x, y)
                real = geo.covers(self.real, cg.TOP, x + self.shift, y)
                total += 1
                wrong += mine != real
                y += step
            x += step
        self.assertLess(wrong / total, 0.002, f"{wrong} of {total} grid points differ")

    def test_vias_are_the_boards(self):
        mine = {(round(v.x + self.shift, 3), round(abs(v.y), 3), round(v.y > 0)) for v in self.mine.vias}
        real = {(round(v.x, 3), round(abs(v.y), 3), round(v.y > 0)) for v in self.real.vias
                if v.x > -25.0 + 0.3 and v.x <= self.launch.half_length - geo.PORT_MARGIN + self.shift + 1e-9
                and abs(v.y) < 3.4}
        self.assertEqual(sorted(mine ^ real), [])

    def test_there_are_ground_vias_round_the_line_and_behind_the_launch(self):
        ys = {round(abs(v.y), 3) for v in self.mine.vias}
        self.assertIn(0.0, ys)
        self.assertIn(0.647, ys)


class TheLevers(unittest.TestCase):
    def test_a_taper_adds_a_polygon_and_moves_the_trace_back(self):
        plain = geo.top_copper(baseline())
        tapered = geo.top_copper(baseline(launch_taper_length=1.0))
        self.assertEqual(len(tapered[0]), len(plain[0]) + 1)
        self.assertAlmostEqual(tapered[1][0].x1 - plain[1][0].x1, 0.7 + 1.0, places=6)

    def test_a_taper_to_a_wider_trace_is_a_widening(self):
        launch = baseline(launch_taper_length=0.5, cpw_trace_width=0.4)
        taper = geo.top_copper(launch)[0][-1].points
        self.assertAlmostEqual(max(abs(y) for _x, y in taper), 0.24, places=6)

    def test_pad_width_moves_the_keepout(self):
        narrow = baseline(launch_pad_width=0.30)
        self.assertAlmostEqual(narrow.pull_edge, 0.15 + 0.2)
        pads = geo.top_copper(narrow)[2]
        self.assertAlmostEqual(pads[0].ysize, 0.30)

    def test_zero_means_the_vendor_value(self):
        launch = baseline(launch_pad_width=0, launch_pad_length=0, launch_void_radius=0)
        self.assertAlmostEqual(launch.pad_width, 0.48)
        self.assertAlmostEqual(launch.pad_length, 1.4)
        self.assertAlmostEqual(launch.void_radius, smp.VOID_RADIUS)

    def test_the_void_radius_changes_the_plane(self):
        small = geo.board(baseline(launch_void_radius=2.0))
        # The disc is smaller, so there is metal 2.2 mm from the origin that the vendor's has not.
        o = baseline().origin_u
        probe = (o - 2.15, 0.3)
        self.assertTrue(geo.covers(small, cg.TOP, *probe))
        self.assertFalse(geo.covers(geo.board(baseline()), cg.TOP, *probe))

    def test_a_void_wider_than_the_land_is_refused(self):
        problems = baseline(launch_void_radius=2.8).problems()
        self.assertTrue(any("land" in p for p in problems), problems)

    def test_a_void_inside_the_slot_is_refused(self):
        self.assertTrue(baseline(launch_void_radius=0.9).problems())

    def test_a_pad_that_barely_clears_the_gap_edge_gets_no_sliver_of_keepout(self):
        """A 0.30 pad with the 0.2 clearance would step the ground by 2.6 um."""
        plane = geo.top_copper(baseline(launch_pad_width=0.30))[0][0].points
        steps = {round(abs(y), 4) for _x, y in plane}
        self.assertNotIn(0.35, steps)

    def test_trace_width_changes_the_gap_edge(self):
        wide = baseline(cpw_trace_width=0.4)
        self.assertAlmostEqual(wide.gnd_inner, 0.2 + GAP + 0.0)


class Outline(unittest.TestCase):
    def test_the_board_reaches_from_the_end_to_the_cut(self):
        launch = baseline()
        reach = launch.port_space - smp.OUTER_V + smp.INNER_V
        self.assertAlmostEqual(2 * launch.half_length, reach + launch.trace_length)

    def test_the_polygons_are_closed_sensible_shapes(self):
        for region in geo.board(baseline()).regions:
            xs = [p[0] for p in region.points]
            self.assertEqual(len(set(region.points)), len(region.points) - sum(
                1 for a, b in zip(region.points, region.points[1:] + region.points[:1]) if a == b))
            self.assertTrue(all(math.isfinite(v) for v in xs))


if __name__ == "__main__":
    unittest.main()
