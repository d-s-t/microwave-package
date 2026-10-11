"""The saved drawing templates: how a drawing's dimensions are named, what is
written beside a template, and the decisions that say whether a combination
can use the template or has to have its drawing built from scratch, and which
of its dimensions to leave off. No SolidWorks.

Run: python -m unittest discover -s tests
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from microwave_package import paths
from microwave_package.design import solidworks_drawing as sd
from microwave_package.design import solidworks_template as tpl

VALUES = {
    "pcb_length": 50.0, "pcb_side": 25.0, "wall_width": 1.0, "wing_length": 5.0, "wing_width": 1.0,
    "pedestal_depth": 0.5, "no_chip": 0.0, "screws_on": 1.0, "screw_inset": 3.5,
    "screw_clearance_diameter": 2.3, "screw_tap_diameter": 1.6, "screw_engagement": 3.0,
    "vents_on": 1.0, "vent_width": 0.5, "vent_depth": 0.3,
    "port_length": 5.1, "port_space": 6.0, "route_width": 1.0, "route_clearence": 0.2,
}
MEASURED = {"channel": 0.2, "end": 0.4, "pocket": 0.4}


def box(**over):
    return sd.box_layout({**VALUES, **over}, 4.951)


def lid(**over):
    return sd.lid_layout({**VALUES, **over}, 1.5, MEASURED)


def sidecar_of(part, layout):
    names = {v.key: "View " + v.key for v in layout}
    placed = {(v.key, d.id): f"D{i}@{names[v.key]}" for v in layout for i, d in enumerate(v.dims)}
    return tpl.make_sidecar(part, r"C:\\base\\" + part + ".SLDPRT", layout, names, placed)


class TestDimensionIds(unittest.TestCase):
    def test_every_dimension_has_an_id_unique_within_its_view(self):
        for layout in (box(), lid()):
            for view in layout:
                ids = [d.id for d in view.dims]
                self.assertTrue(all(ids), view.key)
                self.assertEqual(len(ids), len(set(ids)), view.key)

    def test_ids_do_not_change_when_other_features_are_left_out(self):
        full = {v.key: [d.id for d in v.dims] for v in box()}
        bare = {v.key: [d.id for d in v.dims] for v in box(screws_on=0.0, pedestal_depth=0.0, no_chip=1.0)}
        for key, ids in bare.items():
            for dim_id in ids:
                self.assertIn(dim_id, full[key])

    def test_a_candidate_group_is_named_for_the_group(self):
        ids = [d.id for v in lid() for d in v.dims]
        self.assertIn("sec_h_detail:deep", ids)

    def test_ids_do_not_depend_on_the_values(self):
        a = [d.id for v in box() for d in v.dims]
        b = [d.id for v in box(pcb_length=60.0, pcb_side=30.0) for d in v.dims]
        self.assertEqual(a, b)


class TestSidecar(unittest.TestCase):
    def test_it_records_each_view_and_each_placed_dimension_by_name(self):
        layout = box()
        data = sidecar_of("box", layout)
        self.assertEqual(data["part"], "box")
        self.assertEqual(sorted(data["views"]), sorted(v.key for v in layout))
        self.assertEqual(len(data["dims"]), sum(len(v.dims) for v in layout))
        first = next(iter(data["dims"].values()))
        self.assertIn("name", first)
        self.assertIn("expected", first)

    def test_a_dimension_that_was_not_placed_is_not_recorded(self):
        layout = box()
        names = {v.key: "V" for v in layout}
        data = tpl.make_sidecar("box", "x", layout, names, {})
        self.assertEqual(data["dims"], {})

    def test_it_round_trips_through_a_file(self):
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "box.layout.json")
        data = sidecar_of("box", box())
        tpl.save_sidecar(data, path)
        self.assertEqual(tpl.load_sidecar("box", path), json.loads(json.dumps(data)))

    def test_a_missing_or_broken_sidecar_is_none(self):
        folder = tempfile.mkdtemp()
        self.assertIsNone(tpl.load_sidecar("box", os.path.join(folder, "nope.json")))
        broken = os.path.join(folder, "b.json")
        with open(broken, "w") as f:
            f.write("{not json")
        self.assertIsNone(tpl.load_sidecar("box", broken))
        wrong = os.path.join(folder, "w.json")
        with open(wrong, "w") as f:
            f.write("[1, 2]")
        self.assertIsNone(tpl.load_sidecar("box", wrong))

    def test_the_templates_live_beside_the_base_parts(self):
        self.assertEqual(os.path.dirname(tpl.template_path("box")), str(paths.BASE_PARTS_DIR))
        self.assertTrue(tpl.sidecar_path("box").endswith("box.layout.json"))
        self.assertTrue(tpl.template_path("lid_inside").endswith("lid_inside.SLDDRW"))

    def test_there_is_no_template_until_both_files_are_there(self):
        with mock.patch.object(tpl, "template_path", lambda part: "nowhere.SLDDRW"):
            self.assertFalse(tpl.has_template("box"))


class TestUsable(unittest.TestCase):
    def test_the_layout_a_template_was_made_from_is_usable_as_it_is(self):
        self.assertEqual(tpl.usable(box(), sidecar_of("box", box())), (True, ""))
        self.assertEqual(tpl.usable(lid(), sidecar_of("lid_inside", lid())), (True, ""))

    def test_a_view_the_template_lacks_is_not_usable(self):
        data = sidecar_of("box", box(vents_on=0.0))
        ok, why = tpl.usable(box(), data)
        self.assertFalse(ok)
        self.assertIn("sec_v", why)

    def test_a_moved_section_line_is_not_usable(self):
        data = sidecar_of("box", box())
        ok, why = tpl.usable(box(screw_inset=5.0), data)       # the hole row the A cut follows moves
        self.assertFalse(ok)
        self.assertIn("sec_a", why)

    def test_a_moved_detail_circle_is_not_usable(self):
        data = sidecar_of("lid_inside", lid())
        ok, why = tpl.usable(lid(port_space=8.0), data)
        self.assertFalse(ok)

    def test_a_change_that_only_scales_things_is_usable(self):
        # the overall length moves no cut line and no detail
        data = sidecar_of("box", box())
        self.assertTrue(tpl.usable(box(pcb_length=60.0), data)[0])

    def test_features_a_combination_lacks_do_not_stop_the_template(self):
        # no screws and no vents: fewer views, which the template can simply drop
        data = sidecar_of("box", box())
        self.assertTrue(tpl.usable(box(screws_on=0.0, vents_on=0.0), data)[0])

    def test_a_thinner_floor_moves_the_vent_detail_and_stops_the_template(self):
        # screws off thins the floor, and the groove's circle goes with it
        data = sidecar_of("box", box())
        ok, why = tpl.usable(box(screws_on=0.0), data)
        self.assertFalse(ok)
        self.assertIn("vent_detail", why)


class TestClassify(unittest.TestCase):
    def test_the_same_combination_checks_every_dimension_and_hides_none(self):
        layout = box()
        plan = tpl.classify_dims(layout, sidecar_of("box", layout))
        self.assertEqual(plan["absent"], [])
        self.assertEqual(plan["views_gone"], [])
        self.assertEqual(len(plan["check"]), sum(len(v.dims) for v in layout))

    def test_without_screws_their_section_goes_and_their_dimensions_with_it(self):
        data = sidecar_of("box", box())
        plan = tpl.classify_dims(box(screws_on=0.0), data)
        self.assertEqual(plan["views_gone"], ["sec_a"])
        self.assertTrue(all(not k.startswith("sec_a|") for k in plan["absent"]))   # not listed twice
        self.assertTrue([k for k in plan["absent"] if k.startswith("top|")])       # the plan view's hole dimensions
        self.assertFalse([k for k in plan["check"] if k.startswith("sec_a|")])

    def test_without_a_recess_its_dimensions_are_absent_not_checked(self):
        data = sidecar_of("box", box())
        plan = tpl.classify_dims(box(pedestal_depth=0.0, no_chip=1.0), data)
        self.assertTrue(plan["absent"])
        for key in plan["absent"]:
            self.assertNotIn(key, plan["check"])

    def test_without_vents_the_vent_views_are_gone(self):
        data = sidecar_of("box", box())
        plan = tpl.classify_dims(box(vents_on=0.0), data)
        self.assertEqual(plan["views_gone"], ["sec_v", "vent_detail"])

    def test_expected_values_come_from_this_combination_not_the_template(self):
        data = sidecar_of("box", box())
        plan = tpl.classify_dims(box(pcb_length=60.0), data)
        self.assertEqual(plan["check"][tpl.key_of("top", "top:h.above.1.0")], 72.0)       # 60 + 2 walls + 2 wings
        self.assertEqual(data["dims"][tpl.key_of("top", "top:h.above.1.0")]["expected"], 62.0)


class TestChecks(unittest.TestCase):
    def test_a_value_within_a_hundredth_is_right(self):
        self.assertTrue(tpl.is_right(62.004, 62.0))
        self.assertFalse(tpl.is_right(62.5, 62.0))

    def test_a_dimension_with_no_expected_value_is_never_wrong(self):
        self.assertTrue(tpl.is_right(123.0, None))

    def test_the_note_lists_what_was_left_off_and_is_empty_when_nothing_was(self):
        self.assertEqual(tpl.omitted_note([]), "")
        note = tpl.omitted_note(["a (b)", "c (d)"])
        self.assertIn("NOT SHOWN", note)
        self.assertIn("a (b); c (d)", note)

    def test_a_description_names_the_dimension_and_the_view(self):
        text = tpl.describe("top|dia.right.3.0", "not in this combination")
        self.assertIn("dia.right.3.0", text)
        self.assertIn("top", text)


class TestFallback(unittest.TestCase):
    def test_no_template_means_nothing_is_written_and_the_caller_builds_from_scratch(self):
        with mock.patch.object(tpl, "load_sidecar", lambda part, path=None: None):
            self.assertIsNone(tpl.fill_template(object(), "box", "x.SLDPRT", "out.SLDDRW", "out.pdf", "t",
                                                VALUES, "aluminium", box()))

    def test_an_unusable_template_is_declined_without_touching_solidworks(self):
        data = sidecar_of("box", box(vents_on=0.0))
        logged = []
        with mock.patch.object(tpl, "load_sidecar", lambda part, path=None: data), \
                mock.patch.object(tpl.os.path, "isfile", lambda p: True):
            result = tpl.fill_template(object(), "box", "x.SLDPRT", "out.SLDDRW", "out.pdf", "t", VALUES,
                                       "aluminium", box(), logged.append)
        self.assertIsNone(result)
        self.assertTrue([m for m in logged if "not using the drawing template" in m])


class TestTitleProperties(unittest.TestCase):
    def test_what_changes_per_combination_is_a_property(self):
        props = sd.title_properties("run / combo", "aluminium", "2:1, 2.5:1", "x")
        self.assertEqual(sorted(props), sorted(sd.PROPERTIES))
        self.assertEqual(props["RunCombo"], "run / combo")
        self.assertIn("aluminium", props["Material"])
        self.assertEqual(props["Omitted"], "x")

    def test_the_title_block_notes_read_those_properties(self):
        class Sheet:
            def __init__(self):
                self.texts, self.props = [], None

            def frame(self, *a):
                pass

            def lines(self, *a):
                pass

            def set_properties(self, props):
                self.props = props

            def text(self, x, y, text):
                self.texts.append(text)

        sheet = Sheet()
        sd._title_block(sheet, "box", sd.title_properties("t", "m", "2:1"))
        joined = " ".join(sheet.texts)
        for name in sd.PROPERTIES:
            self.assertIn(f'$PRP:"{name}"', joined)
        self.assertNotIn("run_", joined)


if __name__ == "__main__":
    unittest.main()
