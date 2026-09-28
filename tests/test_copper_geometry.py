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
import math
import os
import random
import unittest

from microwave_package.design import bonds as bond_mod, cavity as cav, copper_geometry as cg

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
    """Records what would have been created, instead of creating it.

    A clone made by `duplicate_along_line` is recorded as though it had
    been created in its own right, moved by the clone's offset -- so the
    geometry the tests read back is every copy in the model, whether the
    builder made it directly or had AEDT clone it. That takes knowing
    what each body is made of: `_parts` maps a body to the records it
    carries, including what a unite or a subtraction folded into it.
    """

    def __init__(self):
        self.boxes = []
        self.cylinders = []
        self.circles = []
        self.polylines = []
        self.thickened = []
        self.swept = []
        self.unites = []
        self.subtractions = []
        self.duplicates = []
        self.deleted = []
        self._n = 0
        self._by_name = {}
        self._parts = {}

    def _make(self, kind, store, record):
        self._n += 1
        solid = FakeSolid(f"{kind}{self._n}")
        self._by_name[solid.name] = solid
        store.append(record)
        self._parts[solid.name] = [(store, record)]
        return solid

    def create_box(self, origin, sizes, **kw):
        return self._make("box", self.boxes, (list(origin), list(sizes)))

    def create_cylinder(self, orientation, origin, radius, height, **kw):
        return self._make("cyl", self.cylinders, (orientation, list(origin), radius, height))

    def create_circle(self, orientation, origin, radius, **kw):
        return self._make("circle", self.circles, (orientation, list(origin), radius))

    def create_polyline(self, points, **kw):
        return self._make("poly", self.polylines, [list(p) for p in points])

    def thicken_sheet(self, sheet, height, **kw):
        self.thickened.append((sheet.name, height))

    def sweep_along_vector(self, name, vector, **kw):
        # Returns truthy like the real one: the builder treats a falsy
        # result as "the sheet never became a solid" and raises, because
        # a sheet carries no copper.
        self.swept.append((name, list(vector)))
        return True

    def _fold(self, into, names):
        for name in names:
            if into in self._parts and name in self._parts:
                self._parts[into].extend(self._parts.pop(name))

    def unite(self, parts):
        # The real one returns the surviving body's name, or False when
        # AEDT refuses -- see copper_geometry._unite_or_warn.
        self.unites.append(list(parts))
        self._fold(parts[0], parts[1:])
        return parts[0]

    def subtract(self, target, tools, keep_originals=False):
        self.subtractions.append((target, list(tools), keep_originals))
        if isinstance(target, str) and not keep_originals:
            self._fold(target, tools)

    def duplicate_along_line(self, assignment, vector, clones=2, attach=False,
                             duplicate_assignment=True, **kw):
        """Clone each named body `clones - 1` times, `vector` apart."""
        names = [assignment] if isinstance(assignment, str) else list(assignment)
        self.duplicates.append((names, list(vector), clones, attach))
        for name in names:
            originals = list(self._parts[name])
            for k in range(1, clones):
                offset = [k * c for c in vector]
                for store, record in originals:
                    clone = _moved(store, self, record, offset)
                    store.append(clone)
                    self._parts[name].append((store, clone))
        return True, []

    def delete(self, assignment):
        names = [assignment] if isinstance(assignment, str) else list(assignment)
        for name in names:
            self.deleted.append(name)
            for store, record in self._parts.pop(name, []):
                for i, kept in enumerate(store):
                    if kept is record:
                        del store[i]
                        break

    @property
    def created(self):
        """How many solids the builder asked for itself -- clones not
        included. What the block building is meant to keep small."""
        return self._n

    def carried(self, name):
        """The recorded solids that make up body `name`."""
        return [record for _store, record in self._parts.get(name, [])]

    def __getitem__(self, name):
        return self._by_name[name]


def _moved(store, modeler, record, offset):
    """A copy of one recorded solid, moved by `offset`."""
    def shift(point):
        return [p + o for p, o in zip(point, offset)]

    if store is modeler.boxes:
        origin, sizes = record
        return (shift(origin), list(sizes))
    if store is modeler.cylinders:
        orientation, origin, radius, height = record
        return (orientation, shift(origin), radius, height)
    if store is modeler.circles:
        orientation, origin, radius = record
        return (orientation, shift(origin), radius)
    return [shift(p) for p in record]


FRAME = cg.Frame(x_offset=14.0, z_offset=6.0)
STACK = cg.Stack(
    pcb_bottom=1.0, pcb_top=1.381,
    chip_bottom=1.0, chip_top=1.381,
    copper_thickness=0.035, chip_metal_thickness=0.0002,
)

# A 6.3 mm die with 0.1 mm of wiggle room each side, as the shipped
# defaults give it. THROUGH matches STACK, where the die is as thick as
# the board; POCKET is the same opening milled part way down.
THROUGH = cav.Cavity(half_length=3.25, half_side=3.25, depth=0.381, through=True)
POCKET = cav.Cavity(half_length=3.25, half_side=3.25, depth=0.2, through=False)


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
        made = cg.build_substrates(modeler, FRAME, STACK, 13, 5, 3.15, 3.15, THROUGH,
                                   log=lambda _m: None)
        self.assertEqual(made, ["pcb_substrate", "chip_substrate"])

    def test_the_pcb_is_pocketed_for_the_die(self):
        """The die sits IN the board, not on it -- so the pocket has to
        be cut, and cut for keeps."""
        modeler = FakeModeler()
        cg.build_substrates(modeler, FRAME, STACK, 13, 5, 3.15, 3.15, THROUGH,
                            log=lambda _m: None)
        self.assertEqual(len(modeler.subtractions), 1)
        target, _tools, keep = modeler.subtractions[0]
        self.assertEqual(target, "pcb_substrate")
        self.assertFalse(keep, "the pocket must actually be removed")

    def test_die_and_board_finish_flush(self):
        modeler = FakeModeler()
        cg.build_substrates(modeler, FRAME, STACK, 13, 5, 3.15, 3.15, THROUGH,
                            log=lambda _m: None)
        tops = [o[1] + s[1] for o, s in modeler.boxes]
        self.assertTrue(all(abs(t - STACK.pcb_top) < 1e-9 for t in tops),
                        f"every top face should be flush at {STACK.pcb_top}, got {tops}")

    def test_substrate_is_the_right_size_on_the_right_axes(self):
        modeler = FakeModeler()
        cg.build_substrates(modeler, FRAME, STACK, 13, 5, 3.15, 3.15, THROUGH,
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
        from microwave_package.hfss import model_builder

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

    def test_each_via_is_a_solid_post_whatever_its_drill(self):
        """No bore: the 0.15 mm drill in the sample is a waveguide cut off
        far above any frequency here, and cutting thousands of them was
        minutes of AEDT per board. So each via is one solid cylinder at
        the via's own radius, and nothing is cut out of it."""
        modeler, _ = self._build()
        radii = [c[2] for c in modeler.cylinders]
        self.assertEqual(radii.count(0.15), 2, "one post per via, at the via's radius")
        self.assertNotIn(0.075, radii, "no bore at the drill's radius")
        self.assertEqual(modeler.subtractions, [])

    def test_each_via_post_has_flat_sides(self):
        """A true cylinder is a curved face for the mesher to facet, finer
        than this, thousands of times over."""
        sides = []

        class Recording(FakeModeler):
            def create_cylinder(self, orientation, origin, radius, height, **kw):
                sides.append(kw.get("num_sides", 0))
                return super().create_cylinder(orientation, origin, radius, height, **kw)

        cg._via_post(Recording(), cg.Via(0.0, 0.0, 0.3, 0.15), FRAME,
                     STACK.pcb_bottom, STACK.pcb_top)
        self.assertEqual(sides, [cg.VIA_SIDES])
        self.assertGreaterEqual(cg.VIA_SIDES, 6, "coarser than a hexagon stops looking like a via")

    def test_several_via_blocks_stay_bodies_of_their_own(self):
        """Joining the blocks is a boolean over every via at once -- on a
        dense board, one of the long AEDT calls. They are named instead,
        each one a name the via material rule matches."""
        from microwave_package.hfss import model_builder

        modeler = FakeModeler()
        board = cg.BoardCopper(name="package", vias=[
            cg.Via(0.0, 0.0, 0.3, 0.15), cg.Via(1.0, 0.0, 0.3, 0.15),
            cg.Via(5.0, 3.0, 0.4, 0.2),
        ])
        created = cg.build(modeler, board, FRAME, top_face=STACK.pcb_top,
                           bottom_face=STACK.pcb_bottom, thickness=STACK.copper_thickness,
                           prefix="pcb", log=lambda _m: None)
        self.assertEqual(created["pcb_vias"], ["pcb_vias", "pcb_vias_2"])
        self.assertEqual(modeler.unites, [])
        self.assertEqual(sorted(s.name for s in modeler._by_name.values()),
                         ["pcb_vias", "pcb_vias_2"])
        rule = next(r for r in model_builder.MATERIAL_RULES if r[1] == "pcb_vias")
        self.assertTrue(all(rule[0] in name for name in created["pcb_vias"]))

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

    def _build_sheets(self):
        modeler = FakeModeler()
        created = cg.build(
            modeler, cg.parse(SAMPLE)["chip"], FRAME,
            top_face=STACK.chip_top, bottom_face=STACK.chip_bottom,
            thickness=0.0, prefix="chip", log=lambda _m: None,
        )
        return modeler, created

    def test_a_film_too_thin_to_mesh_is_drawn_as_sheets(self):
        """Thickness 0: covered outlines lying on the die's face, nothing
        boxed, swept or thickened into a solid."""
        modeler, created = self._build_sheets()
        self.assertEqual(created["chip_copper_top"], ["chip_copper_top"])
        self.assertEqual((modeler.boxes, modeler.cylinders, modeler.swept, modeler.thickened),
                         ([], [], [], []))
        heights = {round(p[1], 9) for poly in modeler.polylines for p in poly}
        heights |= {round(c[1][1], 9) for c in modeler.circles}
        self.assertEqual(heights, {round(STACK.chip_top, 9)})

    def test_a_sheet_track_keeps_its_round_caps_in_the_board_plane(self):
        modeler, _ = self._build_sheets()
        caps = [c for c in modeler.circles if c[2] == 0.1]
        self.assertEqual(len(caps), 2)
        self.assertTrue(all(c[0] == "ZX" for c in caps), "ZX is the plane: the disc's axis is Y")


class TestBondWires(unittest.TestCase):
    """Wires are built from the same file Altium places the pads from,
    so a wire cannot land beside its pad."""

    def _bonds(self):
        from microwave_package.design.bonds import Bond

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
        from microwave_package.design.bonds import Bond

        bond = Bond("signal", -4.15, -0.05, -2.85, -0.05)
        path = cg.wire_path(bond, FRAME, 1.381, 1.381, 0.2)
        self.assertEqual(path[0], FRAME.point(bond.pcb_x, bond.pcb_y, 1.381))
        self.assertEqual(path[-1], FRAME.point(bond.chip_x, bond.chip_y, 1.381))

    def test_build_bond_wires_raises_the_path_onto_the_feet(self):
        """The caller passes the copper's own top surface as pcb_top/
        chip_top. A tube CENTRED there has half its cross-section sunk
        into the pad and the substrate below it. This used to be fixed
        by raising the centreline exactly one radius, which put the
        tube's underside ON the surface -- tangent, touching along a
        line of zero width, which is the one contact HFSS cannot mesh.
        The centreline now clears the pad by a whole foot height, and
        the foot is what carries the contact. See TestBondWireFeet."""
        modeler = FakeModeler()
        pcb_top = 1.381
        diameter = 0.025
        cg.build_bond_wires(modeler, self._bonds(), FRAME, pcb_top=pcb_top,
                            chip_top=pcb_top, height=0.2, diameter=diameter,
                            log=lambda _m: None)
        lowest = min(p[1] for path in modeler.polylines for p in path)
        self.assertAlmostEqual(lowest, pcb_top + cg.foot_of(diameter)[1] / 2.0, places=9)
        self.assertGreater(lowest, pcb_top + diameter / 2.0,
                           "a centreline one radius up leaves the tube tangent")

    def test_the_loop_rises_above_both_ends(self):
        from microwave_package.design.bonds import Bond

        path = cg.wire_path(Bond("signal", -4.15, 0, -2.85, 0), FRAME, 1.381, 1.381, 0.2)
        self.assertGreater(max(p[1] for p in path), 1.381)

    def test_the_apex_follows_bond_height(self):
        from microwave_package.design.bonds import Bond

        low = cg.wire_path(Bond("signal", -4, 0, -3, 0), FRAME, 1.381, 1.381, 0.1)
        high = cg.wire_path(Bond("signal", -4, 0, -3, 0), FRAME, 1.381, 1.381, 0.4)
        self.assertGreater(max(p[1] for p in high), max(p[1] for p in low))

    def test_the_loop_rises_in_height_not_sideways(self):
        from microwave_package.design.bonds import Bond

        path = cg.wire_path(Bond("ground", -4.15, 0.6, -2.85, 0.6), FRAME, 1.381, 1.381, 0.2)
        self.assertEqual(len({round(p[2], 9) for p in path}), 1)

    def test_no_bonds_makes_nothing(self):
        modeler = FakeModeler()
        self.assertEqual(
            cg.build_bond_wires(modeler, [], FRAME, 1.381, 1.381, 0.2, 0.025,
                                log=lambda _m: None),
            [],
        )
        self.assertEqual(modeler.cylinders, [], "no bonds, no feet either")
        self.assertEqual(modeler.unites, [])


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


class TestCoincidentVertices(unittest.TestCase):
    """A vertex drawn twice is a zero-length segment, and AEDT refuses a
    polyline containing one.

    It refuses it as `GrpcApiError: Failed to execute gRPC AEDT command:
    CreatePolyline`, which names neither the shape nor the reason, so
    this is worth catching here rather than four stages downstream.

    The case is real: the vendor's own connector land arrives with three
    of them where the anti-pad arc meets the escape slot -- one pair
    exactly coincident, two more 10 and 30 nm apart. Altium holds
    coordinates in 1/10000 mil, so a vertex the vendor drew once lands
    on its neighbour when the arc is flattened. Nothing this project
    draws itself is fine enough to hit it, which is why it only
    appeared once a real footprint was placed.
    """

    def test_an_exactly_repeated_vertex_is_dropped(self):
        pts = [(0, 0), (1, 0), (1, 0), (1, 1), (0, 1)]
        self.assertEqual(cg._distinct(pts), [(0, 0), (1, 0), (1, 1), (0, 1)])

    def test_a_vertex_a_few_nanometres_away_is_dropped_too(self):
        """Exact equality is not the test -- the vendor's worst pair is
        10 nm apart, which is just as unbuildable and does not compare
        equal."""
        pts = [(0, 0), (1, 0), (1 + 1e-8, 0), (1, 1), (0, 1)]
        self.assertEqual(len(cg._distinct(pts)), 4)

    def test_a_real_vertex_is_kept(self):
        """The tolerance has to stay far below any feature on the board.
        0.1 um is four orders above Altium's coordinate quantum and
        orders below anything that is made or meshed."""
        pts = [(0, 0), (1, 0), (1.001, 0), (1, 1), (0, 1)]
        self.assertEqual(len(cg._distinct(pts)), 5)

    def test_the_outline_is_treated_as_closed(self):
        """The closing segment runs from the last vertex back to the
        first, so a last vertex sitting on the first is a zero-length
        segment as surely as any other."""
        pts = [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]
        self.assertEqual(cg._distinct(pts), [(0, 0), (1, 0), (1, 1), (0, 1)])

    def test_a_clean_outline_is_left_alone(self):
        pts = [(0, 0), (1, 0), (1, 1), (0, 1)]
        self.assertEqual(cg._distinct(pts), pts)

    def test_a_rectangle_with_a_repeated_corner_still_builds_as_a_box(self):
        """Otherwise the duplicate makes a four-cornered rectangle look
        like a five-sided polygon, and it is swept instead -- which is
        the slower path, and the one that fails."""
        modeler = FakeModeler()
        board = cg.BoardCopper(name="package", regions=[
            cg.Region(cg.TOP, ((-1, -1), (1, -1), (1, -1), (1, 1), (-1, 1))),
        ])
        cg.build(modeler, board, FRAME, top_face=STACK.pcb_top,
                 bottom_face=STACK.pcb_bottom, thickness=STACK.copper_thickness,
                 prefix="pcb", log=lambda _m: None)
        self.assertEqual(len(modeler.boxes), 1, "should have taken the box path")
        self.assertEqual(modeler.polylines, [])

    def test_a_shaped_region_is_swept_without_a_zero_length_segment(self):
        """The case that actually failed: a shape that genuinely has to
        be swept, carrying a duplicate vertex into the modeller."""
        import math

        modeler = FakeModeler()
        pts = ((-1, -1), (1, -1), (1, 0), (1, 0), (0.5, 0.5), (-1, 1))
        board = cg.BoardCopper(name="package", regions=[cg.Region(cg.TOP, pts)])
        cg.build(modeler, board, FRAME, top_face=STACK.pcb_top,
                 bottom_face=STACK.pcb_bottom, thickness=STACK.copper_thickness,
                 prefix="pcb", log=lambda _m: None)
        self.assertEqual(len(modeler.polylines), 1)
        handed = modeler.polylines[0]
        for i, point in enumerate(handed):
            nxt = handed[(i + 1) % len(handed)]
            self.assertGreater(
                math.dist(point, nxt), cg.COINCIDENT_MM,
                "a zero-length segment reached the modeller",
            )

    def test_a_region_with_no_area_is_refused_rather_than_built(self):
        """Everything collapsing to one point is not a solid, and a
        silent empty body is worse than a named failure."""
        modeler = FakeModeler()
        board = cg.BoardCopper(name="package", regions=[
            cg.Region(cg.TOP, ((1, 1), (1, 1), (1, 1), (1, 1))),
        ])
        with self.assertRaises(RuntimeError):
            cg.build(modeler, board, FRAME, top_face=STACK.pcb_top,
                     bottom_face=STACK.pcb_bottom,
                     thickness=STACK.copper_thickness, prefix="pcb",
                     log=lambda _m: None)


class TestClearCavityArea(unittest.TestCase):
    """The board has an opening cut in it for the die, so copper must
    not cross that footprint -- it would be hanging over a hole, or
    trapped under the die where the real board has none. Either way it
    is a stray conductor exactly where the fields are strongest."""

    def _clear(self, targets=("pcb_copper_top", "pcb_copper_bottom")):
        modeler = FakeModeler()
        cg.clear_cavity_area(modeler, FRAME, STACK, THROUGH, targets,
                             log=lambda _m: None)
        return modeler

    def test_the_cut_follows_the_opening_not_the_die(self):
        """The opening is what the copper has to clear, and it is wider
        than the die by the assembly clearance. Cutting at the die's own
        edge would leave a 0.1 mm lip of copper overhanging the hole."""
        modeler = self._clear()
        _origin, sizes = modeler.boxes[0]
        self.assertAlmostEqual(sizes[0], THROUGH.length)
        self.assertAlmostEqual(sizes[2], THROUGH.side)
        self.assertGreater(sizes[0], 6.3, "wider than the 6.3 mm die")

    def test_the_cut_spans_past_both_copper_layers(self):
        """It has to reach above the top copper and below the bottom
        copper, or one layer survives the cut."""
        modeler = self._clear()
        origin, sizes = modeler.boxes[0]
        low, high = origin[1], origin[1] + sizes[1]
        self.assertLess(low, STACK.pcb_bottom - STACK.copper_thickness)
        self.assertGreater(high, STACK.pcb_top + STACK.copper_thickness)

    def test_a_blind_pocket_is_cut_through_both_layers_too(self):
        """The opening only reaches part way into the board, but the
        copper that must go is on both faces of it: the top layer is
        inside the pocket, and the bottom layer is under the die with no
        gap. Cutting only as deep as the pocket would leave the second."""
        modeler = FakeModeler()
        cg.clear_cavity_area(modeler, FRAME, STACK, POCKET,
                             ["pcb_copper_top", "pcb_copper_bottom"],
                             log=lambda _m: None)
        origin, sizes = modeler.boxes[0]
        self.assertLess(origin[1], STACK.pcb_bottom - STACK.copper_thickness)
        self.assertGreater(origin[1] + sizes[1], STACK.pcb_top + STACK.copper_thickness)

    def test_both_layers_are_cut_together(self):
        modeler = self._clear()
        target, tools, keep = modeler.subtractions[0]
        self.assertEqual(target, ["pcb_copper_top", "pcb_copper_bottom"])
        self.assertEqual(tools, ["cavity_area_cut"])
        self.assertFalse(keep, "the cutting tool must be consumed")

    def test_the_tool_is_named_not_left_anonymous(self):
        """An unnamed leftover shows up in the tree as NewObject_* with
        no material, which is how a stray body hides in plain sight."""
        modeler = self._clear()
        self.assertEqual(modeler[modeler.boxes and "box1"].name, "cavity_area_cut")

    def test_nothing_to_cut_is_a_no_op(self):
        modeler = FakeModeler()
        cg.clear_cavity_area(modeler, FRAME, STACK, THROUGH, [], log=lambda _m: None)
        self.assertEqual(modeler.boxes, [])
        self.assertEqual(modeler.subtractions, [])


class TestPocketedSubstrate(unittest.TestCase):
    """What gets taken out of the board, and how deep.

    The opening is Altium's feature; these pin that the model reproduces
    it rather than re-deriving something die-shaped of its own."""

    def _pocket(self, cavity):
        modeler = FakeModeler()
        cg.build_substrates(modeler, FRAME, STACK, 13, 5, 3.15, 3.15, cavity,
                            log=lambda _m: None)
        # boxes[0] is the board itself, boxes[1] the tool taken out of it.
        return modeler.boxes[1]

    def test_the_opening_is_the_size_altium_drew_not_the_die(self):
        _origin, sizes = self._pocket(THROUGH)
        self.assertAlmostEqual(sizes[0], THROUGH.length)
        self.assertAlmostEqual(sizes[2], THROUGH.side)

    def test_a_through_cutout_goes_all_the_way_through(self):
        origin, sizes = self._pocket(THROUGH)
        self.assertAlmostEqual(origin[1], STACK.pcb_bottom)
        self.assertAlmostEqual(origin[1] + sizes[1], STACK.pcb_top)

    def test_a_blind_pocket_leaves_a_floor(self):
        """Milled down from the top face by its own depth -- so the
        board survives underneath, which is the whole difference between
        the two shapes."""
        origin, sizes = self._pocket(POCKET)
        self.assertAlmostEqual(sizes[1], POCKET.depth)
        self.assertAlmostEqual(origin[1] + sizes[1], STACK.pcb_top)
        self.assertGreater(origin[1], STACK.pcb_bottom, "a floor is left under it")

    def test_the_die_itself_stays_its_own_size(self):
        """The clearance is room around the die, not a bigger die."""
        modeler = FakeModeler()
        cg.build_substrates(modeler, FRAME, STACK, 13, 5, 3.15, 3.15, THROUGH,
                            log=lambda _m: None)
        _origin, sizes = modeler.boxes[2]
        self.assertAlmostEqual(sizes[0], 6.3)
        self.assertAlmostEqual(sizes[2], 6.3)


class TestWireBondNaming(unittest.TestCase):
    """HFSS will not take a duplicate name: it invents
    NewObject_something instead, which matches no material rule and
    lands in the model as vacuum."""

    def test_the_name_is_used(self):
        from microwave_package.design.bonds import Bond

        modeler = FakeModeler()
        made = cg.build_bond_wires(modeler, [Bond("ground", -4, 0.6, -3, 0.6)],
                                   FRAME, 1.381, 1.381, 0.2, 0.025,
                                   name="wirebond_a", log=lambda _m: None)
        self.assertEqual(made, ["wirebond_a"])

    def test_the_default_name_matches_the_material_rule(self):
        from microwave_package.hfss import model_builder

        patterns = [rule[0] for rule in model_builder.MATERIAL_RULES]
        self.assertTrue(any(p in "wirebond" for p in patterns))


REAL_COPPER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "base_parts", "altium", "generated", "copper.txt",
)


def _grid(nu, nv, pitch_u=0.5, pitch_v=0.4, u0=-3.0, v0=-2.0, skip=lambda i, j: False):
    return [(u0 + i * pitch_u, v0 + j * pitch_v)
            for j in range(nv) for i in range(nu) if not skip(i, j)]


class TestBlocks(unittest.TestCase):
    """Positions gathered into rectangular arrays AEDT can clone.

    The one property that matters above all: every position comes back
    exactly once. A block that drops one is a missing via nothing
    downstream would notice, and one that adds one is a via where the
    generator decided there was no room for it.
    """

    def assertCoversExactly(self, points, found):
        """Every block position matches one input point, one to one,
        within the 6 * COINCIDENT_MM `blocks` promises a lattice moves them."""
        allowed = 6 * cg.COINCIDENT_MM
        cell = 10 * cg.COINCIDENT_MM
        grid = {}
        for index, (u, v) in enumerate(points):
            grid.setdefault((round(u / cell), round(v / cell)), []).append(index)
        used = set()
        slots = [p for block in found for p in block.points()]
        self.assertEqual(len(slots), len(points), "blocks carry the wrong number of positions")
        for u, v in slots:
            cu, cv = round(u / cell), round(v / cell)
            near = [
                i for du in (-1, 0, 1) for dv in (-1, 0, 1)
                for i in grid.get((cu + du, cv + dv), [])
                if i not in used and math.dist(points[i], (u, v)) <= allowed
            ]
            self.assertTrue(near, f"a block puts a copy at ({u}, {v}), where there is none")
            used.add(near[0])

    def test_a_grid_is_one_block(self):
        points = _grid(12, 7)
        found = cg.blocks(points)
        self.assertEqual(len(found), 1)
        self.assertEqual(sorted((found[0].count, found[0].rows)), [7, 12])
        self.assertCoversExactly(points, found)

    def test_a_single_row_or_column_is_one_block(self):
        for points in (_grid(9, 1), _grid(1, 9)):
            found = cg.blocks(points)
            self.assertEqual(len(found), 1)
            self.assertCoversExactly(points, found)

    def test_a_staggered_grid_is_two_blocks(self):
        """Alternate rows offset by half a pitch -- what the stitching
        grid does. Each set of rows is a lattice of its own."""
        even = _grid(10, 5, pitch_v=0.8)
        odd = _grid(10, 5, pitch_v=0.8, u0=-2.75, v0=-1.6)
        found = cg.blocks(even + odd)
        self.assertEqual(len(found), 2)
        self.assertCoversExactly(even + odd, found)

    def test_a_grid_with_a_hole_in_it_is_four_blocks(self):
        """The die opening: the rows either side of it run straight
        through, and the rows beside it are cut in two."""
        points = _grid(20, 20, skip=lambda i, j: 7 <= i < 13 and 7 <= j < 13)
        found = cg.blocks(points)
        self.assertEqual(len(found), 4)
        self.assertCoversExactly(points, found)

    def test_nanometres_off_the_lattice_is_still_on_it(self):
        """Altium holds coordinates in 1/10000 mil, so a pitch that is not
        a whole number of those lands every via a few nanometres off the
        ideal grid. That must not break a row into pieces."""
        rng = random.Random(7)
        points = [(u + rng.uniform(-3e-6, 3e-6), v + rng.uniform(-3e-6, 3e-6))
                  for u, v in _grid(30, 20)]
        found = cg.blocks(points)
        self.assertEqual(len(found), 1)
        self.assertCoversExactly(points, found)

    def test_a_slow_drift_is_not_mistaken_for_a_row(self):
        """Each step 50 nm longer than the last: every neighbour is within
        tolerance of the one before, but by the end of the row the copies
        would be microns out. The run has to end before that happens."""
        points = [(i * 0.5 + 25e-6 * i * i, 0.0) for i in range(40)]
        found = cg.blocks(points)
        self.assertGreater(len(found), 1)
        self.assertCoversExactly(points, found)

    def test_points_with_no_pattern_still_all_come_back(self):
        rng = random.Random(11)
        points = [(rng.uniform(-30, 30), rng.uniform(-10, 10)) for _ in range(300)]
        found = cg.blocks(points)
        self.assertLessEqual(len(found), len(points))
        self.assertCoversExactly(points, found)

    def test_a_point_given_twice_is_built_twice(self):
        """Not this module's call to drop one -- and the two copies unite
        into the same metal either way."""
        points = [(1.0, 2.0), (1.0, 2.0), (1.5, 2.0)]
        self.assertCoversExactly(points, cg.blocks(points))

    def test_nothing_in_nothing_out(self):
        self.assertEqual(cg.blocks([]), [])

    def test_the_real_board_packs_into_a_few_blocks(self):
        if not os.path.isfile(REAL_COPPER):
            self.skipTest("copper.txt not present -- run the Altium generator first")
        for board in cg.read_file(REAL_COPPER).values():
            if not board.vias:
                continue
            points = [(v.x, v.y) for v in board.vias]
            found = cg.blocks(points)
            self.assertLess(len(found), 40, f"{len(points)} vias took {len(found)} blocks")
            self.assertCoversExactly(points, found)


class TestBuildingInBlocks(unittest.TestCase):
    """Copies are cloned by AEDT, not built one call at a time.

    Building every via with calls of its own is what made a model take
    hours: each call is a round trip, and each boolean costs more the
    more bodies the model already holds. These pin the number of things
    the builder makes itself, and that the clones land where the copies
    belong.
    """

    def _build(self, board, modeler=None):
        modeler = modeler if modeler is not None else FakeModeler()
        messages = []
        created = cg.build(modeler, board, FRAME, top_face=STACK.pcb_top,
                           bottom_face=STACK.pcb_bottom, thickness=STACK.copper_thickness,
                           prefix="pcb", log=messages.append)
        return modeler, created, messages

    def _via_grid(self):
        points = _grid(12, 9)
        return points, cg.BoardCopper(
            name="package", vias=[cg.Via(u, v, 0.3, 0.15) for u, v in points])

    def test_a_grid_of_vias_is_one_post_cloned_twice(self):
        points, board = self._via_grid()
        modeler, created, _ = self._build(board)
        self.assertEqual(modeler.created, 1, "one post, whatever the count")
        self.assertEqual(len(modeler.duplicates), 2, "one clone along each side of the block")
        self.assertTrue(all(attach for *_rest, attach in modeler.duplicates),
                        "clones must join the original's body, not each become one")
        self.assertEqual(created["pcb_vias"], ["pcb_vias"])

    def test_every_via_is_in_the_model_where_the_layout_put_it(self):
        points, board = self._via_grid()
        modeler, _, _ = self._build(board)
        posts = sorted((round(c[1][0], 9), round(c[1][2], 9)) for c in modeler.cylinders)
        wanted = sorted((round(p[0], 9), round(p[2], 9))
                        for p in (FRAME.point(u, v, 0.0) for u, v in points))
        self.assertEqual(posts, wanted)

    def test_clones_step_along_the_board_not_the_assembly(self):
        """The same axis flip as `Frame.point`: a step along v is a step
        along -Z. A clone vector left in board axes would lay the grid out
        mirrored, which no count would notice."""
        self.assertEqual(FRAME.vector(1.0, 0.0), [1.0, 0.0, 0.0])
        self.assertEqual(FRAME.vector(0.0, 1.0), [0.0, 0.0, -1.0])
        for u, v, w in ((1.0, 2.0, 3.0), (-0.5, 0.25, 0.0)):
            moved = FRAME.point(u, v, w)
            origin = FRAME.point(0.0, 0.0, 0.0)
            self.assertEqual(FRAME.vector(u, v, w), [a - b for a, b in zip(moved, origin)])

    def test_a_block_aedt_will_not_clone_is_built_one_by_one(self):
        """Slow and right beats fast and wrong -- and says so."""
        class NoClones(FakeModeler):
            def duplicate_along_line(self, *a, **k):
                raise RuntimeError("nope")

        points, board = self._via_grid()
        modeler, _, messages = self._build(board, NoClones())
        self.assertEqual(len(modeler.cylinders), len(points))
        self.assertTrue(modeler.deleted, "the half-cloned original must go")
        self.assertTrue(any("WARNING" in m for m in messages))

    def test_pads_of_one_shape_are_one_pad_cloned(self):
        board = cg.BoardCopper(name="package", pads=[
            cg.Pad(cg.TOP, -2.4 + 0.4 * i, 3.5, 0.3, 0.15) for i in range(13)
        ] + [cg.Pad(cg.TOP, 5.0, 0.0, 1.0, 1.0)])
        modeler, _, _ = self._build(board)
        self.assertEqual(modeler.created, 2, "one pad per shape")
        self.assertEqual(len(modeler.boxes), 14, "and every pad in the model")

    def test_a_row_of_bonds_is_one_bond_cloned(self):
        from microwave_package.design.bonds import Bond

        row = [Bond("ground", -3.5, -2.4 + 0.4 * i, -2.85, -2.4 + 0.4 * i) for i in range(12)]
        modeler = FakeModeler()
        names = cg.build_bond_wires(modeler, row, FRAME, pcb_top=1.381, chip_top=1.381,
                                    height=0.2, diameter=0.025, log=lambda _m: None)
        self.assertEqual(names, ["wirebond"])
        self.assertEqual(modeler.created, 3, "one wire on its two feet")
        self.assertEqual(len(modeler.polylines), 12)
        self.assertEqual(len(modeler.cylinders), 24)
        for bond, path in zip(row, modeler.polylines):
            self.assertAlmostEqual(path[0][0], FRAME.point(bond.pcb_x, bond.pcb_y, 0)[0])
            self.assertAlmostEqual(path[0][2], FRAME.point(bond.pcb_x, bond.pcb_y, 0)[2])
            self.assertAlmostEqual(path[-1][0], FRAME.point(bond.chip_x, bond.chip_y, 0)[0])
            self.assertAlmostEqual(path[-1][2], FRAME.point(bond.chip_x, bond.chip_y, 0)[2])

    def test_the_real_board_takes_a_few_hundred_calls_not_thousands(self):
        if not os.path.isfile(REAL_COPPER):
            self.skipTest("copper.txt not present -- run the Altium generator first")
        board = cg.read_file(REAL_COPPER)["package"]
        modeler, _, _ = self._build(board)
        calls = (modeler.created + len(modeler.duplicates)
                 + len(modeler.unites) + len(modeler.subtractions))
        self.assertLess(calls, 300, f"{len(board)} records took {calls} modeller calls")
        posts = [c for c in modeler.cylinders
                 if any(abs(c[2] - v.diameter / 2.0) < 1e-9 for v in board.vias[:1])]
        self.assertEqual(len(posts), len(board.vias))


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
        from microwave_package import paths
        from microwave_package.design import altium_generation as ag, equations as eq, smp

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

    def test_no_region_carries_a_zero_length_segment(self):
        """Against the real board, because the shape that failed came
        out of a vendor library rather than out of this generator."""
        import math

        for r in self.pkg.regions + (self.chip.regions if self.chip else []):
            pts = cg._distinct(r.points)
            self.assertGreaterEqual(len(pts), 3)
            worst = min(math.dist(pts[i], pts[(i + 1) % len(pts)])
                        for i in range(len(pts)))
            self.assertGreater(worst, cg.COINCIDENT_MM)

    def test_the_centre_conductor_stops_at_the_launch(self):
        """Running it further is what let the connector land bridge the
        signal into both grounds."""
        ends = [abs(x) for t in self.pkg.tracks for x in (t.x1, t.x2)]
        self.assertAlmostEqual(max(ends), self.launch_x, places=6)
        self.assertLess(max(ends), self.half_l)

    def test_the_connectors_ground_keeps_the_anti_pad_clear(self):
        """The vendor holds ground VOID_RADIUS from the centre pin, and
        that clearance is what sets the impedance of the coax-to-
        coplanar transition. A ground land inside it would be a short --
        and united into one body it would leave no seam to notice.

        This replaces a check against the ground strap, which the KiCad
        land pattern had and the vendor Altium one does not. The
        clearance is the thing that mattered; the strap was only where
        it happened to be measured.
        """
        import math

        from microwave_package.design import smp

        origin = smp.launch_origin(self.half_l, self.v["port_space"])
        grounds = [p for p in self._launch_pads()
                   if abs(abs(p.x) - self.launch_x) > 1e-6]
        self.assertTrue(grounds, "no ground lands found at the launch")
        for pad in grounds:
            axis = origin if pad.x > 0 else -origin
            nearest = min(math.hypot(x - axis, y) for x, y in pad.corners())
            self.assertGreaterEqual(
                nearest, smp.VOID_RADIUS - 1e-6,
                f"a ground land reaches {nearest:.4f} mm of the pin, inside the "
                f"{smp.VOID_RADIUS} mm anti-pad",
            )

    def test_the_launch_lands_port_space_from_the_board_edge(self):
        """Measured over ALL the launch copper, not just its pads. On
        this footprint the outermost copper is the ground land, which
        reaches 0.35 mm further back than any pad -- taking the pads
        alone would call the connector correctly placed while it sat
        that much too far out."""
        from microwave_package.design import smp

        origin = smp.launch_origin(self.half_l, self.v["port_space"])
        # The connector's ground land: the region that straddles the
        # launch axis and stays within the land's own half-width. The
        # coplanar planes reach the board edge and the ground fill runs
        # out to it deliberately, so neither may be counted here.
        lands = [
            r for r in self.pkg.regions
            if max(abs(y) for _x, y in r.points) <= smp.GROUND_LAND_HALF + 1e-6
            and min(abs(x) for x, _y in r.points) <= origin
            <= max(abs(x) for x, _y in r.points)
        ]
        self.assertEqual(len(lands), 2, "expected one ground land per launch")
        outer = max(
            max(max(abs(x) for x, _y in r.points) for r in lands),
            max(abs(x) for p in self.pkg.pads for x, _y in p.corners()),
        )
        self.assertAlmostEqual(outer, self.half_l - self.v["port_space"], places=6)

    def test_the_signal_land_stays_inside_the_gap(self):
        """It is wider than the trace, but must still clear the grounds.

        Measured across the pad CORNERS, because the placed connector is
        rotated a quarter turn and xsize/ysize are in the pad own frame.
        Read as board extents they are swapped, which reports a 1.4 mm
        land as 1.4 mm ACROSS the line -- comfortably into both grounds,
        and comfortably passing a test that read them that way.
        """
        lands = [p for p in self.pkg.pads if abs(abs(p.x) - self.launch_x) < 1e-6]
        self.assertTrue(lands, "no signal land at the launch centre")
        for pad in lands:
            across = max(abs(y) for _x, y in pad.corners())
            self.assertLess(across, self.gap_edge)

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
        from microwave_package.design import bonds

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

    def _launch_lands(self):
        """The connector's own ground-land regions -- the shaped
        copper its library footprint brings in, distinguished from
        this generator's own planes and fills by their Y reach, which
        matches the vendor's land half-width (smp.GROUND_LAND_HALF)
        rather than the CPW gap or the board's own half-side.

        A single rectangular threshold does not describe this shape:
        the land's own edge sits at a different X than the signal
        pad's inner edge (self.launch_inner), which reaches further
        back onto the board but only across the pad's own narrow
        width, not the land's full height. Using launch_inner as an X
        bound across the land's whole Y reach is exactly what
        overstated this footprint before -- a via well clear of the
        land in fact, near its outer corner, still failed on X alone.
        Reading the region's own bounding box sidesteps the question
        of which named measurement corresponds to which edge."""
        half = self.smp.GROUND_LAND_HALF
        return [
            r for r in self.pkg.regions
            if r.layer == "top"
            and abs(max(abs(p[1]) for p in r.points) - half) < 1e-3
        ]

    def test_no_via_sits_under_the_connector(self):
        """A via under the launch's own ground land -- real copper the
        library footprint brings in, not drawn by this generator -- is
        a hole under a solder joint. Checked against the land regions'
        own bounding boxes rather than a hand-derived rectangle; see
        _launch_lands for why a single rectangle does not describe it."""
        lands = self._launch_lands()
        self.assertEqual(len(lands), 2, "expected one ground land region per launch")
        under = []
        for via in self.pkg.vias:
            r = via.diameter / 2.0
            for land in lands:
                xs = [p[0] for p in land.points]
                ys = [p[1] for p in land.points]
                if (min(xs) - r <= via.x <= max(xs) + r) and (min(ys) - r <= via.y <= max(ys) + r):
                    under.append(via)
                    break
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
        from microwave_package.design import smp

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
        from microwave_package.design import smp

        origin = smp.launch_origin(self.half_l, self.v["port_space"])
        land_outer = origin - smp.OUTER_V
        for r in self._fills():
            inner = min(abs(p[0]) for p in r.points)
            self.assertGreaterEqual(
                inner, land_outer - 1e-6,
                f"fill starts at {inner:.4f}, inboard of the connector ground "
                f"land at {land_outer:.4f} -- it is filling the footprint interior",
            )

    def test_the_ground_fill_still_does_its_job(self):
        """It exists to put metal in the corridor behind the connector,
        where the ground otherwise stops short of the board edge. Moving
        it out to clear the signal must not move it out of existence: it
        still has to meet the strap and still reach the edge."""
        from microwave_package.design import smp

        origin = smp.launch_origin(self.half_l, self.v["port_space"])
        land_outer = origin - smp.OUTER_V
        for r in self._fills():
            inner = min(abs(p[0]) for p in r.points)
            outer = max(abs(p[0]) for p in r.points)
            self.assertLessEqual(inner, land_outer + 1e-6,
                                 "the fill no longer meets the connector ground land")
            self.assertAlmostEqual(outer, self.half_l, places=6)

    def test_the_dies_own_copper_stops_short_of_the_edge(self):
        """The die's trace and ground planes used to run exactly to the
        die's physical edge, at the same line the PCB's copper was cut
        back to (plus a small keep-out) -- two independently-drawn edges
        meeting at that line is exactly what an interference check
        flags. Both are now pulled in by CHIP_COPPER_INSET, so there is
        a real, non-zero gap between them."""
        from microwave_package.design import constraints

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


# --- wire bonds -------------------------------------------------------

def _unit(a, b):
    d = [b[i] - a[i] for i in range(3)]
    length = math.sqrt(sum(c * c for c in d))
    return [c / length for c in d]


def _cap_points(end, inward, radius, samples=24):
    """The rim of the tube's flat end cap, sampled all the way round.

    The cap is a disc of `radius` about `end`, lying square to the run
    `inward -> end`. Two unit vectors perpendicular to that run span it;
    the first is built against whichever axis the run leans on least, so
    the cross product never collapses on a wire that happens to come
    down along one.
    """
    d = _unit(inward, end)
    axis = min(range(3), key=lambda i: abs(d[i]))
    seed = [0.0, 0.0, 0.0]
    seed[axis] = 1.0
    e1 = _cross(d, seed)
    e1 = [c / math.sqrt(sum(v * v for v in e1)) for c in e1]
    e2 = _cross(d, e1)
    return [
        [end[i] + radius * (math.cos(a) * e1[i] + math.sin(a) * e2[i]) for i in range(3)]
        for a in (2.0 * math.pi * k / samples for k in range(samples))
    ]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def _tube_underside(points, radius):
    """The lowest point on the swept tube's surface, per centreline point.

    For a straight run in direction `d`, the surface is `c + r * n` over
    every unit `n` perpendicular to `d`, so the surface dips
    `r * sqrt(1 - d_up^2)` below the centreline -- a full radius when the
    run is level, nothing at all when it is vertical. The up axis is Y,
    index 1, because the assembly is Y-up (see `Frame`).

    Only the ends of each straight run are checked: the centreline
    height is linear along one, so its minimum is at an end.
    """
    lows = []
    for a, b in zip(points, points[1:]):
        d = _unit(a, b)
        drop = radius * math.sqrt(max(0.0, 1.0 - d[1] * d[1]))
        lows.append(a[1] - drop)
        lows.append(b[1] - drop)
    return lows


class TestBondWireFeet(unittest.TestCase):
    """A bond has to JOIN the pad, and HFSS joins conductors through
    shared mesh -- overlapping volume, or a coincident face. The wire
    alone gives neither: level with the pad it buries half its section
    in the substrate, and lifted clear by a radius it only grazes the
    pad along a line of zero width. Ansys lists tangential contacts as a
    mesh-failure cause and asks for at least 0.001 model units across a
    contact. So each end stands on a foot.
    """

    DIAMETER = 0.025
    HEIGHT = 0.2

    def build(self, bond_list, height=None, diameter=None):
        modeler = FakeModeler()
        names = cg.build_bond_wires(
            modeler, bond_list, FRAME,
            pcb_top=STACK.pcb_copper_top,
            chip_top=STACK.chip_copper_top,
            height=self.HEIGHT if height is None else height,
            diameter=self.DIAMETER if diameter is None else diameter,
            log=lambda *a, **k: None,
        )
        return modeler, names

    def bonds(self):
        return [
            bond_mod.Bond(bond_mod.SIGNAL, -4.0, 0.05, -3.0, 0.05),
            bond_mod.Bond(bond_mod.GROUND, -4.0, 1.20, -3.0, 1.20),
            bond_mod.Bond(bond_mod.GROUND, 2.0, 4.0, 2.0, 3.0),
        ]

    def test_every_bond_gets_a_foot_at_each_end(self):
        modeler, _ = self.build(self.bonds())
        self.assertEqual(len(modeler.polylines), 3)
        self.assertEqual(len(modeler.cylinders), 6)

    def test_feet_stand_on_the_pad_and_rise_from_it(self):
        """The foot's underside must LIE IN the pad's top face -- that
        coincident face is the whole join. A foot sunk into the pad
        would be two conductors claiming one volume on the board and,
        on the die, 25 um of gold inside 0.2 um of metal and the silicon
        under it."""
        modeler, _ = self.build(self.bonds())
        pads = {round(STACK.pcb_copper_top, 9), round(STACK.chip_copper_top, 9)}
        for orientation, origin, _radius, height in modeler.cylinders:
            self.assertEqual(orientation, "Y", "the assembly is Y-up")
            self.assertIn(round(origin[1], 9), pads)
            self.assertGreater(height, 0.0, "the foot must rise off the pad, not sink in")

    def test_the_contact_is_a_face_wider_than_the_wire(self):
        modeler, _ = self.build(self.bonds())
        for _o, _origin, radius, _h in modeler.cylinders:
            self.assertGreater(radius, self.DIAMETER / 2.0)
            # Ansys asks that a contact between objects be at least
            # 0.001 model units across. Units here are mm, so 1 um.
            self.assertGreater(2.0 * radius, 0.001)

    def test_each_foot_is_concentric_with_the_arc_it_carries(self):
        """A foot offset from the wire end is a foot the wire misses."""
        modeler, _ = self.build(self.bonds())
        ends = []
        for points in modeler.polylines:
            ends.extend([points[0], points[-1]])
        for (_o, origin, _r, _h), end in zip(modeler.cylinders, ends):
            self.assertAlmostEqual(origin[0], end[0], places=9)
            self.assertAlmostEqual(origin[2], end[2], places=9)

    def test_the_arc_ends_at_the_centre_of_its_foot(self):
        """Not at the top face. The end cap is a disc of one wire radius
        about the arc's last point, so putting that point at the centre
        is what keeps the cap inside the foot however it is tilted -- and
        putting it at the top face, which is what this did first, leaves
        half the cap standing proud as a cut end of wire poking out of
        the bond."""
        modeler, _ = self.build(self.bonds())
        _radius, height = cg.foot_of(self.DIAMETER)
        for points in modeler.polylines:
            self.assertAlmostEqual(points[0][1], STACK.pcb_copper_top + height / 2.0, places=9)
            self.assertAlmostEqual(points[-1][1], STACK.chip_copper_top + height / 2.0, places=9)

    def test_the_cut_end_of_the_wire_is_buried_in_the_foot(self):
        """The one that matters, stated as geometry rather than as the
        formula that produced it: sample the end cap all the way round
        and put every point of it inside the foot, clear of both faces
        and of the side."""
        modeler, _ = self.build(self.bonds())
        radius, height = cg.foot_of(self.DIAMETER)
        feet = list(modeler.cylinders)
        for wire, points in enumerate(modeler.polylines):
            for end, inward, foot in ((0, 1, feet[2 * wire]),
                                      (-1, -2, feet[2 * wire + 1])):
                _o, origin, _r, _h = foot
                for point in _cap_points(points[end], points[inward], self.DIAMETER / 2.0):
                    across = math.hypot(point[0] - origin[0], point[2] - origin[2])
                    self.assertLess(across, radius, "cap escapes through the side")
                    self.assertGreater(point[1], origin[1], "cap escapes below, into the pad")
                    self.assertLess(point[1], origin[1] + height, "cap escapes out of the top")

    def test_the_foot_has_room_for_the_cap_at_any_angle(self):
        """`foot_of`'s own rule, checked directly: a sphere of the wire's
        radius about the foot's centre fits inside it with margin. The
        cap lies on that sphere whatever the approach angle, so this is
        what makes the test above hold for loop heights and spans nobody
        has tried yet."""
        for diameter in (0.018, 0.025, 0.050):
            radius, height = cg.foot_of(diameter)
            margin = diameter * cg.FOOT_MARGIN
            self.assertGreaterEqual(height / 2.0, diameter / 2.0 + margin,
                                    f"no headroom at d={diameter}")
            self.assertGreaterEqual(radius, diameter / 2.0 + margin,
                                    f"no side room at d={diameter}")

    def test_the_wire_never_dips_below_the_pad_plane(self):
        modeler, _ = self.build(self.bonds())
        floor = min(STACK.pcb_copper_top, STACK.chip_copper_top)
        for points in modeler.polylines:
            for low in _tube_underside(points, self.DIAMETER / 2.0):
                self.assertGreater(low, floor)

    def test_a_flat_loop_clears_the_pad_rather_than_grazing_it(self):
        """The worst case for the underside, and the case the old code
        got exactly wrong: with no loop the wire runs level, the tube
        dips a full radius below the centreline, and lifting it by one
        radius left it tangent -- touching along a line of zero width.
        A foot taller than a radius is what buys the clearance back."""
        modeler, _ = self.build(self.bonds(), height=0.0)
        floor = min(STACK.pcb_copper_top, STACK.chip_copper_top)
        radius = self.DIAMETER / 2.0
        clearance = cg.foot_of(self.DIAMETER)[1] / 2.0 - radius
        self.assertGreater(clearance, 0.0, "the foot must have headroom over the cap")
        for points in modeler.polylines:
            for low in _tube_underside(points, radius):
                self.assertGreaterEqual(low, floor + clearance - 1e-12)

    def test_a_steep_short_loop_still_clears_the_pad(self):
        """The other extreme: a tall loop over a short span comes down
        almost vertically. The tube dips less, not more, so the foot
        still holds -- but only because the clearance is derived from
        the radius and not from the loop."""
        short = [bond_mod.Bond(bond_mod.GROUND, -3.2, 0.0, -3.0, 0.0)]
        modeler, _ = self.build(short, height=1.0)
        floor = min(STACK.pcb_copper_top, STACK.chip_copper_top)
        for points in modeler.polylines:
            for low in _tube_underside(points, self.DIAMETER / 2.0):
                self.assertGreater(low, floor)

    def _pair(self, kinds=(bond_mod.SIGNAL, bond_mod.SIGNAL)):
        """Two wires side by side whose feet touch along a line -- the two
        signal wires at each end of the line, on pads that tile the trace."""
        radius, _height = cg.foot_of(self.DIAMETER)
        return [bond_mod.Bond(kinds[0], -4.0, -radius, -3.0, -radius),
                bond_mod.Bond(kinds[1], -4.0, radius, -3.0, radius)]

    def test_feet_that_touch_are_bridged(self):
        """Feet that meet along a single line are a contact the modeller
        cannot carve around: the vacuum came out with no volume at all.
        Two ball bonds that touch are one blob on the bench, so the waist
        between them is filled -- on the board and on the die."""
        radius, height = cg.foot_of(self.DIAMETER)
        modeler, names = self.build(self._pair())
        self.assertEqual(names, ["wirebond"])
        self.assertEqual(len(modeler.boxes), 2, "one bridge on the board, one on the die")
        pads = {round(STACK.pcb_copper_top, 9), round(STACK.chip_copper_top, 9)}
        for origin, sizes in modeler.boxes:
            self.assertIn(round(origin[1], 9), pads, "a bridge stands on the pad, like a foot")
            self.assertAlmostEqual(sizes[1], height)
            self.assertAlmostEqual(sizes[2], 2 * radius, msg="it reaches centre to centre")
            self.assertLess(sizes[0], 2 * radius,
                            "narrower than a foot, or its sides only graze the feet")
        self.assertEqual(len(modeler.carried("poly1")), 2 + 4 + 2,
                         "wires, feet and bridges are all the one body")

    def test_a_unite_aedt_refuses_is_reported(self):
        """PyAEDT does not raise when AEDT will not join bodies -- it
        returns False -- and the pieces then stay behind with no
        material. That has to reach the run log."""
        class Refusing(FakeModeler):
            def unite(self, parts):
                super().unite(parts)
                return False

        messages = []
        cg.build_bond_wires(Refusing(), self.bonds(), FRAME,
                            pcb_top=STACK.pcb_copper_top, chip_top=STACK.chip_copper_top,
                            height=self.HEIGHT, diameter=self.DIAMETER, log=messages.append)
        self.assertTrue(any("WARNING" in m and "unite" in m for m in messages))

    def test_feet_apart_are_left_alone(self):
        modeler, _ = self.build(self.bonds())
        self.assertEqual(modeler.boxes, [])

    def test_a_signal_foot_touching_a_ground_foot_is_a_short_not_a_bridge(self):
        messages = []
        modeler = FakeModeler()
        cg.build_bond_wires(modeler, self._pair((bond_mod.SIGNAL, bond_mod.GROUND)), FRAME,
                            pcb_top=STACK.pcb_copper_top, chip_top=STACK.chip_copper_top,
                            height=self.HEIGHT, diameter=self.DIAMETER, log=messages.append)
        self.assertEqual(modeler.boxes, [])
        self.assertTrue(any("WARNING" in m and "short" in m for m in messages))

    def test_wires_and_feet_unite_into_the_one_named_body(self):
        """The material rules match on the name, and only the first body
        in a unite keeps its own -- so every unite has to lead with a
        wire, not a foot, and the one body left at the end has to carry
        all of them: three wires on six feet."""
        modeler, names = self.build(self.bonds())
        self.assertEqual(names, ["wirebond"])
        self.assertTrue(all(parts[0].startswith("poly") for parts in modeler.unites),
                        f"a unite led with something other than a wire: {modeler.unites}")
        self.assertEqual(modeler["poly1"].name, "wirebond")
        self.assertEqual(len(modeler.carried("poly1")), 9)
