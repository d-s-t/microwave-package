"""Tests for reading Altium copper and rebuilding it in HFSS.

The parser is checked against the real file the generator produced, so
the wire format is pinned to something Altium actually wrote rather than
to a fixture invented here.

The builder is checked against a stand-in modeller: HFSS is not needed
to verify that the right number of solids get made, that they land at
the right heights, and -- most importantly -- that they end up with the
names the material rules match on.

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
        self.assertEqual(pkg.vias[0].diameter, 0.3)
        self.assertEqual(pkg.tracks[0].width, 0.2)

    def test_region_points_come_back_as_pairs(self):
        pkg = cg.parse(SAMPLE)["package"]
        pts = pkg.regions[0].points
        self.assertEqual(len(pts), 4)
        self.assertEqual(pts[0], (-13.0, 0.37131))
        self.assertEqual(pts[2], (13.0, 5.0))

    def test_layers_are_kept_apart(self):
        pkg = cg.parse(SAMPLE)["package"]
        top_t, top_p, top_r = pkg.on_layer(cg.TOP)
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
        """A silently skipped via is missing shielding that nothing
        downstream would notice, so parsing is strict."""
        bad = "board=x\nvia|1|2|oops|0.15\n"
        with self.assertRaises(cg.CopperParseError) as ctx:
            cg.parse(bad)
        self.assertIn("line 2", str(ctx.exception))

    def test_unknown_record_type_is_rejected(self):
        with self.assertRaises(cg.CopperParseError):
            cg.parse("board=x\nsomething|1|2\n")

    def test_region_with_odd_coordinate_count_is_rejected(self):
        with self.assertRaises(cg.CopperParseError):
            cg.parse("board=x\nregion|top|1,2,3,4,5\n")


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
        self._n = 0
        self._by_name = {}

    def _make(self, kind):
        self._n += 1
        solid = FakeSolid(f"{kind}{self._n}")
        self._by_name[solid.name] = solid
        return solid

    def create_box(self, origin, sizes, **kw):
        self.boxes.append((origin, sizes))
        return self._make("box")

    def create_cylinder(self, orientation, origin, radius, height, **kw):
        self.cylinders.append((origin, radius, height))
        return self._make("cyl")

    def create_polyline(self, points, **kw):
        self.polylines.append(points)
        return self._make("poly")

    def thicken_sheet(self, sheet, height, **kw):
        self.thickened.append((sheet.name, height))

    def unite(self, parts):
        self.unites.append(list(parts))

    def __getitem__(self, name):
        return self._by_name[name]


PLACEMENT = cg.Placement(z_top=0.381, z_bottom=0.0, thickness=0.035)


class TestBuilding(unittest.TestCase):
    def test_names_match_what_the_material_rules_expect(self):
        """The whole reason for rebuilding copper here rather than
        importing it: we choose the names, so they cannot drift away
        from MATERIAL_RULES."""
        from eigen_mode_analyze import model_builder

        modeler = FakeModeler()
        created = cg.build(modeler, cg.parse(SAMPLE)["package"], PLACEMENT,
                           prefix="pcb", log=lambda _m: None)
        self.assertIn("pcb_copper_top", created)
        self.assertIn("pcb_copper_bottom", created)
        self.assertIn("pcb_vias", created)

        patterns = [rule[0] for rule in model_builder.MATERIAL_RULES]
        for name in created:
            self.assertTrue(
                any(p in name for p in patterns),
                f"{name} matches no material rule, so it would keep the default material",
            )

    def test_one_cylinder_per_via(self):
        modeler = FakeModeler()
        cg.build(modeler, cg.parse(SAMPLE)["package"], PLACEMENT,
                 prefix="pcb", log=lambda _m: None)
        via_cyls = [c for c in modeler.cylinders if c[1] == 0.15]
        self.assertEqual(len(via_cyls), 2)

    def test_vias_span_the_whole_substrate(self):
        modeler = FakeModeler()
        cg.build(modeler, cg.parse(SAMPLE)["package"], PLACEMENT,
                 prefix="pcb", log=lambda _m: None)
        via = [c for c in modeler.cylinders if c[1] == 0.15][0]
        self.assertEqual(via[0][2], 0.0)
        self.assertAlmostEqual(via[2], 0.381)

    def test_bottom_copper_grows_downward(self):
        """Top copper grows up from the top face, bottom copper down
        from the bottom one. A sign error here buries the ground plane
        inside the substrate."""
        modeler = FakeModeler()
        cg.build(modeler, cg.parse(SAMPLE)["package"], PLACEMENT,
                 prefix="pcb", log=lambda _m: None)
        heights = dict(modeler.thickened)
        self.assertTrue(any(h > 0 for h in heights.values()))
        self.assertTrue(any(h < 0 for h in heights.values()))

    def test_track_gets_round_caps(self):
        """Square-ended tracks leave wedge gaps where they meet, and a
        gap in a ground plane moves modes."""
        modeler = FakeModeler()
        cg.build(modeler, cg.parse(SAMPLE)["chip"], PLACEMENT,
                 prefix="chip", log=lambda _m: None)
        caps = [c for c in modeler.cylinders if c[1] == 0.1]
        self.assertEqual(len(caps), 2)

    def test_everything_on_a_layer_is_united_into_one_body(self):
        modeler = FakeModeler()
        created = cg.build(modeler, cg.parse(SAMPLE)["package"], PLACEMENT,
                           prefix="pcb", log=lambda _m: None)
        self.assertEqual(len(created["pcb_copper_top"]), 1)
        self.assertTrue(modeler.unites)

    def test_offsets_shift_every_shape(self):
        """Board centre on the assembly origin is the current case, but
        the offset is what would keep copper aligned if it moved."""
        placement = cg.Placement(z_top=0.381, z_bottom=0.0, thickness=0.035,
                                 x_offset=5.0, y_offset=-2.0)
        modeler = FakeModeler()
        cg.build(modeler, cg.parse(SAMPLE)["package"], placement,
                 prefix="pcb", log=lambda _m: None)
        via = [c for c in modeler.cylinders if c[1] == 0.15][0]
        self.assertAlmostEqual(via[0][0], 11.8 + 5.0)
        self.assertAlmostEqual(via[0][1], -0.67131 - 2.0)

    def test_a_board_with_no_vias_makes_no_via_body(self):
        modeler = FakeModeler()
        created = cg.build(modeler, cg.parse(SAMPLE)["chip"], PLACEMENT,
                           prefix="chip", log=lambda _m: None)
        self.assertNotIn("chip_vias", created)


class TestAgainstRealExport(unittest.TestCase):
    """If the generator has been run, parse its actual output."""

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
        self.assertGreater(len(boards["chip"].pads), 0)


if __name__ == "__main__":
    unittest.main()


class TestWireBonds(unittest.TestCase):
    """Bonds are computed rather than read from Altium: AD26 exposes no
    wirebond object id to scripting at all, and a bond is EM geometry
    rather than layout."""

    SPEC = cg.BondSpec(
        x_start=-4.15, x_end=-2.85, z_start=0.381, z_end=0.762,
        count=3, pitch=0.4, height=0.2, diameter=0.025,
    )

    def test_path_starts_and_ends_on_the_pads(self):
        pts = cg.bond_path(self.SPEC, y=0.0)
        self.assertAlmostEqual(pts[0][0], -4.15)
        self.assertAlmostEqual(pts[0][2], 0.381)
        self.assertAlmostEqual(pts[-1][0], -2.85)
        self.assertAlmostEqual(pts[-1][2], 0.762)

    def test_loop_rises_above_both_ends(self):
        """A straight wire would clip the die edge. The loop is the
        whole point of a bond profile."""
        pts = cg.bond_path(self.SPEC, y=0.0)
        apex = max(p[2] for p in pts)
        self.assertGreater(apex, max(self.SPEC.z_start, self.SPEC.z_end))

    def test_apex_scales_with_bond_height(self):
        tall = cg.BondSpec(**{**self.SPEC.__dict__, "height": 0.5})
        self.assertGreater(
            max(p[2] for p in cg.bond_path(tall, 0.0)),
            max(p[2] for p in cg.bond_path(self.SPEC, 0.0)),
        )

    def test_bonds_are_spread_symmetrically_about_centre(self):
        modeler = FakeModeler()
        cg.build_wirebonds(modeler, self.SPEC, log=lambda _m: None)
        ys = sorted({round(p[1], 6) for path in modeler.polylines for p in path})
        self.assertEqual(ys, [-0.4, 0.0, 0.4])

    def test_one_wire_per_bond_united_under_the_material_rule_name(self):
        from eigen_mode_analyze import model_builder

        modeler = FakeModeler()
        created = cg.build_wirebonds(modeler, self.SPEC, log=lambda _m: None)
        self.assertEqual(len(modeler.polylines), 3)
        self.assertEqual(created, ["wirebond"])
        patterns = [rule[0] for rule in model_builder.MATERIAL_RULES]
        self.assertTrue(any(p in "wirebond" for p in patterns))

    def test_no_bonds_requested_makes_nothing(self):
        modeler = FakeModeler()
        spec = cg.BondSpec(**{**self.SPEC.__dict__, "count": 0})
        self.assertEqual(cg.build_wirebonds(modeler, spec, log=lambda _m: None), [])
        self.assertEqual(modeler.polylines, [])
