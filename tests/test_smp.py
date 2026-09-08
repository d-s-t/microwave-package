"""Tests for the SMP land pattern and where the launch sits.

The numbers are Wuerth's, read out of the KiCad footprint shipped with
WR-SMP 60124102122403. These tests exist to catch them being edited by
hand, and to pin the placement rule the board depends on.
"""
import unittest

from eigen_mode_analyze import smp


class TestTheVendorNumbers(unittest.TestCase):
    def test_signal_pad_matches_the_vendor_footprint(self):
        u, v, su, sv = smp.SIGNAL_PAD
        self.assertEqual((u, v, su, sv), (0.00, 2.90, 0.48, 1.40))

    def test_three_ground_pads_form_the_u(self):
        """Two arms along the signal axis and a strap across its closed
        end -- the strap is what joins the two sides of the ground."""
        self.assertEqual(len(smp.PADS), 4)
        arms = [p for p in smp.PADS if abs(p[0]) > 1]
        self.assertEqual(len(arms), 2)
        self.assertAlmostEqual(smp.GROUND_STRAP[0], 0.0)

    def test_extents_follow_from_the_pads(self):
        self.assertAlmostEqual(smp.OUTER_V, -2.60)
        self.assertAlmostEqual(smp.INNER_V, 3.60)
        self.assertAlmostEqual(smp.LENGTH, 6.20)

    def test_the_signal_pad_is_the_innermost_copper(self):
        """The centre conductor meets the launch there, so nothing may
        reach past it."""
        self.assertAlmostEqual(smp.INNER_V, smp.SIGNAL_PAD[1] + smp.SIGNAL_PAD[3] / 2)


class TestPlacement(unittest.TestCase):
    HALF_L = 13.0
    PORT_SPACE = 1.5

    def test_outermost_copper_sits_port_space_from_the_edge(self):
        origin = smp.launch_origin(self.HALF_L, self.PORT_SPACE)
        outer = origin - smp.OUTER_V
        self.assertAlmostEqual(outer, self.HALF_L - self.PORT_SPACE)

    def test_launch_centre_is_where_the_trace_ends(self):
        self.assertAlmostEqual(smp.launch_centre(self.HALF_L, self.PORT_SPACE), 6.0)

    def test_inner_edge_is_inboard_of_the_centre(self):
        self.assertLess(
            smp.launch_inner_edge(self.HALF_L, self.PORT_SPACE),
            smp.launch_centre(self.HALF_L, self.PORT_SPACE),
        )

    def test_the_whole_launch_stays_on_the_board(self):
        origin = smp.launch_origin(self.HALF_L, self.PORT_SPACE)
        for _u, v, _su, sv in smp.PADS:
            for edge in (v - sv / 2, v + sv / 2):
                self.assertLess(abs(origin - edge), self.HALF_L)

    def test_moving_the_connector_out_moves_everything_together(self):
        a = smp.launch_centre(self.HALF_L, 1.5)
        b = smp.launch_centre(self.HALF_L, 2.5)
        self.assertAlmostEqual(a - b, 1.0)


class TestParameterHandoff(unittest.TestCase):
    def test_every_pad_reaches_the_parameter_file(self):
        """The DelphiScript reads these rather than carrying its own
        copy, so the vendor's numbers live in one place."""
        params = smp.as_parameters()
        for prefix in ("smp_sig", "smp_arml", "smp_armr", "smp_strap"):
            for suffix in ("u", "v", "su", "sv"):
                self.assertIn(f"{prefix}_{suffix}", params)
        self.assertIn("smp_outer_v", params)
        self.assertIn("smp_inner_v", params)

    def test_the_values_are_the_pads(self):
        params = smp.as_parameters()
        self.assertAlmostEqual(params["smp_sig_v"], smp.SIGNAL_PAD[1])
        self.assertAlmostEqual(params["smp_strap_su"], smp.GROUND_STRAP[2])


if __name__ == "__main__":
    unittest.main()


class TestTheConnectorModel(unittest.TestCase):
    """Placing the vendor solid on its launch.

    Worth pinning because a frame error here is invisible in the tree:
    the connector simply sits in the wrong plane, and the model still
    solves.
    """

    def test_the_model_ships_with_the_repo(self):
        from eigen_mode_analyze import paths

        self.assertTrue(paths.SMP_MODEL_FILE.is_file(),
                        "the split connector STEP should be in base_parts")

    def test_the_split_model_has_three_named_bodies(self):
        """The whole point of the split: HFSS must see outer conductor,
        centre conductor and dielectric as separate solids, not one lump."""
        from eigen_mode_analyze import paths

        text = paths.SMP_MODEL_FILE.read_text(encoding="utf-8", errors="ignore")
        for product in ("SMP_outer_conductor", "SMP_center_conductor", "SMP_dielectric"):
            self.assertIn(product, text, f"{product} missing from the split STEP")

    def test_the_rotation_matches_the_frame_used_everywhere_else(self):
        """The model has Z up and the assembly has Y up. -90 about X is
        the same mapping Frame applies; disagreeing would put the
        connector in a different plane from the board."""
        from eigen_mode_analyze import copper_geometry as cg

        self.assertAlmostEqual(smp.model_rotation_degrees(), -90.0)

        # Frame sends (u, v, w) to (u, w, -v); a -90 rotation about X
        # sends (x, y, z) to (x, z, -y). Same thing.
        frame = cg.Frame()
        self.assertEqual(frame.point(1.0, 2.0, 3.0), [1.0, 3.0, -2.0])

    def test_the_two_connectors_sit_at_opposite_ends(self):
        from eigen_mode_analyze import copper_geometry as cg

        frame = cg.Frame(x_offset=14.0, z_offset=6.0)
        near = smp.model_offset(frame, 1.381, 13.0, 1.5, -1.0)
        far = smp.model_offset(frame, 1.381, 13.0, 1.5, +1.0)
        self.assertLess(near[0], frame.x_offset)
        self.assertGreater(far[0], frame.x_offset)
        self.assertAlmostEqual(frame.x_offset - near[0], far[0] - frame.x_offset)

    def test_the_connector_sits_on_the_board_surface(self):
        from eigen_mode_analyze import copper_geometry as cg

        frame = cg.Frame(x_offset=14.0, z_offset=6.0)
        offset = smp.model_offset(frame, 1.381, 13.0, 1.5, +1.0)
        self.assertAlmostEqual(offset[1], 1.381, msg="height must be the board top face")

    def test_the_split_bodies_land_on_the_right_material_rules(self):
        """The two conductors hit the bare `connector` rule (PEC lump);
        the bead hits `dielectric` first, which carries no material so
        place_connectors' LCP assignment stands."""
        from eigen_mode_analyze import model_builder

        def first_rule(name):
            for rule in model_builder.MATERIAL_RULES:
                if rule[0] in name.lower():
                    return rule
            return None

        self.assertEqual(first_rule("connector_1_outer")[1], "connector")
        self.assertEqual(first_rule("connector_1_center")[1], "connector")
        diel_rule = first_rule("connector_1_dielectric")
        self.assertEqual(diel_rule[1], "connector_dielectric")
        self.assertIsNone(diel_rule[2], "the bead rule must not override the LCP material")


class _FakeHfss:
    """Enough of an Hfss for the split-body helpers: modeler[name] with a
    bounding_box and/or a volume."""

    def __init__(self, boxes=None, volumes=None):
        boxes, volumes = boxes or {}, volumes or {}

        class _Modeler:
            def __getitem__(_self, name):
                obj = type("_Obj", (), {})()
                if name in boxes:
                    obj.bounding_box = boxes[name]
                if name in volumes:
                    obj.volume = volumes[name]
                return obj

        self.modeler = _Modeler()


class TestSplitBodyHandling(unittest.TestCase):
    """The helpers place_connectors uses to make sense of the three
    imported bodies."""

    def test_bodies_are_sorted_by_the_step_product_names(self):
        from eigen_mode_analyze import model_builder as mb

        got = mb._classify_connector_bodies(
            _FakeHfss(), ["SMP_center_conductor", "SMP_dielectric", "SMP_outer_conductor"]
        )
        self.assertEqual(got, ("SMP_outer_conductor", "SMP_center_conductor", "SMP_dielectric"))

    def test_opaque_names_fall_back_to_volume_order(self):
        """outer ~33 > dielectric ~11 > centre ~1 mm^3."""
        from eigen_mode_analyze import model_builder as mb

        got = mb._classify_connector_bodies(
            _FakeHfss(volumes={"a": 1.29, "b": 33.36, "c": 10.63}), ["a", "b", "c"]
        )
        self.assertEqual(got, ("b", "a", "c"))  # outer, centre, dielectric

    def test_anything_but_three_bodies_is_unclassifiable(self):
        from eigen_mode_analyze import model_builder as mb

        self.assertIsNone(mb._classify_connector_bodies(_FakeHfss(), ["only_one"]))
        self.assertIsNone(mb._classify_connector_bodies(_FakeHfss(), ["a", "b", "c", "d"]))

    def test_combined_bounding_box_is_the_union(self):
        from eigen_mode_analyze import model_builder as mb

        hfss = _FakeHfss(boxes={
            "a": [0.0, -1.0, 0.0, 2.0, 1.0, 3.0],
            "b": [-3.0, 0.0, -0.5, 1.0, 2.0, 1.0],
        })
        self.assertEqual(
            mb._combined_bounding_box(hfss, ["a", "b"]),
            [-3.0, -1.0, -0.5, 2.0, 2.0, 3.0],
        )


class TestOurNumbersStillMatchTheVendorFile(unittest.TestCase):
    """Read the vendor footprint and check nothing has been left out.

    The tests above assert our constants equal the values written in
    this file, which is worth having but could never catch the bug it
    was written after: the land pattern has FIVE copper shapes and only
    four of them are pads. The fifth -- the ground land with the
    circular anti-pad in it -- was missed for exactly that reason, and
    reading pads alone will miss it again.

    So this reads the shipped footprint and accounts for every copper
    shape in it, rather than a chosen subset.
    """

    @classmethod
    def setUpClass(cls):
        import re
        from pathlib import Path

        path = (Path(__file__).resolve().parents[1] / "base_parts" / "altium" /
                "vendor" / "J_Wurth_WR-SMP_60124102122403.kicad_mod")
        cls.path = path
        text = path.read_text(encoding="utf-8")

        cls.pads = []
        for m in re.finditer(
            r'\(pad "(\d)" smd rect \(at ([-0-9.]+) ([-0-9.]+)(?: ([-0-9.]+))?\)'
            r' \(size ([-0-9.]+) ([-0-9.]+)\)', text
        ):
            u, v = float(m.group(2)), float(m.group(3))
            rot = float(m.group(4) or 0.0)
            su, sv = float(m.group(5)), float(m.group(6))
            if abs(rot) % 180 == 90:          # a quarter turn swaps the sizes
                su, sv = sv, su
            cls.pads.append((m.group(1), u, v, su, sv))

        cls.polys = []
        for m in re.finditer(r"\(fp_poly", text):
            i, depth = m.start(), 0
            for j in range(i, len(text)):
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
            block = text[i:j + 1]
            layer = re.search(r'\(layer "([^"]+)"', block).group(1)
            pts = [(float(a), float(b)) for a, b in
                   re.findall(r"\(xy ([-0-9.]+) ([-0-9.]+)\)", block)]
            cls.polys.append((layer, pts))

    def test_the_footprint_is_where_we_say_it_is(self):
        self.assertTrue(self.path.is_file(), f"vendor footprint missing: {self.path}")

    def test_every_pad_is_one_we_model(self):
        ours = {tuple(round(x, 6) for x in p) for p in smp.PADS}
        theirs = {tuple(round(x, 6) for x in p[1:]) for p in self.pads}
        self.assertEqual(theirs, ours)

    def test_the_copper_layer_has_exactly_one_shape_that_is_not_a_pad(self):
        """If the vendor ever adds another, this fails rather than the
        shape quietly not existing on our board."""
        copper = [pts for layer, pts in self.polys if layer == "F.Cu"]
        self.assertEqual(len(copper), 1, "unmodelled copper in the vendor footprint")

    def test_the_ground_land_is_the_square_we_model(self):
        pts = [pts for layer, pts in self.polys if layer == "F.Cu"][0]
        half = smp.GROUND_LAND_HALF
        self.assertAlmostEqual(max(u for u, _v in pts), half, places=6)
        self.assertAlmostEqual(max(v for _u, v in pts), half, places=6)
        self.assertAlmostEqual(min(u for u, _v in pts), -half, places=6)
        self.assertAlmostEqual(min(v for _u, v in pts), -half, places=6)

    def test_the_void_in_it_is_the_radius_we_model(self):
        """The anti-pad. It is drawn as a many-sided polygon, so this
        checks the points that lie on it, not a stated radius."""
        import math

        pts = [pts for layer, pts in self.polys if layer == "F.Cu"][0]
        on_arc = [math.hypot(u, v) for u, v in pts
                  if abs(math.hypot(u, v) - smp.VOID_RADIUS) < 0.05]
        self.assertGreater(len(on_arc), 50, "no circular void found in the ground land")
        for r in on_arc:
            self.assertAlmostEqual(r, smp.VOID_RADIUS, places=2)

    def test_the_signal_pads_escape_slot_is_the_width_we_model(self):
        pts = [pts for layer, pts in self.polys if layer == "F.Cu"][0]
        # Where the slot opens through the land's edge: the only points
        # on that edge that are not its corners.
        half = smp.GROUND_LAND_HALF
        mouth = sorted({round(u, 6) for u, v in pts
                        if abs(abs(round(v, 6)) - half) < 1e-9
                        and abs(u) < half - 1e-9})
        self.assertEqual(mouth, [-smp.SLOT_HALF_U, smp.SLOT_HALF_U])

    def test_the_land_reaches_the_arms_so_the_void_does_not_swallow_them(self):
        """The vendor leaves 0.05 mm between the void and the nearest
        ground arm. Our board cuts the void out of a plane the arms sit
        in, so if that margin ever went negative the arms would be
        floating in the clearance instead of connected to ground."""
        nearest = min(abs(u) - su / 2.0 for _n, u, _v, su, _sv in self.pads
                      if abs(u) > 1.0)
        self.assertGreater(nearest, smp.VOID_RADIUS)
