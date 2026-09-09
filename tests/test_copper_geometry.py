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


class TestStackCopperTop(unittest.TestCase):
    """The board's actual top surface is the copper, not the substrate
    it sits on. A connector or a bond wire that lands on pcb_top/
    chip_top instead sinks one plating thickness into the pad -- this is
    what an interference check calls an intersection."""

    def test_pcb_copper_top_is_above_the_substrate(self):
        self.assertAlmostEqual(STACK.pcb_copper_top, 1.381 + 0.035)
        self.assertGreater(STACK.pcb_copper_top, STACK.pcb_top)

    def test_chip_copper_top_is_above_the_substrate(self):
        self.assertAlmostEqual(STACK.chip_copper_top, 1.381 + 0.0002)
        self.assertGreater(STACK.chip_copper_top, STACK.chip_top)

    def test_zero_thickness_copper_leaves_the_surface_unchanged(self):
        bare = cg.Stack(pcb_bottom=1.0, pcb_top=1.381, chip_bottom=1.0,
                        chip_top=1.381, copper_thickness=0.0, chip_metal_thickness=0.0)
        self.assertEqual(bare.pcb_copper_top, bare.pcb_top)


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

    def test_each_via_is_a_hollow_barrel_with_the_drill_hole_clear(self):
        """A plated through-hole, not a solid post: copper out to the via
        radius, with `hole` diameter of clear space bored down the middle
        and taken back out of the barrel."""
        modeler, _ = self._build()
        barrels = [c for c in modeler.cylinders if abs(c[2] - 0.15) < 1e-9]
        bores = [c for c in modeler.cylinders if abs(c[2] - 0.075) < 1e-9]
        self.assertEqual(len(barrels), 2, "one barrel OD per via")
        self.assertEqual(len(bores), 2, "one drilled bore per via")
        self.assertTrue(all(c[0] == "Y" for c in barrels + bores))
        self.assertTrue(
            any(keep is False for _t, _tools, keep in modeler.subtractions),
            "the bore must be subtracted from the barrel",
        )

    def test_the_bore_passes_clear_through_the_barrel(self):
        """Proud of both ends, so no skin of copper is left across the
        mouth by a boolean that stopped exactly flush."""
        modeler, _ = self._build()
        barrel = [c for c in modeler.cylinders if abs(c[2] - 0.15) < 1e-9][0]
        bore = [c for c in modeler.cylinders if abs(c[2] - 0.075) < 1e-9][0]
        self.assertLess(bore[1][1], barrel[1][1], "bore starts below the barrel")
        self.assertGreater(
            bore[1][1] + bore[3], barrel[1][1] + barrel[3],
            "bore ends above the barrel",
        )

    def test_a_filled_via_stays_solid(self):
        """`hole` = 0 is a filled or tented via -- no bore to cut."""
        modeler = FakeModeler()
        board = cg.BoardCopper(name="package", vias=[cg.Via(1.0, 2.0, 0.3, 0.0)])
        cg.build(modeler, board, FRAME, top_face=STACK.pcb_top,
                 bottom_face=STACK.pcb_bottom, thickness=STACK.copper_thickness,
                 prefix="pcb", log=lambda _m: None)
        self.assertEqual(len(modeler.cylinders), 1, "just the solid post")
        self.assertEqual(modeler.subtractions, [])

    def test_top_copper_sits_on_the_board_face(self):
        """Regions and rectangular pads are now boxes (create_box), not
        thickened sheets -- thicken_sheet's extrusion direction follows
        the sheet's winding, which this code does not control, so
        anything axis-aligned is built as an unambiguous box instead."""
        modeler, _ = self._build()
        origins_y = {round(o[1], 6) for o, _s in modeler.boxes}
        self.assertIn(round(STACK.pcb_top, 6), origins_y)

    def test_bottom_copper_grows_away_from_the_board(self):
        modeler, _ = self._build()
        tops = {round(o[1] + s[1], 6) for o, s in modeler.boxes}
        origins_y = {round(o[1], 6) for o, s in modeler.boxes}
        # Top layer starts exactly at the board face and grows up from
        # there; bottom layer ends exactly at it, having grown down.
        self.assertIn(round(STACK.pcb_top, 6), origins_y)
        self.assertIn(round(STACK.pcb_bottom, 6), tops)

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


class TestBondWires(unittest.TestCase):
    """Wires are built from the same file Altium places the pads from,
    so a wire cannot land beside its pad."""

    def _bonds(self):
        from eigen_mode_analyze.bonds import Bond

        return [
            Bond("signal", -4.15, -0.05, -2.85, -0.05),
            Bond("signal", -4.15, 0.05, -2.85, 0.05),
            Bond("ground", -4.15, -0.60, -2.85, -0.60),
        ]

    def _build(self, height=0.2):
        modeler = FakeModeler()
        made = cg.build_bond_wires(modeler, self._bonds(), FRAME,
                                   pcb_top=1.381, chip_top=1.381,
                                   height=height, diameter=0.025,
                                   log=lambda _m: None)
        return modeler, made

    def test_one_wire_per_bond(self):
        modeler, _ = self._build()
        self.assertEqual(len(modeler.polylines), 3)

    def test_they_become_one_named_body(self):
        modeler, made = self._build()
        self.assertEqual(made, ["wirebond"])
        self.assertTrue(modeler.unites)

    def test_a_wire_starts_and_ends_on_its_pads(self):
        from eigen_mode_analyze.bonds import Bond

        bond = Bond("signal", -4.15, -0.05, -2.85, -0.05)
        path = cg.wire_path(bond, FRAME, 1.381, 1.381, 0.2)
        self.assertEqual(path[0], FRAME.point(bond.pcb_x, bond.pcb_y, 1.381))
        self.assertEqual(path[-1], FRAME.point(bond.chip_x, bond.chip_y, 1.381))

    def test_build_bond_wires_raises_the_path_by_one_radius(self):
        """The caller passes the copper's own top surface as pcb_top/
        chip_top. A tube CENTRED there has half its cross-section sunk
        into the pad and the substrate below it -- which an interference
        check flags as an intersection even though the wire is only
        doing what a real bond does, resting on the pad. Raising the
        centreline by one radius puts the tube's underside, not its
        centre, at the surface."""
        modeler = FakeModeler()
        pcb_top = 1.381
        diameter = 0.025
        cg.build_bond_wires(modeler, self._bonds(), FRAME, pcb_top=pcb_top,
                            chip_top=pcb_top, height=0.2, diameter=diameter,
                            log=lambda _m: None)
        lowest = min(p[1] for path in modeler.polylines for p in path)
        self.assertAlmostEqual(lowest, pcb_top + diameter / 2.0, places=9)
        self.assertGreater(lowest, pcb_top, "the tube must not dip to the bare pad surface")

    def test_the_loop_rises_above_both_ends(self):
        from eigen_mode_analyze.bonds import Bond

        path = cg.wire_path(Bond("signal", -4.15, 0, -2.85, 0), FRAME, 1.381, 1.381, 0.2)
        self.assertGreater(max(p[1] for p in path), 1.381)

    def test_the_apex_follows_bond_height(self):
        from eigen_mode_analyze.bonds import Bond

        low = cg.wire_path(Bond("signal", -4, 0, -3, 0), FRAME, 1.381, 1.381, 0.1)
        high = cg.wire_path(Bond("signal", -4, 0, -3, 0), FRAME, 1.381, 1.381, 0.4)
        self.assertGreater(max(p[1] for p in high), max(p[1] for p in low))

    def test_the_loop_rises_in_height_not_sideways(self):
        from eigen_mode_analyze.bonds import Bond

        path = cg.wire_path(Bond("ground", -4.15, 0.6, -2.85, 0.6), FRAME, 1.381, 1.381, 0.2)
        self.assertEqual(len({round(p[2], 9) for p in path}), 1)

    def test_no_bonds_makes_nothing(self):
        modeler = FakeModeler()
        self.assertEqual(
            cg.build_bond_wires(modeler, [], FRAME, 1.381, 1.381, 0.2, 0.025,
                                log=lambda _m: None),
            [],
        )


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

    def test_the_cut_is_wider_than_the_die_footprint(self):
        """A shade wider on purpose, the way a real cutout carries a fab
        keep-out: the die's own copper runs exactly to the die edge, and
        an exactly-coincident cut boundary shares a face with it, which
        an interference check flags even though nothing overlaps."""
        modeler = self._clear()
        _origin, sizes = modeler.boxes[0]
        margin = max(STACK.copper_thickness, STACK.chip_metal_thickness) * 2.0
        self.assertAlmostEqual(sizes[0], 6.3 + 2 * margin)
        self.assertAlmostEqual(sizes[2], 6.3 + 2 * margin)
        self.assertGreater(sizes[0], 6.3)

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
    """HFSS will not take a duplicate name: it invents
    NewObject_something instead, which matches no material rule and
    lands in the model as vacuum."""

    def test_the_name_is_used(self):
        from eigen_mode_analyze.bonds import Bond

        modeler = FakeModeler()
        made = cg.build_bond_wires(modeler, [Bond("ground", -4, 0.6, -3, 0.6)],
                                   FRAME, 1.381, 1.381, 0.2, 0.025,
                                   name="wirebond_a", log=lambda _m: None)
        self.assertEqual(made, ["wirebond_a"])

    def test_the_default_name_matches_the_material_rule(self):
        from eigen_mode_analyze import model_builder

        patterns = [rule[0] for rule in model_builder.MATERIAL_RULES]
        self.assertTrue(any(p in "wirebond" for p in patterns))


class TestDrillVias(unittest.TestCase):
    VIAS = [cg.Via(11.8, -0.67131, 0.3, 0.15), cg.Via(11.8, 0.67131, 0.3, 0.15)]

    def _drill(self, modeler=None):
        modeler = modeler if modeler is not None else FakeModeler()
        cg.drill_vias(modeler, "pcb_substrate", self.VIAS, FRAME,
                      top_face=STACK.pcb_top, bottom_face=STACK.pcb_bottom,
                      log=lambda _m: None)
        return modeler

    def test_the_board_loses_the_whole_footprint_with_its_own_tool(self):
        """The barrels are hollow, so drilling with the barrels
        themselves would leave a dielectric plug where the hole belongs.
        The drill is a separate solid tool at the via's full outer
        radius, and it is consumed by the cut."""
        modeler = self._drill()
        drills = [c for c in modeler.cylinders if abs(c[2] - 0.15) < 1e-9]
        self.assertEqual(len(drills), 2, "one solid drill per via, at the outer radius")
        target, tools, keep = modeler.subtractions[-1]
        self.assertEqual(target, "pcb_substrate")
        self.assertEqual(tools, ["via_drill_tool"])
        self.assertFalse(keep, "the drill tool must be consumed by the cut")

    def test_the_hole_runs_right_through_the_board(self):
        """A shade proud of both faces, so nothing is left as a skin of
        dielectric across the via mouth."""
        modeler = self._drill()
        drill = [c for c in modeler.cylinders if abs(c[2] - 0.15) < 1e-9][0]
        low = drill[1][1]
        high = drill[1][1] + drill[3]
        self.assertLess(low, STACK.pcb_bottom)
        self.assertGreater(high, STACK.pcb_top)

    def test_nothing_to_drill_is_a_no_op(self):
        modeler = FakeModeler()
        cg.drill_vias(modeler, "pcb_substrate", [], FRAME,
                      top_face=STACK.pcb_top, bottom_face=STACK.pcb_bottom,
                      log=lambda _m: None)
        self.assertEqual(modeler.cylinders, [])
        self.assertEqual(modeler.subtractions, [])

    def test_a_failure_is_loud(self):
        class Failing(FakeModeler):
            def subtract(self, *a, **k):
                raise RuntimeError("nope")

        messages = []
        cg.drill_vias(Failing(), "pcb_substrate", self.VIAS, FRAME,
                      top_face=STACK.pcb_top, bottom_face=STACK.pcb_bottom,
                      log=messages.append)
        self.assertTrue(any("WARNING" in m for m in messages))
        self.assertTrue(any("overlap" in m for m in messages))


class TestOpenViaHoles(unittest.TestCase):
    """The barrel is bored and the substrate is drilled, but the lands
    and planes on the two layers are still solid over the via. A real
    plated through-hole is open through the annular ring on every layer,
    not capped by a copper disc at each end."""

    COPPERS = ["pcb_copper_top", "pcb_copper_bottom"]
    VIAS = [cg.Via(11.8, -0.67131, 0.3, 0.15), cg.Via(11.8, 0.67131, 0.3, 0.15)]

    def _open(self, modeler=None, vias=None, coppers=None):
        modeler = modeler if modeler is not None else FakeModeler()
        cg.open_via_holes(
            modeler, self.COPPERS if coppers is None else coppers,
            self.VIAS if vias is None else vias, FRAME,
            top_face=STACK.pcb_top, bottom_face=STACK.pcb_bottom,
            copper_thickness=STACK.copper_thickness, log=lambda _m: None,
        )
        return modeler

    def test_only_the_drill_diameter_comes_out_not_the_land(self):
        """Cutting the barrel OD would sink an anti-pad into every plane
        the board does not have. The hole is `via.hole` across, so the
        annular ring survives."""
        modeler = self._open()
        radii = {round(c[2], 6) for c in modeler.cylinders}
        self.assertEqual(radii, {0.075}, "every tool is hole/2, never diameter/2")

    def test_both_layers_lose_the_hole_together_and_the_tool_is_consumed(self):
        modeler = self._open()
        target, tools, keep = modeler.subtractions[-1]
        self.assertEqual(target, self.COPPERS)
        self.assertEqual(tools, ["via_hole_tool"])
        self.assertFalse(keep, "the tool must be consumed by the cut")

    def test_the_hole_reaches_past_both_copper_layers(self):
        """It has to start below the bottom copper and end above the top
        copper, or one layer keeps its cap."""
        modeler = self._open()
        tool = modeler.cylinders[0]
        low = tool[1][1]
        high = tool[1][1] + tool[3]
        self.assertLess(low, STACK.pcb_bottom - STACK.copper_thickness)
        self.assertGreater(high, STACK.pcb_top + STACK.copper_thickness)

    def test_a_filled_via_keeps_its_copper(self):
        """`hole` = 0 is a filled or tented via -- solid copper over it,
        matching the solid post `_via_barrel` leaves."""
        modeler = self._open(vias=[cg.Via(1.0, 2.0, 0.3, 0.0)])
        self.assertEqual(modeler.cylinders, [])
        self.assertEqual(modeler.subtractions, [])

    def test_no_copper_on_a_layer_is_a_no_op(self):
        modeler = self._open(coppers=[])
        self.assertEqual(modeler.cylinders, [])
        self.assertEqual(modeler.subtractions, [])

    def test_a_failure_is_loud(self):
        class Failing(FakeModeler):
            def subtract(self, *a, **k):
                raise RuntimeError("nope")

        messages = []
        cg.open_via_holes(Failing(), self.COPPERS, self.VIAS, FRAME,
                          top_face=STACK.pcb_top, bottom_face=STACK.pcb_bottom,
                          copper_thickness=STACK.copper_thickness,
                          log=messages.append)
        self.assertTrue(any("WARNING" in m for m in messages))
        self.assertTrue(any("capped" in m for m in messages))


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
        boards = cg.read_file(self.PATH)
        self.pkg = boards["package"]
        self.chip = boards.get("chip")
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

    def test_no_pad_inboard_of_the_launch_touches_the_ground(self):
        """The bond pads.

        A signal pad straddles the centre line and may be a little wider
        than the trace -- pads usually are -- but it must not reach the
        ground. A ground pad must stay off the gap entirely: metal at
        the gap edge sits where the field between line and ground is
        strongest.
        """
        launch = {id(p) for p in self._launch_pads()}
        signal = ground = 0
        for pad in self.pkg.pads:
            if id(pad) in launch:
                continue
            inner = abs(pad.y) - pad.ysize / 2
            outer = abs(pad.y) + pad.ysize / 2
            if pad.y - pad.ysize / 2 <= 0 <= pad.y + pad.ysize / 2:
                signal += 1
                self.assertLess(
                    outer, self.gap_edge,
                    f"signal pad at y={pad.y} reaches {outer:.4f}, into the ground "
                    f"at {self.gap_edge:.4f}",
                )
            else:
                ground += 1
                self.assertGreaterEqual(
                    inner, self.gap_edge - 1e-6,
                    f"ground pad at y={pad.y} reaches {inner:.4f}, inside the gap "
                    f"edge {self.gap_edge:.4f}",
                )
        self.assertGreater(signal, 0, "no signal pads were checked")
        self.assertGreater(ground, 0, "no ground pads were checked")

    def test_two_signal_wires_land_at_each_end(self):
        """Two wires in parallel rather than one, to halve the series
        inductance at the launch."""
        from eigen_mode_analyze import bonds

        layout = bonds.layout(self.v, self.gap_edge - self.trace / 2)
        for sign in (-1, 1):
            here = [b for b in layout
                    if b.kind == bonds.SIGNAL and (b.pcb_x > 0) == (sign > 0)]
            self.assertEqual(len(here), 2)

    def test_no_via_lands_on_a_bond_pad(self):
        """The fence used to run straight across the die region, putting
        vias on top of the bond fingers. Nothing in the tree shows that;
        it only appears in the 3D view."""
        worst = None
        for via in self.pkg.vias:
            radius = via.diameter / 2
            for pad in self.pkg.pads:
                dx = max(abs(via.x - pad.x) - pad.xsize / 2, 0.0)
                dy = max(abs(via.y - pad.y) - pad.ysize / 2, 0.0)
                clearance = (dx * dx + dy * dy) ** 0.5 - radius
                if worst is None or clearance < worst[0]:
                    worst = (clearance, via, pad)
        self.assertIsNotNone(worst, "no vias or no pads to compare")
        clearance, via, pad = worst
        self.assertGreater(
            clearance, 0,
            f"via at ({via.x}, {via.y}) overlaps the pad at ({pad.x}, {pad.y})",
        )

    def test_no_via_sits_under_the_connector(self):
        """A via under a land is a hole under a solder joint."""
        under = [v for v in self.pkg.vias if abs(v.x) >= self.launch_inner]
        self.assertEqual(under, [], f"{len(under)} vias under the launch")

    def test_the_fence_does_not_cross_the_die_pocket(self):
        """There is no board there to stitch -- it is a cutout."""
        die_half = self.v["chip_length"] / 2
        fence = [v for v in self.pkg.vias if abs(v.y) < 1.0]
        self.assertTrue(fence, "no fence vias found")
        self.assertTrue(all(abs(v.x) > die_half for v in fence))

    def test_ground_is_filled_behind_each_launch(self):
        """Otherwise the CPW corridor runs bare to the board edge,
        leaving a slot in the ground behind the connector."""
        half_l = self.v["pcb_length"] / 2
        fills = [r for r in self.pkg.regions
                 if r.layer == "top"
                 and max(abs(p[1]) for p in r.points) <= self.gap_edge + 1e-6]
        self.assertEqual(len(fills), 2, "expected one ground fill behind each launch")
        for r in fills:
            xs = [p[0] for p in r.points]
            self.assertAlmostEqual(max(abs(x) for x in xs), half_l, places=6)

    def _fills(self):
        """The ground fills behind the launches: top-layer regions that
        live inside the CPW corridor."""
        return [r for r in self.pkg.regions
                if r.layer == "top"
                and max(abs(p[1]) for p in r.points) <= self.gap_edge + 1e-6]

    def _signal_reach(self):
        """How far out signal copper extends -- the trace plus the
        rounded cap the model adds, and the connector's own signal land,
        which reaches further still."""
        from eigen_mode_analyze import smp

        track = self.pkg.tracks[0]
        trace_end = abs(track.x2) + self.trace / 2.0
        origin = smp.launch_origin(self.half_l, self.v["port_space"])
        land_end = origin - smp.SIGNAL_PAD[1] + smp.SIGNAL_PAD[3] / 2.0
        return max(trace_end, land_end)

    def test_the_ground_fill_touches_no_signal_copper(self):
        """Everything on the top layer is united into ONE body, so a
        ground region overlapping anything carrying signal is a dead
        short -- and the union leaves no seam, so nothing in the tree
        shows it.

        Both earlier attempts got this wrong in the same way, each
        clearing one piece of signal copper and not the next: from the
        via fence's outer bound the fill sat 0.7 mm inside the live
        trace; moved out to clear the trace, it still overlapped the
        connector's own SIGNAL LAND by 0.45 mm. So this checks the
        furthest reach of any signal copper, not just the trace.
        """
        reach = self._signal_reach()
        fills = self._fills()
        self.assertEqual(len(fills), 2, "expected one ground fill behind each launch")
        for r in fills:
            inner = min(abs(p[0]) for p in r.points)
            self.assertGreater(
                inner, reach,
                f"ground fill starts at {inner:.4f} but signal copper reaches "
                f"{reach:.4f} -- they would be united into one shorted body",
            )

    def test_the_ground_fill_leaves_the_footprint_interior_alone(self):
        """The connector's footprint is a U of ground lands open toward
        the die, with the signal land inside the opening. That interior
        is the launch itself and is the vendor's shape, not ours to fill
        in -- the fill belongs BEHIND the outermost ground land."""
        from eigen_mode_analyze import smp

        origin = smp.launch_origin(self.half_l, self.v["port_space"])
        strap_inner = origin - smp.GROUND_STRAP[1] - smp.GROUND_STRAP[3] / 2.0
        for r in self._fills():
            inner = min(abs(p[0]) for p in r.points)
            self.assertGreaterEqual(
                inner, strap_inner - 1e-6,
                f"fill starts at {inner:.4f}, inboard of the strap at "
                f"{strap_inner:.4f} -- it is filling the footprint interior",
            )

    def test_the_ground_fill_still_does_its_job(self):
        """It exists to put metal in the corridor behind the connector,
        where the ground otherwise stops short of the board edge. Moving
        it out to clear the signal must not move it out of existence: it
        still has to meet the strap and still reach the edge."""
        from eigen_mode_analyze import smp

        origin = smp.launch_origin(self.half_l, self.v["port_space"])
        strap_outer = origin - smp.GROUND_STRAP[1] + smp.GROUND_STRAP[3] / 2.0
        for r in self._fills():
            inner = min(abs(p[0]) for p in r.points)
            outer = max(abs(p[0]) for p in r.points)
            self.assertLessEqual(inner, strap_outer + 1e-6,
                                 "the fill no longer meets the connector's ground strap")
            self.assertAlmostEqual(outer, self.half_l, places=6)

    def test_the_dies_own_copper_stops_short_of_the_edge(self):
        """The die's trace and ground planes used to run exactly to the
        die's physical edge, at the same line the PCB's copper was cut
        back to (plus a small keep-out) -- two independently-drawn edges
        meeting at that line is exactly what an interference check
        flags. Both are now pulled in by CHIP_COPPER_INSET, so there is
        a real, non-zero gap between them."""
        from eigen_mode_analyze import constraints

        die_half = self.v["chip_length"] / 2
        inset = constraints.CHIP_COPPER_INSET
        expected_edge = die_half - inset

        track = self.chip.tracks[0]
        self.assertAlmostEqual(abs(track.x2), expected_edge, places=6)

        ground_outer = max(
            max(abs(p[0]) for p in r.points) for r in self.chip.regions
        )
        self.assertAlmostEqual(ground_outer, expected_edge, places=6)
        self.assertLess(ground_outer, die_half, "the die's copper must not reach its edge")

    def test_only_copper_layers_are_exported_as_regions(self):
        """Altium's own board-outline shape shows up through the same
        iterator as a region object, on a non-copper layer -- harmless
        to the model (copper_geometry only ever reads top/bottom), but
        it was silently inflating the record count and, worse, made a
        quick sanity read of the export look like it had an extra,
        unexplained ground plane."""
        self.assertTrue(all(r.layer in ("top", "bottom") for r in self.pkg.regions))
        self.assertTrue(all(r.layer in ("top", "bottom") for r in self.chip.regions))

    def test_signal_bond_pads_tile_the_trace_without_exceeding_it(self):
        """Two signal wires land side by side at each end. At the
        general bond pad size, the pair together spanned 0.25 mm across
        a 0.20 mm trace -- 0.025 mm of copper on each side wider than
        the line, a real (if small) impedance discontinuity right at the
        bond. Each pad is now sized to Trace / 2, so the pair exactly
        tiles the trace width and touches at the centre line."""
        half_trace = self.trace / 2.0
        signal_pads = [p for p in self.pkg.pads if abs(p.ysize - half_trace) < 1e-9]
        self.assertTrue(signal_pads, "no half-trace-width pads found")

        by_x = {}
        for p in signal_pads:
            by_x.setdefault(round(p.x, 3), []).append(p)
        checked = 0
        for x, pads in by_x.items():
            lo = min(p.y - p.ysize / 2 for p in pads)
            hi = max(p.y + p.ysize / 2 for p in pads)
            self.assertAlmostEqual(lo, -half_trace, places=6, msg=f"at x={x}")
            self.assertAlmostEqual(hi, half_trace, places=6, msg=f"at x={x}")
            checked += 1
        self.assertGreater(checked, 0)
