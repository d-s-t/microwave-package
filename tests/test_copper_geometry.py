"""Tests for reading the Altium layout and rebuilding it in HFSS.

The parser is checked against the real file the generator produced, so
the wire format is pinned to something Altium actually wrote rather than
to a fixture invented here.

The builder is checked against a stand-in modeller. HFSS is not needed
to verify the thing that actually went wrong in practice: which way is
up. Altium works Z-up and the SolidWorks assembly is Y-up, and a model
built in the wrong frame still solves -- it just reports plausible
nonsense with the copper lying in the wrong plane.

Run: python -m unittest discover -s tests
"""
import os
import unittest

from eigen_mode_analyze import copper_geometry as cg

# A trimmed copy of real output from base_parts/altium/ExportCopper.pas.
SAMPLE = """# copper geometry, millimetres, relative to board centre
board=package
pad|top|11.5|0|1.275|1.275
pad|top|-11.5|0|1.275|1.275
via|11.8|-0.67131|0.3|0.15
via|11.8|0.67131|0.3|0.15
track|top|-13|0|13|0|0.2
region|top|-13,0.37131,13,0.37131,13,5,-13,5
region|bottom|-13,-5,13,-5,13,5,-13,5
board=chip
track|top|-3.15|0|3.15|0|0.2
pad|top|-2.85|0|0.3|0.15
"""


class TestParsing(unittest.TestCase):
    def test_splits_records_by_board(self):
        boards = cg.parse(SAMPLE)
        self.assertEqual(sorted(boards), ["chip", "package"])
        self.assertEqual(len(boards["package"]), 7)
        self.assertEqual(len(boards["chip"]), 2)

    def test_reads_each_record_type(self):
        pkg = cg.parse(SAMPLE)["package"]
        self.assertEqual(len(pkg.pads), 2)
        self.assertEqual(len(pkg.vias), 2)
        self.assertEqual(len(pkg.tracks), 1)
        self.assertEqual(len(pkg.regions), 2)

    def test_values_survive_intact(self):
        pkg = cg.parse(SAMPLE)["package"]
        self.assertEqual(pkg.pads[0].x, 11.5)
        self.assertEqual(pkg.pads[0].xsize, 1.275)
        self.assertAlmostEqual(pkg.vias[0].y, -0.67131)
        self.assertEqual(pkg.tracks[0].width, 0.2)

    def test_region_points_come_back_as_pairs(self):
        pts = cg.parse(SAMPLE)["package"].regions[0].points
        self.assertEqual(len(pts), 4)
        self.assertEqual(pts[0], (-13.0, 0.37131))

    def test_layers_are_kept_apart(self):
        pkg = cg.parse(SAMPLE)["package"]
        _, top_p, top_r = pkg.on_layer(cg.TOP)
        _, _, bot_r = pkg.on_layer(cg.BOTTOM)
        self.assertEqual(len(top_r), 1)
        self.assertEqual(len(bot_r), 1)
        self.assertEqual(len(top_p), 2)

    def test_comments_and_blanks_ignored(self):
        self.assertEqual(cg.parse("# nothing\n\n"), {})

    def test_record_before_a_board_line_is_rejected(self):
        with self.assertRaises(cg.CopperParseError):
            cg.parse("via|1|2|0.3|0.15\n")

    def test_malformed_record_names_its_line(self):
        with self.assertRaises(cg.CopperParseError) as ctx:
            cg.parse("board=x\nvia|1|2|oops|0.15\n")
        self.assertIn("line 2", str(ctx.exception))

    def test_unknown_record_type_is_rejected(self):
        with self.assertRaises(cg.CopperParseError):
            cg.parse("board=x\nsomething|1|2\n")

    def test_region_with_odd_coordinate_count_is_rejected(self):
        with self.assertRaises(cg.CopperParseError):
            cg.parse("board=x\nregion|top|1,2,3,4,5\n")


class TestFrame(unittest.TestCase):
    """The axis mapping. This is the bug that produced a model which
    solved and was wrong: Altium is Z-up, the assembly is Y-up."""

    FRAME = cg.Frame(x_offset=14.0, z_offset=6.0)

    def test_up_becomes_y_not_z(self):
        x, y, z = self.FRAME.point(0.0, 0.0, 1.5)
        self.assertEqual(y, 1.5, "the board normal must land on Y")

    def test_feedline_runs_along_x(self):
        x, _, _ = self.FRAME.point(5.0, 0.0, 0.0)
        self.assertEqual(x, 5.0 + 14.0)

    def test_across_the_board_becomes_negative_z(self):
        """u x w = -v, so the third axis is negated. Dropping the sign
        mirrors the board, which no test of extents would notice."""
        _, _, z = self.FRAME.point(0.0, 2.0, 0.0)
        self.assertEqual(z, -2.0 + 6.0)

    def test_the_mapping_is_right_handed(self):
        """A wrong-handed frame mirrors the layout -- geometrically
        plausible, and wrong."""
        o = self.FRAME.point(0, 0, 0)
        du = [a - b for a, b in zip(self.FRAME.point(1, 0, 0), o)]
        dv = [a - b for a, b in zip(self.FRAME.point(0, 1, 0), o)]
        dw = [a - b for a, b in zip(self.FRAME.point(0, 0, 1), o)]
        cross = [
            du[1] * dv[2] - du[2] * dv[1],
            du[2] * dv[0] - du[0] * dv[2],
            du[0] * dv[1] - du[1] * dv[0],
        ]
        self.assertEqual(cross, dw, "u x v must give w")

    def test_board_centre_lands_at_the_box_centre(self):
        """The assembly origin is a box corner, so a board centred on the
        Altium origin has to be carried half a box in."""
        x, _, z = self.FRAME.point(0.0, 0.0, 0.0)
        self.assertEqual((x, z), (14.0, 6.0))

    def test_box_returns_positive_sizes_whatever_the_order(self):
        o1, s1 = self.FRAME.box(-13, -5, 13, 5, 1.0, 0.381)
        o2, s2 = self.FRAME.box(13, 5, -13, -5, 1.0, 0.381)
        self.assertEqual(s1, s2)
        self.assertEqual(o1, o2)
        self.assertTrue(all(v > 0 for v in s1))

    def test_box_spans_the_right_axes(self):
        _, sizes = self.FRAME.box(-13, -5, 13, 5, 1.0, 0.381)
        self.assertAlmostEqual(sizes[0], 26.0)   # along the feedline -> X
        self.assertAlmostEqual(sizes[1], 0.381)  # thickness -> Y
        self.assertAlmostEqual(sizes[2], 10.0)   # across -> Z


class FakeSolid:
    def __init__(self, name):
        self.name = name


class FakeModeler:
    """Records what would have been created, instead of creating it."""

    def __init__(self):
        self.boxes = []
        self.cylinders = []
        self.polylines = []
        self.thickened = []
        self.unites = []
        self.subtractions = []
        self._n = 0
        self._by_name = {}

    def _make(self, kind):
        self._n += 1
        solid = FakeSolid(f"{kind}{self._n}")
        self._by_name[solid.name] = solid
        return solid

    def create_box(self, origin, sizes, **kw):
        self.boxes.append((list(origin), list(sizes)))
        return self._make("box")

    def create_cylinder(self, orientation, origin, radius, height, **kw):
        self.cylinders.append((orientation, list(origin), radius, height))
        return self._make("cyl")

    def create_polyline(self, points, **kw):
        self.polylines.append([list(p) for p in points])
        return self._make("poly")

    def thicken_sheet(self, sheet, height, **kw):
        self.thickened.append((sheet.name, height))

    def unite(self, parts):
        self.unites.append(list(parts))

    def subtract(self, target, tools, keep_originals=False):
        self.subtractions.append((target, list(tools), keep_originals))

    def __getitem__(self, name):
        return self._by_name[name]


FRAME = cg.Frame(x_offset=14.0, z_offset=6.0)
STACK = cg.Stack(
    pcb_bottom=1.0, pcb_top=1.381,
    chip_bottom=1.0, chip_top=1.381,
    copper_thickness=0.035, chip_metal_thickness=0.0002,
)


class TestSubstrates(unittest.TestCase):
    def test_names_are_the_ones_the_material_rules_expect(self):
        modeler = FakeModeler()
        made = cg.build_substrates(modeler, FRAME, STACK, 13, 5, 3.15, 3.15,
                                   log=lambda _m: None)
        self.assertEqual(made, ["pcb_substrate", "chip_substrate"])

    def test_the_pcb_is_pocketed_for_the_die(self):
        """The die sits IN the board, not on it -- so the pocket has to
        be cut, and cut for keeps."""
        modeler = FakeModeler()
        cg.build_substrates(modeler, FRAME, STACK, 13, 5, 3.15, 3.15,
                            log=lambda _m: None)
        self.assertEqual(len(modeler.subtractions), 1)
        target, _tools, keep = modeler.subtractions[0]
        self.assertEqual(target, "pcb_substrate")
        self.assertFalse(keep, "the pocket must actually be removed")

    def test_die_and_board_finish_flush(self):
        modeler = FakeModeler()
        cg.build_substrates(modeler, FRAME, STACK, 13, 5, 3.15, 3.15,
                            log=lambda _m: None)
        tops = [o[1] + s[1] for o, s in modeler.boxes]
        self.assertTrue(all(abs(t - STACK.pcb_top) < 1e-9 for t in tops),
                        f"every top face should be flush at {STACK.pcb_top}, got {tops}")

    def test_substrate_is_the_right_size_on_the_right_axes(self):
        modeler = FakeModeler()
        cg.build_substrates(modeler, FRAME, STACK, 13, 5, 3.15, 3.15,
                            log=lambda _m: None)
        _, sizes = modeler.boxes[0]
        self.assertAlmostEqual(sizes[0], 26.0)
        self.assertAlmostEqual(sizes[1], 0.381)
        self.assertAlmostEqual(sizes[2], 10.0)


class TestBuildingCopper(unittest.TestCase):
    def _build(self, board="package", prefix="pcb"):
        modeler = FakeModeler()
        created = cg.build(
            modeler, cg.parse(SAMPLE)[board], FRAME,
            top_face=STACK.pcb_top, bottom_face=STACK.pcb_bottom,
            thickness=STACK.copper_thickness, prefix=prefix, log=lambda _m: None,
        )
        return modeler, created

    def test_names_match_what_the_material_rules_expect(self):
        from eigen_mode_analyze import model_builder

        _, created = self._build()
        self.assertIn("pcb_copper_top", created)
        self.assertIn("pcb_copper_bottom", created)
        self.assertIn("pcb_vias", created)
        patterns = [rule[0] for rule in model_builder.MATERIAL_RULES]
        for name in created:
            self.assertTrue(any(p in name for p in patterns),
                            f"{name} matches no material rule")

    def test_vias_run_along_the_up_axis(self):
        """Y, not Z. A via along Z would lie in the board plane."""
        modeler, _ = self._build()
        vias = [c for c in modeler.cylinders if c[2] == 0.15]
        self.assertEqual(len(vias), 2)
        self.assertTrue(all(c[0] == "Y" for c in vias))

    def test_vias_span_the_substrate(self):
        modeler, _ = self._build()
        via = [c for c in modeler.cylinders if c[2] == 0.15][0]
        self.assertAlmostEqual(via[1][1], STACK.pcb_bottom)
        self.assertAlmostEqual(via[3], STACK.pcb_top - STACK.pcb_bottom)

    def test_top_copper_sits_on_the_board_face(self):
        modeler, _ = self._build()
        ys = {round(p[1], 6) for path in modeler.polylines for p in path}
        self.assertIn(round(STACK.pcb_top, 6), ys)

    def test_bottom_copper_grows_away_from_the_board(self):
        modeler, _ = self._build()
        heights = [h for _n, h in modeler.thickened]
        self.assertTrue(any(h > 0 for h in heights))
        self.assertTrue(any(h < 0 for h in heights))

    def test_track_gets_round_caps(self):
        modeler, _ = self._build(board="chip", prefix="chip")
        caps = [c for c in modeler.cylinders if c[2] == 0.1]
        self.assertEqual(len(caps), 2)

    def test_everything_on_a_layer_becomes_one_body(self):
        modeler, created = self._build()
        self.assertEqual(len(created["pcb_copper_top"]), 1)
        self.assertTrue(modeler.unites)

    def test_a_board_with_no_vias_makes_no_via_body(self):
        _, created = self._build(board="chip", prefix="chip")
        self.assertNotIn("chip_vias", created)


class TestWireBonds(unittest.TestCase):
    SPEC = cg.BondSpec(
        u_start=-4.15, u_end=-2.85, w_start=1.381, w_end=1.381,
        count=3, pitch=0.4, height=0.2, diameter=0.025,
    )

    def test_path_starts_and_ends_on_the_pads(self):
        pts = cg.bond_path(self.SPEC, FRAME, v=0.0)
        self.assertAlmostEqual(pts[0][0], -4.15 + 14.0)
        self.assertAlmostEqual(pts[0][1], 1.381)
        self.assertAlmostEqual(pts[-1][0], -2.85 + 14.0)

    def test_loop_rises_above_both_ends(self):
        """A straight wire would clip the die edge."""
        pts = cg.bond_path(self.SPEC, FRAME, v=0.0)
        self.assertGreater(max(p[1] for p in pts), self.SPEC.w_start)

    def test_the_loop_rises_along_y(self):
        """The apex must be in height, not sideways."""
        pts = cg.bond_path(self.SPEC, FRAME, v=0.0)
        self.assertEqual(len({round(p[2], 9) for p in pts}), 1,
                         "a bond at fixed v must not wander across the board")

    def test_bonds_spread_symmetrically_across_the_board(self):
        modeler = FakeModeler()
        cg.build_wirebonds(modeler, self.SPEC, FRAME, log=lambda _m: None)
        zs = sorted({round(p[2], 6) for path in modeler.polylines for p in path})
        self.assertEqual(zs, [6.0 - 0.4, 6.0, 6.0 + 0.4])

    def test_one_wire_per_bond_united_under_the_rule_name(self):
        modeler = FakeModeler()
        created = cg.build_wirebonds(modeler, self.SPEC, FRAME, log=lambda _m: None)
        self.assertEqual(len(modeler.polylines), 3)
        self.assertEqual(created, ["wirebond"])

    def test_no_bonds_requested_makes_nothing(self):
        modeler = FakeModeler()
        spec = cg.BondSpec(**{**self.SPEC.__dict__, "count": 0})
        self.assertEqual(cg.build_wirebonds(modeler, spec, FRAME, log=lambda _m: None), [])


class TestAgainstRealExport(unittest.TestCase):
    PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "base_parts", "altium", "generated", "copper.txt",
    )

    def test_real_file_parses_and_has_both_boards(self):
        if not os.path.isfile(self.PATH):
            self.skipTest("copper.txt not present -- run the Altium generator first")
        boards = cg.read_file(self.PATH)
        self.assertIn("package", boards)
        self.assertIn("chip", boards)
        self.assertGreater(len(boards["package"].vias), 0)


if __name__ == "__main__":
    unittest.main()


class TestClearChipArea(unittest.TestCase):
    """The board is pocketed for the die, so copper must not cross that
    footprint -- it would be hanging over a hole, or trapped under the
    die where the real board has none. Either way it is a stray
    conductor exactly where the fields are strongest."""

    def _clear(self, targets=("pcb_copper_top", "pcb_copper_bottom")):
        modeler = FakeModeler()
        cg.clear_chip_area(modeler, FRAME, STACK, 3.15, 3.15, targets,
                           log=lambda _m: None)
        return modeler

    def test_the_cut_covers_the_die_footprint(self):
        modeler = self._clear()
        _origin, sizes = modeler.boxes[0]
        self.assertAlmostEqual(sizes[0], 6.3)
        self.assertAlmostEqual(sizes[2], 6.3)

    def test_the_cut_spans_past_both_copper_layers(self):
        """It has to reach above the top copper and below the bottom
        copper, or one layer survives the cut."""
        modeler = self._clear()
        origin, sizes = modeler.boxes[0]
        low, high = origin[1], origin[1] + sizes[1]
        self.assertLess(low, STACK.pcb_bottom - STACK.copper_thickness)
        self.assertGreater(high, STACK.pcb_top + STACK.copper_thickness)

    def test_both_layers_are_cut_together(self):
        modeler = self._clear()
        target, tools, keep = modeler.subtractions[0]
        self.assertEqual(target, ["pcb_copper_top", "pcb_copper_bottom"])
        self.assertEqual(tools, ["chip_area_cut"])
        self.assertFalse(keep, "the cutting tool must be consumed")

    def test_the_tool_is_named_not_left_anonymous(self):
        """An unnamed leftover shows up in the tree as NewObject_* with
        no material, which is how a stray body hides in plain sight."""
        modeler = self._clear()
        self.assertEqual(modeler[modeler.boxes and "box1"].name, "chip_area_cut")

    def test_nothing_to_cut_is_a_no_op(self):
        modeler = FakeModeler()
        cg.clear_chip_area(modeler, FRAME, STACK, 3.15, 3.15, [], log=lambda _m: None)
        self.assertEqual(modeler.boxes, [])
        self.assertEqual(modeler.subtractions, [])


class TestWireBondNaming(unittest.TestCase):
    """Each row of bonds needs its own name.

    HFSS will not take a duplicate: it invents NewObject_something
    instead, which matches no material rule and lands in the model as
    vacuum. That is exactly how one of the two bond rows went missing.
    """

    SPEC = cg.BondSpec(u_start=-4.15, u_end=-2.85, w_start=1.381, w_end=1.381,
                       count=3, pitch=0.4, height=0.2, diameter=0.025)

    def test_the_name_is_used(self):
        modeler = FakeModeler()
        made = cg.build_wirebonds(modeler, self.SPEC, FRAME, name="wirebond_a",
                                  log=lambda _m: None)
        self.assertEqual(made, ["wirebond_a"])

    def test_two_rows_get_different_names(self):
        modeler = FakeModeler()
        a = cg.build_wirebonds(modeler, self.SPEC, FRAME, name="wirebond_a", log=lambda _m: None)
        b = cg.build_wirebonds(modeler, self.SPEC, FRAME, name="wirebond_b", log=lambda _m: None)
        self.assertNotEqual(a, b)

    def test_both_names_still_match_the_material_rule(self):
        from eigen_mode_analyze import model_builder

        patterns = [rule[0] for rule in model_builder.MATERIAL_RULES]
        for name in ("wirebond_a", "wirebond_b"):
            self.assertTrue(any(p in name for p in patterns), name)


class TestDrillVias(unittest.TestCase):
    def test_the_board_is_drilled_and_the_barrels_kept(self):
        """A barrel and the board cannot share a volume. It is the board
        that gets the hole; the barrels are the conductor being
        modelled."""
        modeler = FakeModeler()
        cg.drill_vias(modeler, "pcb_substrate", "pcb_vias", log=lambda _m: None)
        target, tools, keep = modeler.subtractions[0]
        self.assertEqual(target, "pcb_substrate")
        self.assertEqual(tools, ["pcb_vias"])
        self.assertTrue(keep, "the via barrels must survive the drilling")

    def test_a_failure_is_loud(self):
        class Failing(FakeModeler):
            def subtract(self, *a, **k):
                raise RuntimeError("nope")

        messages = []
        cg.drill_vias(Failing(), "pcb_substrate", "pcb_vias", log=messages.append)
        self.assertTrue(any("WARNING" in m for m in messages))
        self.assertTrue(any("overlap" in m for m in messages))


class TestExportedLayoutIsSane(unittest.TestCase):
    """Geometric facts about the real generated board.

    These are the failures that only showed up once the model was looked
    at in 3D, so they are checked against the actual export rather than a
    fixture.
    """

    PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "base_parts", "altium", "generated", "copper.txt",
    )

    def setUp(self):
        if not os.path.isfile(self.PATH):
            self.skipTest("copper.txt not present -- run the Altium generator first")
        from eigen_mode_analyze import altium_generation as ag, equations as eq, paths, smp

        self.smp = smp
        self.v = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
        self.pkg = cg.read_file(self.PATH)["package"]
        self.trace = self.v["cpw_trace_width"]
        self.gap_edge = self.trace / 2 + ag.build_params(self.v)["pcb_cpw_gap"]
        self.half_l = self.v["pcb_length"] / 2
        self.launch_x = smp.launch_centre(self.half_l, self.v["port_space"])
        self.launch_inner = smp.launch_inner_edge(self.half_l, self.v["port_space"])

    def _launch_pads(self):
        """Everything outboard of where the launch begins.

        The connector's own grounds are allowed to cross the centre line
        -- the strap exists to short the two coplanar grounds together
        behind the launch. Nothing inboard of it may.
        """
        return [p for p in self.pkg.pads if abs(p.x) >= self.launch_inner - 1e-6]

    def test_the_centre_conductor_stops_at_the_launch(self):
        """Running it further is what let the connector land bridge the
        signal into both grounds."""
        track = self.pkg.tracks[0]
        self.assertAlmostEqual(abs(track.x2), self.launch_x, places=6)
        self.assertLess(abs(track.x2), self.half_l)

    def test_the_trace_stops_before_the_ground_strap(self):
        """The strap joins the two ground sides. The signal reaching it
        would be a dead short."""
        track = self.pkg.tracks[0]
        strap = max(self.pkg.pads, key=lambda p: p.x)
        self.assertGreater(strap.x - strap.xsize / 2, track.x2)

    def test_the_launch_lands_port_space_from_the_board_edge(self):
        outer = max(p.x + p.xsize / 2 for p in self.pkg.pads)
        self.assertAlmostEqual(outer, self.half_l - self.v["port_space"], places=6)

    def test_the_signal_land_stays_inside_the_gap(self):
        """It is wider than the trace, but must still clear the grounds."""
        lands = [p for p in self.pkg.pads if abs(abs(p.x) - self.launch_x) < 1e-6]
        self.assertTrue(lands, "no signal land at the launch centre")
        for pad in lands:
            self.assertLess(pad.ysize / 2, self.gap_edge)

    def test_no_pad_inboard_of_the_launch_intrudes_into_the_gap(self):
        """The bond fingers. A pad straddling the gap edge is metal the
        real board does not have, in the one place the field is most
        sensitive to it."""
        launch = {id(p) for p in self._launch_pads()}
        checked = 0
        for pad in self.pkg.pads:
            if id(pad) in launch:
                continue
            checked += 1
            inner = abs(pad.y) - pad.ysize / 2
            outer = abs(pad.y) + pad.ysize / 2
            if outer <= self.trace / 2 + 1e-9:
                continue          # the signal bond, on the centre conductor
            self.assertGreaterEqual(
                inner, self.gap_edge - 1e-6,
                f"pad at y={pad.y} reaches {inner:.4f}, inside the gap edge "
                f"{self.gap_edge:.4f}",
            )
        self.assertGreater(checked, 0, "no inboard pads were actually checked")
