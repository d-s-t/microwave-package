"""The SMP connector windows in the lid: design/constraints.py (smp_window,
smp_window_clearances, check_smp_window) and base_parts/add_smp_window.py, which grows the
windows in SolidWorks by the same numbers.

What is pinned:
- the defaults: smp_window_extra 0.25 mm, smp_window_radius 1.0 mm, smp_relief_on 0. The +x
  window is 13.65 .. 19.25 by +-2.80 (5.6 mm square, centre unchanged; the old one was 13.9 ..
  19.0 by +-2.55);
- the 5.0 x 5.0 mm housing (corners cut back 0.42 mm) clears the window by 0.199 mm at its
  corners (the R1 arcs centred at (18.25, +-1.80)), 0.25 mm at the ends and 0.30 mm at the
  sides; a brute-force distance to a sampled outline agrees;
- the least extra that keeps 0.15 mm at the corners is 0.2118 mm: 0.20 is refused, with
  smp_window_extra named;
- the lid volume the SolidWorks script must find is -2.822456 mm3 (relief off, window grown,
  corners rounded), from the eight relief cuts coming out, the two windows growing with
  their corners rounded, and the channel ending 0.25 mm sooner at each connector;
- equation lines starting "smp_" stay under 400 characters: SolidWorks dropped longer new
  names from a linked equation file on 2026-10-22.

Run: python -m unittest tests.test_smp_window
"""
import math
import unittest

from microwave_package import paths
from microwave_package.design import constraints, equations as eq, parameter_meta

# The +x connector housing in the lid's frame, mm, in outline order: AEDT, 2026-10-22.
HOUSING = [(14.42, 2.50), (18.58, 2.50), (19.00, 2.08), (19.00, -2.08),
           (18.58, -2.50), (14.42, -2.50), (14.00, -2.08), (14.00, 2.08)]
TMM = "Rogers TMM 10i (tm)"


def defaults(**changes):
    values = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    values.update(changes)
    return values


def outline(w, n=200):
    """The window's outline as a dense polyline: the four R arcs, and the straight walls
    between them, `n` points to each piece."""
    xi, xo, hw, r = w["xi"], w["xo"], w["hw"], w["r"]
    arcs = [((xo - r, hw - r), 0.0, math.pi / 2), ((xi + r, hw - r), math.pi / 2, math.pi),
            ((xi + r, -(hw - r)), math.pi, 1.5 * math.pi), ((xo - r, -(hw - r)), 1.5 * math.pi, 2 * math.pi)]
    pts = []
    for k, ((cx, cz), a, b) in enumerate(arcs):
        for i in range(n):
            t = a + (b - a) * i / n
            pts.append((cx + r * math.cos(t), cz + r * math.sin(t)))
        (nx, nz), na, _ = arcs[(k + 1) % 4]
        start = (pts[-1][0], pts[-1][1])
        end = (nx + r * math.cos(na), nz + r * math.sin(na))
        for i in range(1, n):
            pts.append((start[0] + (end[0] - start[0]) * i / n, start[1] + (end[1] - start[1]) * i / n))
    return pts


def housing_samples(n=40):
    out = []
    for (ax, az), (bx, bz) in zip(HOUSING, HOUSING[1:] + HOUSING[:1]):
        for i in range(n):
            out.append((ax + (bx - ax) * i / n, az + (bz - az) * i / n))
    return out


class TestWindow(unittest.TestCase):
    def test_the_defaults_give_a_5p6_mm_window_from_13p65_to_19p25(self):
        w = constraints.smp_window(defaults())
        self.assertAlmostEqual(w["xi"], 13.65, places=9)
        self.assertAlmostEqual(w["xo"], 19.25, places=9)
        self.assertAlmostEqual(w["hw"], 2.80, places=9)
        self.assertAlmostEqual(w["length"], 5.6, places=9)
        self.assertEqual(w["r"], 1.0)

    def test_the_defaults_declare_the_new_parameters_and_the_reliefs_are_off(self):
        v = defaults()
        self.assertEqual(v["smp_window_extra"], 0.25)
        self.assertEqual(v["smp_window_radius"], 1.0)
        self.assertEqual(v["smp_relief_on"], 0.0)
        for name in ("smp_window_extra", "smp_window_radius", "smp_relief_on"):
            self.assertIn(name, parameter_meta.LABELS)
        self.assertIn("smp_relief_on", parameter_meta.BOOLEAN_PARAMETERS)
        self.assertFalse(constraints.smp_relief_enabled(v))
        self.assertTrue(constraints.smp_relief_enabled(defaults(smp_relief_on=1.0)))

    def test_the_housing_clears_the_window_by_0p199_at_the_corners_0p25_at_the_ends_0p30_at_the_sides(self):
        c = constraints.smp_window_clearances(defaults())
        self.assertAlmostEqual(c["corners"], 0.1994, places=4)
        self.assertAlmostEqual(c["ends"], 0.25, places=9)
        self.assertAlmostEqual(c["sides"], 0.30, places=9)

    def test_the_corner_clearance_agrees_with_a_brute_force_distance_to_the_outline(self):
        poly = outline(constraints.smp_window(defaults()))
        brute = min(min(math.hypot(p[0] - q[0], p[1] - q[1]) for q in poly) for p in housing_samples())
        self.assertAlmostEqual(brute, constraints.smp_window_clearances(defaults())["corners"], places=3)

    def test_the_least_extra_that_keeps_0p15_mm_at_the_corners_is_0p2118(self):
        lo, hi = 0.20, 0.25
        for _ in range(60):
            mid = (lo + hi) / 2
            clear = min(constraints.smp_window_clearances(defaults(smp_window_extra=mid)).values())
            if clear < constraints.SMP_WINDOW_MIN_CLEARANCE:
                lo = mid
            else:
                hi = mid
        self.assertAlmostEqual(hi, 0.2118, delta=0.0005)

    def test_a_window_too_small_for_the_housing_is_refused_with_the_extra_named(self):
        reason = constraints.check_smp_window(defaults(smp_window_extra=0.2))
        self.assertIsNotNone(reason)
        self.assertIn("smp_window_extra", reason)
        self.assertIn("too small", reason)
        self.assertIn("corners", reason)
        self.assertIsNone(constraints.check_smp_window(defaults(smp_window_extra=0.22)))

    def test_a_negative_extra_or_a_zero_radius_is_refused(self):
        self.assertIn("negative", constraints.check_smp_window(defaults(smp_window_extra=-0.1)))
        self.assertIn("must be above 0", constraints.check_smp_window(defaults(smp_window_radius=0.0)))

    def test_a_radius_of_half_the_window_side_is_refused(self):
        self.assertIn("not less than half", constraints.check_smp_window(defaults(smp_window_radius=2.8)))

    def test_a_channel_that_runs_into_the_corner_arcs_is_refused(self):
        self.assertIn("corner arcs", constraints.check_smp_window(defaults(route_width=3.4)))
        self.assertIsNone(constraints.check_smp_window(defaults(route_width=2.4)))

    def test_the_window_keeps_clear_of_the_screws_and_the_lid_edge_at_the_defaults(self):
        w = constraints.smp_window(defaults())
        # The screw at (21.5, 9.0) with a 2.14 mm countersink: 4.76 mm of lid to the window.
        self.assertGreater(constraints._window_gap(21.5, 9.0, w) - 2.14, 4.0)
        self.assertIsNone(constraints.check_smp_window(defaults()))

    def test_the_layout_check_passes_the_defaults_and_refuses_a_window_too_small(self):
        self.assertIsNone(constraints.check_layout(defaults(), TMM))
        self.assertIn("smp_window_extra", constraints.check_layout(defaults(smp_window_extra=0.1), TMM))

    def test_the_optional_reliefs_pass_the_layout_check_with_the_new_window(self):
        self.assertIsNone(constraints.check_layout(defaults(smp_relief_on=1.0), TMM))

    def test_the_lid_volume_the_script_must_find_is_minus_2p822456(self):
        v = defaults()
        depth = v["lid_width"]
        w = constraints.smp_window(v)
        area_new = w["length"] * w["width"] - (4 - math.pi) * w["r"] ** 2
        area_old = 5.1 ** 2 + 4 * constraints.smp_relief_area(1.0, 0.6)
        channel = 2 * v["route_width"] * v["smp_window_extra"] * v["route_clearence"]
        self.assertAlmostEqual(-2 * depth * (area_new - area_old) + channel, -2.822456, delta=1e-5)

    def test_equation_lines_for_the_smp_window_stay_under_400_characters(self):
        with open(paths.DEFAULT_EQUATIONS_FILE, encoding="utf-8") as f:
            for line in f:
                if line.startswith('"smp_'):
                    self.assertLessEqual(len(line.rstrip("\n")), 400, line[:40])


if __name__ == "__main__":
    unittest.main()
