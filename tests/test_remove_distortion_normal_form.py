"""base_parts/remove_distortion.py: the one form its post-delete check compares equations in.

SolidWorks writes an equation differently after an in-memory delete (a full-width "@",
a "@box.Part" suffix on a feature's name), so comparing raw text reports the same
equations as both missing and extra. These tests pin the normal form, and the owner
and comment handling the removal depends on. Nothing here needs SolidWorks.
"""
import importlib.util
import os
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "base_parts", "remove_distortion.py")


def load_script():
    spec = importlib.util.spec_from_file_location("remove_distortion", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rd = load_script()


class NormalEquationTests(unittest.TestCase):

    def test_full_width_at_and_part_suffix_match_the_plain_form(self):
        before = '"D1@Sketch6@box.Part"="vent_width" / 2'
        after = '"D1＠Sketch6" = "vent_width" / 2'
        self.assertEqual(rd.normal_equation(before), rd.normal_equation(after))

    def test_a_different_value_does_not_match(self):
        self.assertNotEqual(rd.normal_equation('"D1@Sketch6"="vent_width"/2'),
                            rd.normal_equation('"D1@Sketch6"="vent_width"/3'))

    def test_a_comment_is_ignored(self):
        with_comment = '"no_chip" = 0 \' (boolean) when set to 1, the bare board'
        self.assertEqual(rd.normal_equation(with_comment), rd.normal_equation('"no_chip" = 0'))

    def test_two_quoted_names_on_one_line_are_not_merged(self):
        text = '"D1@Sketch3" = "route_width" / 2 + "distortion_radius" + "distortion_y"'
        self.assertIn('"D1@Sketch3"', rd.ascii_form(text))
        self.assertIn('+"distortion_radius"+"distortion_y"', rd.normal_equation(text))


class OwnerOfTests(unittest.TestCase):

    def test_a_dimension_belongs_to_its_sketch(self):
        self.assertEqual(rd.owner_of('"D2@Sketch3" = "chip_length" / 2'), "Sketch3")

    def test_the_part_suffix_does_not_change_the_owner(self):
        self.assertEqual(rd.owner_of('"D1@Sketch6@box.Part"="vent_width" / 2'), "Sketch6")

    def test_a_suppression_equation_belongs_to_its_feature(self):
        self.assertEqual(rd.owner_of('"Boss-Extrude7"= "no_distortion"'), "Boss-Extrude7")

    def test_a_global_is_its_own_owner(self):
        self.assertEqual(rd.owner_of('"no_distortion" = 1 \' (boolean)'), "no_distortion")

    def test_a_feature_name_with_spaces_is_kept_whole(self):
        name = "CSK for M2 Countersunk Flat Head Screw1"
        self.assertEqual(rd.owner_of(f'"D1@{name}" = 2'), name)


if __name__ == "__main__":
    unittest.main()
