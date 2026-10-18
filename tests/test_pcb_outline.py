"""Tests for the PCB's rounded corners (design/pcb_outline.py and the copper
decisions in design/copper_geometry.py), plus the AEDT call sequence of
hfss/model_builder.clip_to_outline against a recording fake modeler.

What is pinned: the outline is the rectangle less four quarter-circle corners;
each corner cut is the corner square less that quarter circle (the cut the
model removes); a feature is clipped only if its bounding box reaches a corner
square; and the radius is read from the equations file with the right fallback.

Run: python -m unittest tests.test_pcb_outline
"""
import math
import types
import unittest

from microwave_package.design import copper_geometry as cg
from microwave_package.design import pcb_outline as po
from microwave_package.hfss import model_builder

X, Y, R = 25.0, 12.5, 1.0   # the default board: 50 x 25 mm, 1 mm corners


def shoelace(points):
    area = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        area += x1 * y2 - x2 * y1
    return area / 2.0


def in_outline(u, v, x=X, y=Y, r=R):
    """Analytic membership in the rounded rectangle."""
    eps = 1e-9   # the arc's own points are a rounding error outside the circle
    if abs(u) > x + eps or abs(v) > y + eps:
        return False
    cu, cv = x - r, y - r
    if abs(u) > cu and abs(v) > cv:
        return (abs(u) - cu) ** 2 + (abs(v) - cv) ** 2 <= r * r + eps
    return True


class TestRadiusFromTheEquations(unittest.TestCase):
    def test_the_pcb_radius_is_used_when_the_file_has_one(self):
        self.assertEqual(po.corner_radius_of({"pcb_corner_radius": 1.5, "cavity_corner_radius": 1.0}), 1.5)

    def test_a_file_with_only_the_cavity_radius_matches_the_cavity(self):
        self.assertEqual(po.corner_radius_of({"cavity_corner_radius": 0.8}), 0.8)

    def test_a_file_from_before_the_rounding_is_sharp(self):
        self.assertEqual(po.corner_radius_of({"pcb_length": 50.0}), 0.0)


class TestOutline(unittest.TestCase):
    def test_the_area_is_the_rectangle_less_four_corners(self):
        self.assertAlmostEqual(po.outline_area(X, Y, R), 4 * X * Y - (4 - math.pi) * R * R)
        self.assertAlmostEqual(po.outline_area(X, Y, 0.0), 4 * X * Y)

    def test_the_polygon_area_matches_the_formula(self):
        # A fine polygon of the outline encloses the analytic area.
        for r in (0.5, 1.0, 2.0):
            pts = po.outline_points(X, Y, r, steps=400)
            self.assertAlmostEqual(abs(shoelace(pts)), po.outline_area(X, Y, r), delta=2e-3, msg=r)

    def test_the_polygon_is_counter_clockwise(self):
        self.assertGreater(shoelace(po.outline_points(X, Y, R, steps=8)), 0)

    def test_every_point_is_inside_the_rounded_rectangle(self):
        for u, v in po.outline_points(X, Y, R, steps=32):
            self.assertTrue(in_outline(u, v, X, Y, R), (u, v))

    def test_the_arc_ends_are_where_the_straight_edges_stop(self):
        pts = po.outline_points(X, Y, R, steps=4)
        self.assertIn((X, Y - R), [(round(u, 9), round(v, 9)) for u, v in pts])
        self.assertIn((X - R, Y), [(round(u, 9), round(v, 9)) for u, v in pts])

    def test_a_sharp_board_is_the_four_corners(self):
        self.assertEqual(len(po.outline_points(X, Y, 0.0)), 4)


class TestCornerCuts(unittest.TestCase):
    def test_there_is_one_cut_per_corner_and_none_for_a_sharp_board(self):
        cuts = po.corner_cuts(X, Y, R)
        self.assertEqual(len(cuts), 4)
        self.assertEqual(po.corner_cuts(X, Y, 0.0), [])

    def test_each_cut_is_centred_on_its_arc_and_is_two_radii_square(self):
        for cut in po.corner_cuts(X, Y, R):
            cu, cv = cut.centre
            self.assertAlmostEqual(abs(cu), X - R)
            self.assertAlmostEqual(abs(cv), Y - R)
            u1, v1, u2, v2 = cut.box
            self.assertAlmostEqual(u2 - u1, 2 * R)
            self.assertAlmostEqual(v2 - v1, 2 * R)
            self.assertLess(u1, u2)
            self.assertLess(v1, v2)

    def test_the_cut_removes_exactly_the_points_outside_the_arc(self):
        # Walk a grid over the board. A point is removed by a cut iff it is in
        # that cut's box and not in its disc. Removed exactly where the outline
        # does not reach, and nowhere else inside the board.
        cuts = po.corner_cuts(X, Y, R)
        n = 120
        for i in range(n + 1):
            for j in range(n + 1):
                u = -X + 2 * X * i / n
                v = -Y + 2 * Y * j / n
                removed = False
                for cut in cuts:
                    u1, v1, u2, v2 = cut.box
                    cu, cv = cut.centre
                    in_box = u1 <= u <= u2 and v1 <= v <= v2
                    in_disc = (u - cu) ** 2 + (v - cv) ** 2 <= cut.radius ** 2
                    if in_box and not in_disc:
                        removed = True
                self.assertEqual(removed, not in_outline(u, v), (u, v))

    def test_the_removed_area_per_corner_is_one_minus_quarter_pi_r_squared(self):
        expected = (1 - math.pi / 4) * R * R
        # Area inside the board's corner square and outside the arc, sampled.
        n, count = 400, 0
        for i in range(n):
            for j in range(n):
                u = (X - R) + R * (i + 0.5) / n
                v = (Y - R) + R * (j + 0.5) / n
                if not in_outline(u, v):
                    count += 1
        self.assertAlmostEqual(count * (R / n) ** 2, expected, delta=2e-3)


class TestWhatReachesACorner(unittest.TestCase):
    def test_a_box_in_the_middle_does_not_reach_a_corner(self):
        self.assertFalse(po.reaches_corner((-5, -2, 5, 2), X, Y, R))

    def test_a_box_across_the_whole_board_reaches_every_corner(self):
        self.assertTrue(po.reaches_corner((-X, -Y, X, Y), X, Y, R))

    def test_a_box_touching_one_corner_square_reaches_it(self):
        self.assertTrue(po.reaches_corner((X - 0.5, Y - 0.5, X + 0.2, Y + 0.2), X, Y, R))

    def test_a_box_just_short_of_the_corner_square_does_not(self):
        self.assertFalse(po.reaches_corner((X - R - 0.5, Y - R - 0.5, X - R - 0.1, Y - R - 0.1), X, Y, R))

    def test_a_sharp_board_has_no_corner_to_reach(self):
        self.assertFalse(po.reaches_corner((X - 0.5, Y - 0.5, X, Y), X, Y, 0.0))


class TestCopperAtCorners(unittest.TestCase):
    def test_a_via_at_the_corner_is_clipped_and_a_central_one_is_not(self):
        at_corner = cg.BoardCopper("pcb", vias=[cg.Via(X - 0.5, Y - 0.5, 0.3, 0.1)])
        central = cg.BoardCopper("pcb", vias=[cg.Via(0.0, 0.0, 0.3, 0.1)])
        self.assertEqual(cg.copper_reaching_corners(at_corner, X, Y, R), (False, True))
        self.assertEqual(cg.copper_reaching_corners(central, X, Y, R), (False, False))

    def test_a_ground_plane_across_the_board_is_clipped(self):
        plane = cg.BoardCopper("pcb", regions=[cg.Region("top", (
            (-X, -Y), (X, -Y), (X, Y), (-X, Y)))])
        self.assertEqual(cg.copper_reaching_corners(plane, X, Y, R), (True, False))

    def test_a_track_in_the_middle_is_left_alone(self):
        track = cg.BoardCopper("pcb", tracks=[cg.Track("top", -10, 0, 10, 0, 0.5)])
        self.assertEqual(cg.copper_reaching_corners(track, X, Y, R), (False, False))

    def test_a_pad_rotated_into_a_corner_is_seen(self):
        pad = cg.BoardCopper("pcb", pads=[cg.Pad("top", X - 0.4, Y - 0.4, 1.0, 0.3, 45.0)])
        self.assertEqual(cg.copper_reaching_corners(pad, X, Y, R), (True, False))

    def test_a_sharp_board_clips_nothing(self):
        plane = cg.BoardCopper("pcb", regions=[cg.Region("top", (
            (-X, -Y), (X, -Y), (X, Y), (-X, Y)))])
        self.assertEqual(cg.copper_reaching_corners(plane, X, Y, 0.0), (False, False))


class _Obj:
    def __init__(self, name):
        self.name = name


class _RecordingModeler:
    """Records the calls clip_to_outline makes, in order, and names what it
    makes as the real modeler would (the blank of a subtract keeps its name)."""

    def __init__(self):
        self.calls = []
        self._objects = {}
        self._n = 0

    def _make(self, kind):
        self._n += 1
        obj = _Obj(f"{kind}{self._n}")
        self._objects[obj.name] = obj
        return obj

    def create_box(self, origin, sizes):
        self.calls.append(("box", origin, sizes))
        return self._make("Box")

    def create_cylinder(self, orientation, origin, radius, height):
        self.calls.append(("cylinder", orientation, origin, radius, height))
        return self._make("Cyl")

    def subtract(self, blank, tool, keep_originals=False):
        self.calls.append(("subtract", blank, list(tool), keep_originals))
        return True

    def unite(self, parts):
        self.calls.append(("unite", list(parts)))
        return True

    def __getitem__(self, name):
        return self._objects.get(name) or _Obj(name)


class TestClipCallSequence(unittest.TestCase):
    def setUp(self):
        self.modeler = _RecordingModeler()
        self.hfss = types.SimpleNamespace(modeler=self.modeler)
        self.frame = cg.Frame(x_offset=27.0, z_offset=14.0)

    def test_each_corner_is_a_box_less_a_cylinder_then_subtracted_from_all_targets(self):
        model_builder.clip_to_outline(
            self.hfss, self.frame, X, Y, R, base=-0.1, height=0.2,
            targets=["pcb_substrate", "pcb_copper_top"], log=lambda *_: None)
        kinds = [c[0] for c in self.modeler.calls]
        self.assertEqual(kinds, ["box", "cylinder", "subtract", "subtract"] * 4)
        subtracts = [c for c in self.modeler.calls if c[0] == "subtract"]
        names = []
        for number in range(4):
            cut_piece, disc_part = subtracts[2 * number][1], subtracts[2 * number][2]
            self.assertTrue(cut_piece.startswith("pcb_corner_cut_"))
            self.assertEqual(len(disc_part), 1)
            onto_targets = subtracts[2 * number + 1]
            self.assertEqual(onto_targets[1], ["pcb_substrate", "pcb_copper_top"])
            self.assertEqual(onto_targets[2], [cut_piece])
            self.assertFalse(onto_targets[3])
            names.append(cut_piece)
        self.assertEqual(len(set(names)), 4)

    def test_every_call_uses_names_no_earlier_call_used(self):
        # AEDT would not keep a cut's name when an earlier, consumed cut had
        # the same one, so the names of two clips in one model must differ.
        first = _RecordingModeler()
        second = _RecordingModeler()
        for modeler in (first, second):
            model_builder.clip_to_outline(
                types.SimpleNamespace(modeler=modeler), self.frame, X, Y, R,
                base=-0.1, height=0.2, targets=["vacuum"], log=lambda *_: None)
        first_names = {c[1] for c in first.calls if c[0] == "subtract" and isinstance(c[1], str)
                  and c[1].startswith("pcb_corner_cut")}
        second_names = {c[1] for c in second.calls if c[0] == "subtract" and isinstance(c[1], str)
                  and c[1].startswith("pcb_corner_cut")}
        self.assertTrue(first_names.isdisjoint(second_names))

    def test_every_cylinder_is_a_true_cylinder_of_the_radius_along_up(self):
        model_builder.clip_to_outline(
            self.hfss, self.frame, X, Y, R, base=-0.1, height=0.2,
            targets=["pcb_substrate"], log=lambda *_: None)
        cylinders = [c for c in self.modeler.calls if c[0] == "cylinder"]
        self.assertEqual(len(cylinders), 4)
        for _kind, orientation, _origin, radius, height in cylinders:
            self.assertEqual(orientation, "Y")
            self.assertEqual(radius, R)
            self.assertEqual(height, 0.2)

    def test_the_cylinder_sits_on_the_arc_centre_in_assembly_coordinates(self):
        model_builder.clip_to_outline(
            self.hfss, self.frame, X, Y, R, base=-0.1, height=0.2,
            targets=["pcb_substrate"], log=lambda *_: None)
        origins = [c[2] for c in self.modeler.calls if c[0] == "cylinder"]
        # frame.point maps board (u, v, w) to (u + x_offset, w, -v + z_offset).
        expected = {
            (27.0 + (X - R), -0.1, 14.0 - (Y - R)),
            (27.0 - (X - R), -0.1, 14.0 - (Y - R)),
            (27.0 + (X - R), -0.1, 14.0 + (Y - R)),
            (27.0 - (X - R), -0.1, 14.0 + (Y - R)),
        }
        self.assertEqual({tuple(round(c, 9) for c in o) for o in origins}, {tuple(round(c, 9) for c in e) for e in expected})

    def test_a_sharp_board_makes_no_calls(self):
        model_builder.clip_to_outline(
            self.hfss, self.frame, X, Y, 0.0, base=-0.1, height=0.2,
            targets=["pcb_substrate"], log=lambda *_: None)
        self.assertEqual(self.modeler.calls, [])

    def test_no_targets_makes_no_calls(self):
        model_builder.clip_to_outline(
            self.hfss, self.frame, X, Y, R, base=-0.1, height=0.2,
            targets=[], log=lambda *_: None)
        self.assertEqual(self.modeler.calls, [])


if __name__ == "__main__":
    unittest.main()
