"""Tests for the SMP land pattern and where the launch sits.

The numbers are Wuerth's, read out of the Altium library shipped with
WR-SMP 60124102122403 -- the same library the generator now places the
footprint from. These tests exist to catch them being edited by hand,
and to pin the placement rule the board depends on.

The vendor file cannot be read directly here the way the KiCad one used
to be: a PcbLib is a compiled compound document and only Altium can
open it. So the last class checks our constants against the committed
transcript that `base_parts/altium/spikes/s3_libcomp.pas` produced by
reading the library inside Altium. Re-run that spike after changing
connector or library revision, and these tests say whether anything
moved.
"""
import unittest

from microwave_package.design import smp


class TestTheVendorNumbers(unittest.TestCase):
    def test_signal_pad_matches_the_vendor_footprint(self):
        u, v, su, sv = smp.SIGNAL_PAD
        self.assertEqual((u, v, su, sv), (0.00, 2.90, 0.48, 1.40))

    def test_four_square_ground_pads_sit_inside_the_land(self):
        """The Altium library gives the ground as four solder points in
        a continuous land, where the KiCad one drew a U of bars. Both
        describe the same flange; this is the one being placed."""
        self.assertEqual(len(smp.GROUND_PADS), 4)
        for u, v, su, sv in smp.GROUND_PADS:
            self.assertEqual((abs(u), abs(v)), (2.0, 2.0))
            self.assertEqual((su, sv), (0.5, 0.5))
            self.assertLess(max(abs(u) + su / 2, abs(v) + sv / 2),
                            smp.GROUND_LAND_HALF,
                            "a ground pad reaches outside the land it sits in")

    def test_the_ground_pads_clear_the_anti_pad(self):
        """They sit in a plane our board cuts the void out of, so a pad
        inside the void would be floating in the clearance rather than
        connected to ground."""
        import math

        for u, v, su, sv in smp.GROUND_PADS:
            nearest = math.hypot(abs(u) - su / 2, abs(v) - sv / 2)
            self.assertGreater(nearest, smp.VOID_RADIUS)

    def test_extents_follow_from_the_pads_and_the_land(self):
        """Unchanged when the source moved from the KiCad footprint to
        the Altium one -- which is why nothing measured from the launch
        had to move with it."""
        self.assertAlmostEqual(smp.OUTER_V, -2.60)
        self.assertAlmostEqual(smp.INNER_V, 3.60)
        self.assertAlmostEqual(smp.LENGTH, 6.20)

    def test_the_outermost_copper_is_the_land_not_a_pad(self):
        """On this footprint no pad reaches as far back as the ground
        land does, so an envelope taken from the pads alone would place
        the connector 0.35 mm too far out."""
        furthest_pad = min(v - sv / 2 for _u, v, _su, sv in smp.PADS)
        self.assertLess(smp.OUTER_V, furthest_pad)
        self.assertAlmostEqual(smp.OUTER_V, -smp.GROUND_LAND_HALF)

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
    """What crosses to the DelphiScript, and what deliberately does not."""

    def test_the_envelope_and_the_anti_pad_reach_the_parameter_file(self):
        """The board around the launch is drawn in Pascal, so it needs
        how far the launch reaches and the shape of the hole in the
        ground -- but it reads them rather than carrying its own copy."""
        params = smp.as_parameters()
        for name in ("smp_sig_v", "smp_outer_v", "smp_inner_v",
                     "smp_land_half", "smp_void_r", "smp_slot_half"):
            self.assertIn(name, params)

    def test_the_pads_do_not_cross(self):
        """The footprint is placed from the vendor library, so sending
        the lands too would be a second copy of the land pattern free to
        disagree with the first."""
        params = smp.as_parameters()
        self.assertFalse(
            [n for n in params if n.endswith(("_su", "_sv", "_u"))],
            "pad geometry should not reach the generator any more",
        )

    def test_the_values_are_the_ones_we_model(self):
        params = smp.as_parameters()
        self.assertAlmostEqual(params["smp_sig_v"], smp.SIGNAL_PAD[1])
        self.assertAlmostEqual(params["smp_land_half"], smp.GROUND_LAND_HALF)
        self.assertAlmostEqual(params["smp_void_r"], smp.VOID_RADIUS)


if __name__ == "__main__":
    unittest.main()


class TestTheConnectorModel(unittest.TestCase):
    """Placing the vendor solid on its launch.

    Worth pinning because a frame error here is invisible in the tree:
    the connector simply sits in the wrong plane, and the model still
    solves.
    """

    def test_the_model_ships_with_the_repo(self):
        from microwave_package import paths

        self.assertTrue(paths.SMP_MODEL_FILE.is_file(),
                        "the split connector STEP should be in base_parts")

    def test_the_split_model_has_three_named_bodies(self):
        """The whole point of the split: HFSS must see outer conductor,
        centre conductor and dielectric as separate solids, not one lump."""
        from microwave_package import paths

        text = paths.SMP_MODEL_FILE.read_text(encoding="utf-8", errors="ignore")
        for product in ("SMP_outer_conductor", "SMP_center_conductor", "SMP_dielectric"):
            self.assertIn(product, text, f"{product} missing from the split STEP")

    def test_the_rotation_matches_the_frame_used_everywhere_else(self):
        """The model has Z up and the assembly has Y up. -90 about X is
        the same mapping Frame applies; disagreeing would put the
        connector in a different plane from the board."""
        from microwave_package.design import copper_geometry as cg

        self.assertAlmostEqual(smp.model_rotation_degrees(), -90.0)

        # Frame sends (u, v, w) to (u, w, -v); a -90 rotation about X
        # sends (x, y, z) to (x, z, -y). Same thing.
        frame = cg.Frame()
        self.assertEqual(frame.point(1.0, 2.0, 3.0), [1.0, 3.0, -2.0])

    def test_the_two_connectors_sit_at_opposite_ends(self):
        from microwave_package.design import copper_geometry as cg

        frame = cg.Frame(x_offset=14.0, z_offset=6.0)
        near = smp.model_offset(frame, 1.381, 13.0, 1.5, -1.0)
        far = smp.model_offset(frame, 1.381, 13.0, 1.5, +1.0)
        self.assertLess(near[0], frame.x_offset)
        self.assertGreater(far[0], frame.x_offset)
        self.assertAlmostEqual(frame.x_offset - near[0], far[0] - frame.x_offset)

    def test_the_connector_sits_on_the_board_surface(self):
        from microwave_package.design import copper_geometry as cg

        frame = cg.Frame(x_offset=14.0, z_offset=6.0)
        offset = smp.model_offset(frame, 1.381, 13.0, 1.5, +1.0)
        self.assertAlmostEqual(offset[1], 1.381, msg="height must be the board top face")

    def test_the_split_bodies_land_on_the_right_material_rules(self):
        """The conductors hit the bare `connector` rule, the bead hits
        `dielectric` first -- and both rules carry no material and no
        boundary, so place_connectors' brass/LCP assignments stand and
        nothing on the connector is a PEC surface."""
        from microwave_package.hfss import model_builder

        def first_rule(name):
            for rule in model_builder.MATERIAL_RULES:
                if rule[0] in name.lower():
                    return rule
            return None

        outer = first_rule("connector_1_outer")
        centre = first_rule("connector_1_center")
        diel = first_rule("connector_1_dielectric")
        self.assertEqual(outer[1], "connector")
        self.assertEqual(centre[1], "connector")
        self.assertEqual(diel[1], "connector_dielectric")
        for rule in (outer, centre, diel):
            self.assertIsNone(rule[2], "place_connectors owns the connector materials")
            self.assertIsNone(rule[3], "the connector carries no perfect-E boundary")


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
        from microwave_package.hfss import model_builder as mb

        got = mb._classify_connector_bodies(
            _FakeHfss(), ["SMP_center_conductor", "SMP_dielectric", "SMP_outer_conductor"]
        )
        self.assertEqual(got, ("SMP_outer_conductor", "SMP_center_conductor", "SMP_dielectric"))

    def test_opaque_names_fall_back_to_volume_order(self):
        """outer ~33 > dielectric ~11 > centre ~1 mm^3."""
        from microwave_package.hfss import model_builder as mb

        got = mb._classify_connector_bodies(
            _FakeHfss(volumes={"a": 1.29, "b": 33.36, "c": 10.63}), ["a", "b", "c"]
        )
        self.assertEqual(got, ("b", "a", "c"))  # outer, centre, dielectric

    def test_anything_but_three_bodies_is_unclassifiable(self):
        from microwave_package.hfss import model_builder as mb

        self.assertIsNone(mb._classify_connector_bodies(_FakeHfss(), ["only_one"]))
        self.assertIsNone(mb._classify_connector_bodies(_FakeHfss(), ["a", "b", "c", "d"]))

    def test_combined_bounding_box_is_the_union(self):
        from microwave_package.hfss import model_builder as mb

        hfss = _FakeHfss(boxes={
            "a": [0.0, -1.0, 0.0, 2.0, 1.0, 3.0],
            "b": [-3.0, 0.0, -0.5, 1.0, 2.0, 1.0],
        })
        self.assertEqual(
            mb._combined_bounding_box(hfss, ["a", "b"]),
            [-3.0, -1.0, -0.5, 2.0, 2.0, 3.0],
        )


def measured(pads, land=None, body="X", name="X"):
    """A footprint file as MeasureFootprint.pas would have written it.

    `pads` are (name, x, y, xsize, ysize, rotation) in some vendor's own
    frame; `land` is a copper outline in the same frame.
    """
    lines = [f"footprint={name}"]
    for pad in pads:
        lines.append("pad|{0}|top|{1}|{2}|{3}|{4}|{5}".format(*pad))
    if land:
        lines.append("copper|" + ",".join(f"{u},{v}" for u, v in land))
    if body:
        lines.append(f"body|{body}|4.1")
    return "\n".join(lines) + "\n"


def square_land(half=2.6, void=2.3, slot=1.0, steps=48, turn=0.0):
    """A ground land like a coax launch carries: a square with a
    circular void and a slot for the centre pin to escape through.

    Drawn canonically -- pin toward +y, so the slot breaks the +y edge
    -- then rigidly rotated by `turn` radians. The rotation has to be
    the SAME one the pads get, or the fixture describes a part whose
    land and pads disagree, which is a different bug from the one being
    tested.
    """
    import math

    h = math.sqrt(max(void ** 2 - slot ** 2, 0.0))
    a1 = math.atan2(h, slot)
    a2 = math.atan2(h, -slot) - 2 * math.pi      # clockwise, under the void

    pts = [(-half, -half), (half, -half), (half, half), (slot, half)]
    for i in range(steps + 1):
        a = a1 + (a2 - a1) * i / steps
        pts.append((void * math.cos(a), void * math.sin(a)))
    pts += [(-slot, half), (-half, half)]

    cos, sin = math.cos(turn), math.sin(turn)
    return [(round(x * cos - y * sin, 6), round(x * sin + y * cos, 6)) for x, y in pts]


class TestOrientingAnyVendorFrame(unittest.TestCase):
    """The part with no second chance.

    Which way a library draws a footprint is nobody's standard. Wuerth
    point the centre pin at -y; the next manufacturer may not. A launch
    placed a half turn out faces the board edge and looks perfect doing
    it, so the frame is taken from pin 1 rather than assumed -- and
    these are the tests of that, because the one footprint on disk
    happens to be drawn the one way.
    """

    # The same part every time, drawn pin-toward-+y, then turned. The
    # ground pad is deliberately off the signal axis so that a mirror --
    # which no test of extents would notice -- shows up as a sign.
    CANONICAL = (
        ("1", 0.0, 2.9, 0.48, 1.4, 0.0),
        ("2", -1.0, 2.0, 0.5, 0.5, 0.0),
    )

    def _derive(self, direction):
        """The same footprint drawn with the centre pin pointing `direction`."""
        import math

        ux, uy = direction
        turn = math.atan2(uy, ux) - math.pi / 2.0
        cos, sin = math.cos(turn), math.sin(turn)
        pads = [
            (name,
             round(x * cos - y * sin, 6), round(x * sin + y * cos, 6),
             sx, sy, (rot + math.degrees(turn)) % 360.0)
            for name, x, y, sx, sy, rot in self.CANONICAL
        ]
        return smp.derive(measured(pads, square_land(turn=turn)))

    def test_a_pin_on_minus_y_comes_out_on_plus_v(self):
        """How Wuerth draw this part."""
        got = self._derive((0.0, -1.0))
        self.assertAlmostEqual(got["signal_pad"][0], 0.0, places=5)
        self.assertAlmostEqual(got["signal_pad"][1], 2.9, places=5)

    def test_a_pin_on_plus_x_comes_out_on_plus_v(self):
        got = self._derive((1.0, 0.0))
        self.assertAlmostEqual(got["signal_pad"][0], 0.0, places=5)
        self.assertAlmostEqual(got["signal_pad"][1], 2.9, places=5)

    def test_a_pin_already_on_plus_y_is_left_alone(self):
        got = self._derive((0.0, 1.0))
        self.assertAlmostEqual(got["signal_pad"][0], 0.0, places=5)
        self.assertAlmostEqual(got["signal_pad"][1], 2.9, places=5)

    def test_every_orientation_gives_the_same_footprint(self):
        """The whole claim, stated once: how the vendor drew it must not
        survive into anything downstream."""
        frames = [(0.0, -1.0), (1.0, 0.0), (-1.0, 0.0), (0.0, 1.0)]
        results = [self._derive(f) for f in frames]
        for other in results[1:]:
            for key in ("signal_pad", "land_half", "void_radius",
                        "slot_half", "outer_v", "inner_v"):
                self.assertAlmostEqual(
                    _flat(other[key]), _flat(results[0][key]), places=4,
                    msg=f"{key} depends on how the library drew the part",
                )

    def test_it_is_a_rotation_not_a_reflection(self):
        """A mirrored footprint is geometrically plausible, solders on
        upside down, and is noticed when the boards arrive. The ground
        pad is deliberately off-axis so its handedness is visible."""
        for direction in ((0.0, -1.0), (1.0, 0.0), (-1.0, 0.0), (0.0, 1.0)):
            got = self._derive(direction)
            u, v, _su, _sv = got["ground_pads"][0]
            # drawn one unit anticlockwise of the pin, and must stay there
            self.assertAlmostEqual(u, -1.0, places=5, msg=f"mirrored at {direction}")
            self.assertAlmostEqual(v, 2.0, places=5, msg=f"mirrored at {direction}")

    def test_a_pad_turned_within_the_footprint_has_its_sizes_swapped(self):
        """xsize/ysize are in the PAD's frame, not the footprint's.

        A ground strap drawn as a 0.3 x 4.0 pad turned a quarter turn --
        which is exactly how the KiCad library draws this part's strap --
        is 4.0 ACROSS the signal axis and 0.3 along it. Read the other
        way round it becomes a 0.3 mm sliver where a 4 mm bar belongs,
        and the launch is built from it without complaint.
        """
        pads = [("1", 0.0, 2.9, 0.48, 1.4, 0.0),
                ("2", 0.0, -2.45, 0.3, 4.0, 90.0)]
        got = smp.derive(measured(pads, square_land()))
        _u, _v, size_u, size_v = got["ground_pads"][0]
        self.assertAlmostEqual(size_u, 4.0, places=5)
        self.assertAlmostEqual(size_v, 0.3, places=5)

    def test_a_pads_own_rotation_is_counted_too(self):
        """A vendor can draw the same land as a rotated pad instead of a
        transposed one, and the two must come out the same."""
        upright = smp.derive(measured([("1", 0.0, 2.9, 0.48, 1.4, 0.0)], square_land()))
        turned = smp.derive(measured([("1", 0.0, 2.9, 1.4, 0.48, 90.0)], square_land()))
        self.assertEqual(upright["signal_pad"], turned["signal_pad"])


def _flat(value):
    """Collapse a tuple to one number so assertAlmostEqual can compare
    whole pads as easily as scalars."""
    if isinstance(value, (tuple, list)):
        return sum((i + 1) * v for i, v in enumerate(value))
    return value


class TestAFootprintThisBoardCannotUse(unittest.TestCase):
    """Refusals, each naming what is wrong with the part.

    These are what the swap procedure leans on: drop in a connector that
    does not suit this board and find out now, by name, rather than from
    the boards.
    """

    def test_a_footprint_with_no_pin_one_is_refused(self):
        with self.assertRaises(smp.FootprintError) as ctx:
            smp.derive(measured([("A", 0.0, 2.9, 0.48, 1.4, 0.0)], square_land()))
        self.assertIn("pin 1", str(ctx.exception).lower().replace("named '1'", "pin 1"))

    def test_two_pads_named_one_are_refused(self):
        pads = [("1", 0.0, 2.9, 0.48, 1.4, 0.0), ("1", 0.0, -2.9, 0.48, 1.4, 0.0)]
        with self.assertRaises(smp.FootprintError):
            smp.derive(measured(pads, square_land()))

    def test_a_pin_one_on_the_origin_is_refused(self):
        """There would be nothing to take the signal direction from."""
        with self.assertRaises(smp.FootprintError) as ctx:
            smp.derive(measured([("1", 0.0, 0.0, 0.48, 1.4, 0.0)], square_land()))
        self.assertIn("origin", str(ctx.exception))

    def test_a_second_copper_shape_is_refused(self):
        """It would silently not exist on the board -- which is exactly
        how the anti-pad was missed the first time."""
        text = measured([("1", 0.0, 2.9, 0.48, 1.4, 0.0)], square_land())
        text += "copper|" + ",".join(f"{u},{v}" for u, v in
                                     [(0, 0), (1, 0), (1, 1), (0, 1)]) + "\n"
        with self.assertRaises(smp.FootprintError) as ctx:
            smp.derive(text)
        self.assertIn("copper shapes", str(ctx.exception))

    def test_a_through_hole_connector_is_refused(self):
        """Its pads live on the multi-layer with a drill, and this
        generator places a footprint's copper without drilling anything
        for it -- so the holes would simply be missing, leaving what
        looks like a perfectly good surface-mount launch.

        Not hypothetical: 60124002122502 in the very library this board
        uses is exactly this part, and it read as having NO PADS AT ALL
        until MeasureFootprint learned to collect the multi-layer.
        """
        pads = [("1", 0.0, -2.9, 1.95, 1.95, 0.0)]
        text = "footprint=X\n" + "".join(
            f"pad|{n}|multi|{x}|{y}|{sx}|{sy}|{r}|1.3\n"
            for n, x, y, sx, sy, r in pads
        )
        with self.assertRaises(smp.FootprintError) as ctx:
            smp.derive(text)
        self.assertIn("through-hole", str(ctx.exception))

    def test_a_footprint_with_no_pads_is_refused(self):
        """Rather than treated as a launch with no lands, which is what
        a layer this measurer does not collect looks like."""
        with self.assertRaises(smp.FootprintError) as ctx:
            smp.derive(measured([], square_land()))
        self.assertIn("no copper pads", str(ctx.exception))

    def test_a_ground_land_with_no_anti_pad_is_refused(self):
        """The void is what holds ground off the centre pin and sets the
        impedance of the transition. A land without one is not a launch."""
        plain = [(-2.6, -2.6), (-2.6, 2.6), (2.6, 2.6), (2.6, -2.6)]
        with self.assertRaises(smp.FootprintError) as ctx:
            smp.derive(measured([("1", 0.0, 2.9, 0.48, 1.4, 0.0)], plain))
        self.assertIn("anti-pad", str(ctx.exception))

    def test_an_oblong_land_is_refused(self):
        """The coplanar planes step around the land as a square."""
        land = [(u * 1.5, v) for u, v in square_land()]
        with self.assertRaises(smp.FootprintError) as ctx:
            smp.derive(measured([("1", 0.0, 2.9, 0.48, 1.4, 0.0)], land))
        self.assertIn("not square", str(ctx.exception))

    def test_a_skewed_pad_is_refused(self):
        """This board draws its launch from axis-aligned lands, so a pad
        at 30 degrees needs code, not just a new library."""
        pads = [("1", 0.0, 2.9, 0.48, 1.4, 0.0), ("2", 2.0, 2.0, 0.5, 0.5, 30.0)]
        with self.assertRaises(smp.FootprintError) as ctx:
            smp.derive(measured(pads, square_land()))
        self.assertIn("degrees", str(ctx.exception))

    def test_a_malformed_record_names_its_line(self):
        with self.assertRaises(smp.FootprintError) as ctx:
            smp.derive("footprint=X\npad|1|top|0|oops|0.48|1.4|0\n")
        self.assertIn("line 2", str(ctx.exception))

    def test_measuring_a_different_footprint_is_refused(self):
        text = measured([("1", 0.0, 2.9, 0.48, 1.4, 0.0)], square_land(), name="OTHER")
        with self.assertRaises(smp.FootprintError) as ctx:
            smp.derive(text, expect="WANTED")
        self.assertIn("WANTED", str(ctx.exception))


class TestTheMeasuredFootprintSuitsThisBoard(unittest.TestCase):
    """The real part, checked for the things that would make it unusable
    rather than against a second copy of its own numbers.

    There is no second copy any more: smp.py derives everything from the
    committed measurement, so asserting they agree would test nothing.
    What is worth asserting is that the footprint which arrived is one
    this board can build a launch around -- which is exactly the question
    when a new connector is dropped in.
    """

    def test_the_measurement_ships_with_the_repo(self):
        from microwave_package import paths

        self.assertTrue(paths.ALTIUM_SMP_FOOTPRINT_FILE.is_file(),
                        f"no measured footprint: {paths.ALTIUM_SMP_FOOTPRINT_FILE}")
        self.assertTrue(paths.ALTIUM_SMP_LIBRARY.is_file(),
                        f"no vendor library: {paths.ALTIUM_SMP_LIBRARY}")

    def test_the_centre_pin_exits_toward_plus_v(self):
        self.assertGreater(smp.SIGNAL_PAD[1], 0)

    def test_the_ground_pads_sit_inside_the_land(self):
        for u, v, su, sv in smp.GROUND_PADS:
            self.assertLess(abs(u) + su / 2, smp.GROUND_LAND_HALF)
            self.assertLess(abs(v) + sv / 2, smp.GROUND_LAND_HALF)

    def test_the_ground_pads_clear_the_anti_pad(self):
        """They sit in a plane our board cuts the void out of, so a pad
        inside the void would be floating in the clearance rather than
        connected to ground."""
        import math

        for u, v, su, sv in smp.GROUND_PADS:
            nearest = math.hypot(max(abs(u) - su / 2, 0.0), max(abs(v) - sv / 2, 0.0))
            self.assertGreater(nearest, smp.VOID_RADIUS)

    def test_the_void_fits_inside_the_land(self):
        self.assertLess(smp.VOID_RADIUS, smp.GROUND_LAND_HALF)

    def test_the_escape_slot_is_narrower_than_the_void(self):
        self.assertLess(smp.SLOT_HALF_U, smp.VOID_RADIUS)

    def test_the_signal_land_is_the_innermost_copper(self):
        """The centre conductor meets the launch there, so nothing may
        reach past it."""
        self.assertAlmostEqual(smp.INNER_V, smp.SIGNAL_PAD[1] + smp.SIGNAL_PAD[3] / 2)

    def test_the_outermost_copper_is_the_land_not_a_pad(self):
        """On this footprint no pad reaches as far back as the ground
        land does, so an envelope taken from the pads alone would place
        the connector 0.35 mm too far out."""
        furthest_pad = min(v - sv / 2 for _u, v, _su, sv in smp.PADS)
        self.assertLess(smp.OUTER_V, furthest_pad)
        self.assertAlmostEqual(smp.OUTER_V, -smp.GROUND_LAND_HALF)

    def test_the_footprint_carries_a_3d_body(self):
        """What makes the placed component show up in Altium's own 3D
        view and STEP export -- the thing four loose pads could not do."""
        self.assertTrue(smp.BODY_IDENTIFIER,
                        "no 3D body: the board will export without a connector on it")
