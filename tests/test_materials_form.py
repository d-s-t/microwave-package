"""Tests for reading the materials/boundaries settings off the form.

Each material field is a dropdown of common choices plus a "Custom..."
escape hatch that pairs with a free-text box (see the `material_field`
macro in index.html) -- this is what reads that pair back into a plain
material name, the same way `_solve_settings_from_form` reads the solve
settings.

Run: python -m unittest discover -s tests
"""
import unittest
from unittest import mock

from werkzeug.datastructures import MultiDict

from microwave_package import webapp
from microwave_package.hfss import model_builder


def form(**fields):
    return MultiDict(list(fields.items()))


class TestPresetChoices(unittest.TestCase):
    def test_a_preset_choice_is_used_as_is(self):
        materials = webapp._materials_from_form(form(mat_box="brass"))
        self.assertEqual(materials["mat_box"], "brass")

    def test_an_empty_submission_falls_back_to_the_usual_defaults(self):
        materials = webapp._materials_from_form(form())
        self.assertEqual(materials["mat_box"], "aluminum")
        self.assertEqual(materials["mat_pcb"], "Rogers TMM 10i (tm)")
        self.assertEqual(materials["mat_chip"], "silicon")
        self.assertEqual(materials["mat_vacuum"], "vacuum")
        self.assertEqual(materials["mat_conductor_pcb"], "copper")
        self.assertEqual(materials["mat_conductor_chip"], "niobium")


class TestCustomMaterial(unittest.TestCase):
    def test_custom_reads_the_paired_text_box(self):
        materials = webapp._materials_from_form(form(
            mat_pcb=model_builder.CUSTOM_MATERIAL, mat_pcb_custom="Rogers RO3010",
        ))
        self.assertEqual(materials["mat_pcb"], "Rogers RO3010")

    def test_custom_with_a_blank_box_falls_back_rather_than_posting_the_sentinel(self):
        """The sentinel itself must never reach HFSS as a material name."""
        materials = webapp._materials_from_form(form(
            mat_chip=model_builder.CUSTOM_MATERIAL, mat_chip_custom="  ",
        ))
        self.assertEqual(materials["mat_chip"], "silicon")

    def test_a_stray_custom_box_with_no_custom_selection_is_ignored(self):
        """Only read when the dropdown actually says "Custom..." -- a
        leftover value in a hidden box must not silently win."""
        materials = webapp._materials_from_form(form(
            mat_chip="sapphire", mat_chip_custom="something else entirely",
        ))
        self.assertEqual(materials["mat_chip"], "sapphire")


class TestWirebondMaterialFromForm(unittest.TestCase):
    def test_gold_and_aluminum_both_round_trip(self):
        for material in model_builder.WIREBOND_MATERIALS:
            materials = webapp._materials_from_form(form(mat_wirebond=material))
            self.assertEqual(materials["mat_wirebond"], material)

    def test_anything_outside_the_two_choices_falls_back(self):
        """Closed choice, unlike the other materials -- there is no
        custom box for this one, so an unrecognised value (a tampered
        submission, or an old bookmark) falls back rather than reaching
        HFSS with a name the wirebond process cannot actually load."""
        materials = webapp._materials_from_form(form(mat_wirebond="copper"))
        self.assertEqual(materials["mat_wirebond"], model_builder.MATERIAL_FALLBACKS["mat_wirebond"])

    def test_missing_entirely_falls_back(self):
        materials = webapp._materials_from_form(form())
        self.assertEqual(materials["mat_wirebond"], model_builder.MATERIAL_FALLBACKS["mat_wirebond"])


class TestIncludeWirebonds(unittest.TestCase):
    def test_present_means_true(self):
        materials = webapp._materials_from_form(form(include_wirebonds="on"))
        self.assertTrue(materials["include_wirebonds"])

    def test_absent_means_false(self):
        """Same checkbox convention as perfect_e_box/use_altium: an
        unticked box sends nothing at all."""
        materials = webapp._materials_from_form(form())
        self.assertFalse(materials["include_wirebonds"])


class TestRenderingAnOutOfListMaterial(unittest.TestCase):
    """A run started before this curated-list form existed (or one whose
    material genuinely isn't on the list) must still render its real
    value -- silently snapping it to the nearest preset would change what
    the next submission of that same form actually builds.
    """

    def setUp(self):
        self.client = webapp.app.test_client()

    def _get_with_materials(self, **overrides):
        # index() gets its starting settings from _form_starting_point (a
        # real run on disk, normally) -- patching that one function is
        # simpler and more direct than fabricating a whole run just to
        # render the form against a chosen `materials` dict.
        from microwave_package import pipeline

        materials = {**pipeline.DEFAULT_ANALYSIS_SETTINGS["materials"], **overrides}
        settings = {**pipeline.DEFAULT_ANALYSIS_SETTINGS, "materials": materials}
        with mock.patch.object(
            webapp, "_form_starting_point",
            return_value=(settings, {}, None),
        ):
            return self.client.get("/").get_data(as_text=True)

    def test_a_material_outside_the_curated_list_selects_custom_and_keeps_its_value(self):
        html = self._get_with_materials(mat_chip="diamond")
        self.assertRegex(
            html,
            r'<input type="text" name="mat_chip_custom"[^>]*value="diamond"(?![^>]*hidden)[^>]*>',
        )

    def test_a_curated_choice_still_selects_the_dropdown_not_custom(self):
        html = self._get_with_materials(mat_chip="sapphire")
        self.assertRegex(html, r'<option value="sapphire"\s+selected>sapphire</option>')
        self.assertRegex(html, r'name="mat_chip_custom"[^>]*value=""[^>]*hidden>')


if __name__ == "__main__":
    unittest.main()
