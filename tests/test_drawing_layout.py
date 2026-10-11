"""The dimensioned drawings' layout: which dimensions the box and lid
drawings carry, and what each must measure. Pure functions of the
combination's values; SolidWorks is not involved.

Run: python -m unittest discover -s tests
"""
import os
import tempfile
import unittest

from microwave_package.design import solidworks_drawing as sd
from microwave_package.workflow import simple_pdf

VALUES = {
    "pcb_length": 50.0, "pcb_side": 25.0, "wall_width": 1.0, "wing_length": 5.0, "wing_width": 1.0,
    "pedestal_depth": 0.5, "no_chip": 0.0, "screws_on": 1.0, "screw_inset": 3.5,
    "screw_clearance_diameter": 2.3, "screw_tap_diameter": 1.6, "screw_engagement": 3.0,
    "port_length": 5.1, "port_space": 6.0, "route_width": 1.0,
}


def expected(view):
    return [round(d.expected, 3) for d in view.dims if d.expected is not None]


class TestBoxLayout(unittest.TestCase):
    def views(self, **over):
        return {v.key: v for v in sd.box_layout({**VALUES, **over}, 4.951)}

    def test_views_are_plan_front_and_two_sections(self):
        self.assertEqual(sorted(self.views()), ["front", "sec_a", "sec_b", "top"])

    def test_plan_view_carries_the_overall_and_hole_dimensions(self):
        top = expected(self.views()["top"])
        for value in (62.0, 52.0, 27.0, 43.0, 18.0, 50.0, 25.0, 1.6):
            self.assertIn(value, top)

    def test_tap_holes_are_called_out_with_count_and_depth(self):
        callout = [d for d in self.views()["top"].dims if d.kind == "dia"][0]
        self.assertTrue(callout.prefix.startswith("4X"))
        self.assertIn("THRU", callout.suffix)

    def test_no_screws_means_no_hole_dimensions_or_section_through_them(self):
        views = self.views(screws_on=0.0)
        self.assertNotIn("sec_a", views)
        self.assertFalse([d for d in views["top"].dims if d.kind == "dia"])
        self.assertNotIn(43.0, expected(views["top"]))

    def test_bare_board_has_no_pedestal_dimensions(self):
        with_chip = self.views()
        bare = self.views(no_chip=1.0)
        self.assertGreater(len(with_chip["top"].dims), len(bare["top"].dims))
        self.assertGreater(len(with_chip["sec_b"].dims), len(bare["sec_b"].dims))

    def test_floor_thicknesses_come_from_the_screw_engagement(self):
        floors = expected(self.views()["sec_a"])
        self.assertIn(3.0, floors)              # floor at the tapped holes
        self.assertIn(2.5, expected(self.views()["sec_b"]))   # under the chip recess
        self.assertIn(0.5, expected(self.views()["sec_b"]))   # the recess itself

    def test_a_wider_board_widens_the_box(self):
        top = expected(self.views(pcb_length=60.0)["top"])
        self.assertIn(72.0, top)                # 60 + 2 walls + 2 wings
        self.assertIn(62.0, top)                # the body

    def test_sections_are_sectioned_from_the_plan_view(self):
        views = self.views()
        self.assertEqual(views["sec_a"].section_of, "top")
        self.assertEqual(views["sec_b"].section_at, 0.0)
        self.assertNotEqual(views["sec_a"].section_at, 0.0)

    def test_every_view_fits_the_sheet(self):
        for view in self.views().values():
            width = view.scale * view.extent[0] / 1000.0
            self.assertLess(view.x + width / 2 + 0.03, sd.SHEET_W - sd.MARGIN, view.key)
            self.assertGreater(view.x - width / 2 - 0.02, sd.MARGIN, view.key)


class TestVentsOnTheBoxDrawing(unittest.TestCase):
    def views(self, **over):
        return {v.key: v for v in sd.box_layout({**VALUES, "vents_on": 1.0, "vent_width": 0.5,
                                                 "vent_depth": 0.3, **over}, 4.951)}

    def test_vents_get_a_section_and_an_enlarged_detail_of_it(self):
        views = self.views()
        self.assertEqual(views["sec_v"].section_of, "top")
        detail = views["vent_detail"]
        self.assertEqual(detail.detail_of, "sec_v")
        self.assertGreater(detail.scale, views["sec_v"].scale)

    def test_the_detail_dimensions_the_groove_width_and_depth(self):
        detail = self.views(vent_width=0.6, vent_depth=0.25)["vent_detail"]
        self.assertEqual(sorted(expected(detail)), [0.25, 0.6])

    def test_the_section_cuts_across_the_pedestal_vent_clear_of_the_screw_vents(self):
        cut = self.views()["sec_v"].section_at
        self.assertGreater(cut, 3.15)                    # beyond the chip recess
        self.assertLess(cut, 9.0 - 0.8)                  # short of the screw holes' row

    def test_no_vents_no_vent_views(self):
        views = {v.key: v for v in sd.box_layout({**VALUES, "vents_on": 0.0}, 4.951)}
        self.assertNotIn("sec_v", views)
        self.assertNotIn("vent_detail", views)

    def test_the_detail_sits_inside_the_sheet_clear_of_the_title_block(self):
        detail = self.views()["vent_detail"]
        radius = detail.scale * detail.detail_radius / 1000.0
        self.assertGreater(detail.y - radius, 0.05)      # above the notes
        self.assertLess(detail.x + radius, 0.26)         # left of the title block


class TestLidLayout(unittest.TestCase):
    def views(self, **over):
        return {v.key: v for v in sd.lid_layout({**VALUES, **over}, 1.5)}

    def test_inner_face_dimensions_the_pockets_and_the_channel(self):
        inner = expected(self.views()["inner"])
        for value in (50.0, 25.0, 27.8, 38.0, 5.1, 1.0):
            self.assertIn(value, inner)

    def test_outer_face_has_the_countersunk_holes(self):
        outer = self.views()["outer"]
        self.assertIn(43.0, expected(outer))
        callout = [d for d in outer.dims if d.kind == "dia"][0]
        self.assertIn("CSK", callout.suffix)
        self.assertEqual(callout.expected, 2.3)

    def test_channel_width_follows_route_width(self):
        self.assertIn(0.8, expected(self.views(route_width=0.8)["inner"]))

    def test_pocket_positions_follow_the_port_space(self):
        inner = expected(self.views(port_space=8.0)["inner"])
        self.assertIn(round(2 * (25 - 8.0), 3), inner)        # outer edges
        self.assertIn(round(2 * (25 - 8.0 - 5.1), 3), inner)  # inner edges

    def test_section_along_the_feedline_shows_the_thickness(self):
        sec = self.views()["sec_c"]
        self.assertEqual(sec.section_of, "outer")
        self.assertIn(1.5, expected(sec))

    def test_no_screws_no_hole_dimensions(self):
        self.assertFalse([d for d in self.views(screws_on=0.0)["outer"].dims if d.kind == "dia"])


class TestLidReliefDepths(unittest.TestCase):
    """The relief depths set the line impedance, so each is dimensioned with
    a tolerance on a detail enlarged enough to show it."""

    def views(self, **over):
        return {v.key: v for v in sd.lid_layout({**VALUES, "route_clearence": 0.2, **over}, 1.5)}

    def test_each_relief_section_has_an_enlarged_detail(self):
        views = self.views()
        for key in ("sec_g", "sec_h", "sec_k"):
            detail = views[key + "_detail"]
            self.assertEqual(detail.detail_of, key)
            self.assertGreaterEqual(detail.scale, 10.0)          # 5:1 or more, per the review
            self.assertGreater(detail.scale, views[key].scale)

    def test_sections_across_and_along_the_features(self):
        views = self.views()
        self.assertEqual(views["sec_g"].section_axis, "v")
        self.assertEqual(views["sec_k"].section_axis, "h")
        self.assertEqual(views["sec_g"].section_of, "inner")

    def test_the_channel_depth_follows_route_clearence_and_carries_a_tolerance(self):
        detail = self.views(route_clearence=0.25)["sec_g_detail"]
        depth = [d for d in detail.dims if d.kind == "v"][0]
        self.assertEqual(depth.expected, 0.25)
        self.assertEqual(depth.tolerance, sd.RELIEF_TOLERANCE)
        width = [d for d in detail.dims if d.kind == "h"][0]
        self.assertEqual(width.expected, 1.0)

    def test_the_deeper_steps_are_read_off_the_section_with_a_tolerance_each(self):
        for key in ("sec_h_detail", "sec_k_detail"):
            dims = self.views()[key].dims
            self.assertTrue(dims)
            self.assertTrue(all(d.tolerance == sd.RELIEF_TOLERANCE for d in dims))
            self.assertEqual({d.group for d in dims}, {"deep"})

    def test_the_details_stay_on_the_sheet_clear_of_the_title_block(self):
        for key, view in self.views().items():
            if not key.endswith("_detail"):
                continue
            radius = view.scale * view.detail_radius / 1000.0
            self.assertGreater(view.x - radius, sd.MARGIN, key)
            self.assertLess(view.x + radius, sd.SHEET_W - sd.MARGIN, key)
            if view.x + radius > 0.26:                            # under the title block's columns
                self.assertGreater(view.y - radius, 0.06, key)


class TestScaleAndPdfCheck(unittest.TestCase):
    def test_pick_scale_takes_the_largest_that_fits(self):
        self.assertEqual(sd.pick_scale(62, 27, 150, 60), 2.0)
        self.assertEqual(sd.pick_scale(10, 10, 150, 60), 5.0)
        self.assertEqual(sd.pick_scale(1000, 1000, 150, 60), sd.SCALES[-1])

    def test_a_pdf_inside_the_page_passes_and_one_off_the_page_is_refused(self):
        folder = tempfile.mkdtemp()
        inside = os.path.join(folder, "in.pdf")
        doc = simple_pdf.Document("t")
        page = doc.add_page(simple_pdf.A3_LANDSCAPE)
        page.rect(10, 10, 400, 277)
        page.line(0, 0, 100, 100)
        doc.save(inside)
        sd.check_pdf(inside)                       # no exception
        outside = os.path.join(folder, "out.pdf")
        doc = simple_pdf.Document("t")
        page = doc.add_page(simple_pdf.A3_LANDSCAPE)
        page.line(20, 20, 820, 574)                # the frame that came out twice the size
        doc.save(outside)
        with self.assertRaises(RuntimeError):
            sd.check_pdf(outside)


if __name__ == "__main__":
    unittest.main()
