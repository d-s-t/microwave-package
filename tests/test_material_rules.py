"""Tests for how an imported solid's name picks its material.

This is worth testing precisely because getting it wrong is invisible.
The old if/elif chain would have handed `pcb_copper` the Rogers
permittivity, and nothing would have complained -- the model would just
have solved to the wrong frequencies.

HFSS is not involved: a tiny stand-in records what would have been
assigned, so the rules can be checked without AEDT.

Run: python -m unittest discover -s tests
"""
import os
import re
import unittest

from microwave_package.hfss import model_builder


class FakeObject:
    def __init__(self, name):
        self.name = name
        self.material_name = None


class FakeMaterial:
    """What PyAEDT's `add_material` hands back for a name the library
    lacks: a BLANK material -- permittivity 1, conductivity 0 -- until
    someone sets its properties."""

    def __init__(self, name):
        self.name = name
        self.permittivity = 1
        self.conductivity = 0

    def update(self):
        pass


class FakeLibrary:
    STOCK = {"copper", "aluminum", "gold", "silicon", "vacuum", "rogers tmm 10i (tm)"}

    def __init__(self, project=()):
        self.project = {m.name.lower(): m for m in project}

    def exists_material(self, name):
        return self.project.get(name.lower(), False)

    def add_material(self, name):
        self.project[name.lower()] = FakeMaterial(name)
        return self.project[name.lower()]

    def knows(self, name):
        return name.lower() in self.STOCK or name.lower() in self.project


class FakeHfss:
    """Records assignments instead of performing them."""

    def __init__(self, names):
        self._objects = [FakeObject(n) for n in names]
        self.perfect_e = []
        self.subtractions = []
        self.materials = FakeLibrary()

    @property
    def modeler(self):
        return self

    @property
    def solid_objects(self):
        return self._objects

    def assign_perfect_e(self, assignment):
        self.perfect_e.append(assignment)

    def subtract(self, target, tools, keep_originals=False):
        self.subtractions.append((target, list(tools), keep_originals))


SETTINGS = {
    "mat_box": "aluminum",
    "mat_pcb": "Rogers TMM 10i (tm)",
    "mat_chip": "silicon",
    "mat_vacuum": "vacuum",
    "mat_conductor_pcb": "copper",
    "mat_conductor_chip": "niobium",
    "mat_wirebond": "gold",
    "perfect_e_box": True,
    "perfect_e_conductors": True,
}


def assign(names, settings=None, log=lambda _msg: None):
    hfss = FakeHfss(names)
    objects = model_builder.assign_materials_and_boundaries(
        hfss, dict(settings or SETTINGS), log=log
    )
    return hfss, objects


class TestTheOriginalBodies(unittest.TestCase):
    """The bodies that existed before copper did must keep behaving."""

    def test_each_gets_its_own_material(self):
        hfss, _ = assign(["vacuum", "PCB", "chip", "box", "lid_inside"])
        got = {o.name: o.material_name for o in hfss.modeler.solid_objects}
        self.assertEqual(got["vacuum"], "vacuum")
        self.assertEqual(got["PCB"], "Rogers TMM 10i (tm)")
        self.assertEqual(got["chip"], "silicon")
        self.assertEqual(got["box"], "aluminum")
        self.assertEqual(got["lid_inside"], "aluminum")

    def test_lid_inside_is_metal_not_silicon(self):
        """`lid_inside` contains no `chip`/`pcb` substring, but this
        guards the same failure mode the old `chip_lid` rule did: the
        enclosure must never fall through to a die/board rule."""
        hfss, _ = assign(["lid_inside"])
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "aluminum")

    def test_perfect_e_on_the_enclosure(self):
        hfss, _ = assign(["box", "lid_inside", "PCB"])
        self.assertEqual(sorted(hfss.perfect_e), ["box", "lid_inside"])


class TestCopperDoesNotBecomeSubstrate(unittest.TestCase):
    """The regression this refactor exists to prevent."""

    def test_pcb_copper_is_not_given_the_pcb_dielectric(self):
        hfss, _ = assign(["pcb_copper_top"])
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "copper")

    def test_pcb_vias_are_conductor(self):
        hfss, _ = assign(["pcb_vias"])
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "copper")

    def test_pcb_substrate_is_still_the_dielectric(self):
        hfss, _ = assign(["pcb_substrate"])
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "Rogers TMM 10i (tm)")

    def test_chip_conductor_is_not_given_silicon(self):
        hfss, _ = assign(["chip_conductor"])
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "niobium")

    def test_chip_substrate_is_still_silicon(self):
        hfss, _ = assign(["chip_substrate"])
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "silicon")

    def test_a_full_copper_model_assigns_every_body_correctly(self):
        names = [
            "vacuum", "box", "lid_inside",
            "pcb_substrate", "pcb_copper_top", "pcb_copper_bottom", "pcb_vias",
            "chip_substrate", "chip_conductor", "wirebond_1",
            "connector_1_outer", "connector_1_center", "connector_1_dielectric",
        ]
        hfss, _ = assign(names)
        got = {o.name: o.material_name for o in hfss.modeler.solid_objects}
        self.assertEqual(got["pcb_substrate"], "Rogers TMM 10i (tm)")
        self.assertEqual(got["pcb_copper_top"], "copper")
        self.assertEqual(got["pcb_copper_bottom"], "copper")
        self.assertEqual(got["pcb_vias"], "copper")
        self.assertEqual(got["chip_substrate"], "silicon")
        self.assertEqual(got["chip_conductor"], "niobium")
        self.assertEqual(got["wirebond_1"], "gold")
        # All three connector bodies get their material in place_connectors
        # (brass conductors, LCP bead), not from the rules, so the rule
        # layer leaves them untouched here.
        self.assertIsNone(got["connector_1_outer"])
        self.assertIsNone(got["connector_1_center"])
        self.assertIsNone(got["connector_1_dielectric"])


class TestPerfectEToggles(unittest.TestCase):
    def test_conductors_get_perfect_e_by_default(self):
        hfss, _ = assign(["pcb_copper_top", "chip_conductor"])
        self.assertEqual(
            sorted(hfss.perfect_e), ["chip_conductor", "pcb_copper_top"],
        )

    def test_the_connector_never_gets_a_perfect_e_boundary(self):
        """It carries its real vendor metal -- brass on the conductors,
        LCP on the bead, all set in place_connectors -- so none of its
        three bodies is a PEC surface."""
        hfss, objects = assign(
            ["connector_1_outer", "connector_1_center", "connector_1_dielectric"]
        )
        self.assertEqual(hfss.perfect_e, [])
        self.assertIn("connector", objects)
        self.assertIn("connector_dielectric", objects)

    def test_turning_the_conductor_toggle_off_leaves_the_materials(self):
        settings = dict(SETTINGS, perfect_e_conductors=False)
        hfss, _ = assign(["pcb_copper_top", "chip_conductor"], settings)
        self.assertEqual(hfss.perfect_e, [])
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "copper")
        self.assertEqual(hfss.modeler.solid_objects[1].material_name, "niobium")

    def test_substrates_never_get_perfect_e(self):
        hfss, _ = assign(["pcb_substrate", "chip_substrate", "vacuum"])
        self.assertEqual(hfss.perfect_e, [])


class TestUnmatchedBodies(unittest.TestCase):
    def test_an_unknown_body_is_warned_about_not_silently_defaulted(self):
        """A renamed or mis-exported body must be loud. Silence here
        shows up as a puzzling frequency shift days later."""
        messages = []
        hfss, _ = assign(["mystery_part"], log=messages.append)
        self.assertIsNone(hfss.modeler.solid_objects[0].material_name)
        self.assertTrue(any("mystery_part" in m for m in messages))
        self.assertTrue(any("WARNING" in m for m in messages))

    def test_no_warning_when_everything_matches(self):
        messages = []
        assign(["vacuum", "box", "pcb_substrate"], log=messages.append)
        self.assertEqual(messages, [])


class TestObjectsDict(unittest.TestCase):
    def test_keys_the_rest_of_the_pipeline_relies_on(self):
        """post_processing looks up 'vacuum' by name, so the key must
        survive the rename of the underlying body."""
        _, objects = assign(["vacuum", "pcb_substrate", "chip_substrate", "box"])
        self.assertEqual(objects["vacuum"].name, "vacuum")
        self.assertEqual(objects["pcb"].name, "pcb_substrate")
        self.assertEqual(objects["chip"].name, "chip_substrate")
        self.assertEqual(objects["box"].name, "box")



class TestSupersededSolids(unittest.TestCase):
    """The substrates now come from the layout, so the imported copies
    must go -- two solids in one region make HFSS either error or resolve
    it silently by material priority, and the silent case still solves."""

    class Modeler:
        def __init__(self, names):
            self._objects = [FakeObject(n) for n in names]
            self.deleted = []

        @property
        def modeler(self):
            return self

        @property
        def solid_objects(self):
            return self._objects

        def delete(self, names):
            self.deleted.extend(names)

    def _drop(self, names):
        m = self.Modeler(names)
        model_builder._drop_superseded_solids(m, lambda _msg: None)
        return m.deleted

    def test_the_imported_pcb_and_chip_are_removed(self):
        self.assertEqual(
            sorted(self._drop(["PCB", "chip", "box", "lid", "vacuum"])),
            ["PCB", "chip"],
        )

    def test_lid_inside_survives(self):
        """It is enclosure, not die, and shares the "lid" guard."""
        self.assertEqual(self._drop(["lid_inside", "box"]), [])

    def test_the_enclosure_and_vacuum_survive(self):
        self.assertEqual(self._drop(["box", "lid_inside", "vacuum"]), [])

    def test_bodies_we_build_ourselves_are_not_targets(self):
        """They are created after this runs, but the names must not
        collide even so."""
        self.assertEqual(
            self._drop(["pcb_substrate", "chip_substrate", "pcb_copper_top"]), []
        )


class TestChipCopperIsAConductor(unittest.TestCase):
    """Regression: the bodies the copper builder actually makes.

    The earlier tests only ever used a body called `chip_conductor`,
    which no part of the pipeline produces. The real names are
    `chip_copper_top` and `chip_copper_bottom`, and with no rule of their
    own they matched the bare "chip" rule and were built as SILICON -- a
    conductor modelled as a dielectric, which solves happily and is
    completely wrong.
    """

    def test_chip_copper_is_not_silicon(self):
        hfss, _ = assign(["chip_copper_top", "chip_copper_bottom"])
        for obj in hfss.modeler.solid_objects:
            self.assertEqual(obj.material_name, "niobium", obj.name)

    def test_chip_copper_gets_perfect_e(self):
        hfss, _ = assign(["chip_copper_top"])
        self.assertEqual(hfss.perfect_e, ["chip_copper_top"])

    def test_chip_vias_are_conductor(self):
        hfss, _ = assign(["chip_vias"])
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "niobium")

    def test_every_name_the_builder_produces_is_covered(self):
        """Ties the rules to the builder, so a rename on either side
        cannot quietly go back to silent mis-assignment."""
        produced = [
            "pcb_substrate", "pcb_copper_top", "pcb_copper_bottom", "pcb_vias",
            "chip_substrate", "chip_copper_top", "chip_copper_bottom", "wirebond",
        ]
        expected = {
            "pcb_substrate": "Rogers TMM 10i (tm)",
            "pcb_copper_top": "copper",
            "pcb_copper_bottom": "copper",
            "pcb_vias": "copper",
            "chip_substrate": "silicon",
            "chip_copper_top": "niobium",
            "chip_copper_bottom": "niobium",
            "wirebond": "gold",
        }
        hfss, _ = assign(produced)
        got = {o.name: o.material_name for o in hfss.modeler.solid_objects}
        self.assertEqual(got, expected)


class TestWirebondMaterial(unittest.TestCase):
    """The wirebond's own material setting, separate from the PCB
    conductor it used to piggyback on.

    Before this, a wirebond matched the bare "wirebond" rule with
    `mat_conductor_pcb` as its material key -- so there was no way to set
    a bond's material without also changing the board's copper. Gold and
    aluminum are the only two a bonder actually loads (see
    model_builder.WIREBOND_MATERIALS), so `mat_wirebond` is the closed
    setting for it.
    """

    def test_wirebond_uses_its_own_setting_not_the_pcb_conductor(self):
        settings = dict(SETTINGS, mat_conductor_pcb="copper", mat_wirebond="aluminum")
        hfss, _ = assign(["wirebond_1", "pcb_copper_top"], settings)
        got = {o.name: o.material_name for o in hfss.modeler.solid_objects}
        self.assertEqual(got["wirebond_1"], "aluminum")
        self.assertEqual(got["pcb_copper_top"], "copper")

    def test_changing_the_pcb_conductor_does_not_move_the_wirebond(self):
        settings = dict(SETTINGS, mat_conductor_pcb="gold", mat_wirebond="aluminum")
        hfss, _ = assign(["wirebond_1"], settings)
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "aluminum")

    def test_both_real_options_are_settable(self):
        for material in model_builder.WIREBOND_MATERIALS:
            hfss, _ = assign(["wirebond_1"], dict(SETTINGS, mat_wirebond=material))
            self.assertEqual(hfss.modeler.solid_objects[0].material_name, material)

    def test_an_unset_wirebond_material_falls_back_to_aluminum(self):
        """Matches model_builder.MATERIAL_FALLBACKS["mat_wirebond"]:
        aluminum superconducts at millikelvin, gold does not, so silently
        landing on gold would be the wrong direction to fail in."""
        settings = {k: v for k, v in SETTINGS.items() if k != "mat_wirebond"}
        hfss, _ = assign(["wirebond_1"], settings)
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "aluminum")

    def test_the_wirebond_still_gets_perfect_e_like_any_other_conductor(self):
        hfss, _ = assign(["wirebond_1"])
        self.assertEqual(hfss.perfect_e, ["wirebond_1"])


class TestMaterialFallbacks(unittest.TestCase):
    """An empty material setting is not "leave it alone".

    A run whose settings predate a key posts an empty string for it, and
    an unset material leaves the body on HFSS's default -- vacuum, which
    in the tree is indistinguishable from a deliberate choice. That is
    how copper, vias and bonds all came out as vacuum.
    """

    def test_a_missing_key_falls_back_rather_than_leaving_vacuum(self):
        settings = {k: v for k, v in SETTINGS.items() if k != "mat_conductor_pcb"}
        hfss, _ = assign(["pcb_copper_top"], settings)
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "copper")

    def test_an_empty_string_falls_back_too(self):
        """The actual failure: the form posts "" for a blank field."""
        settings = dict(SETTINGS, mat_conductor_pcb="", mat_conductor_chip="")
        hfss, _ = assign(["pcb_copper_top", "chip_copper_top"], settings)
        got = {o.name: o.material_name for o in hfss.modeler.solid_objects}
        self.assertEqual(got["pcb_copper_top"], "copper")
        self.assertEqual(got["chip_copper_top"], "niobium")

    def test_falling_back_is_announced(self):
        settings = dict(SETTINGS, mat_conductor_pcb="")
        messages = []
        assign(["pcb_copper_top"], settings, log=messages.append)
        self.assertTrue(any("mat_conductor_pcb" in m for m in messages))

    def test_an_explicit_value_is_never_overridden(self):
        settings = dict(SETTINGS, mat_conductor_pcb="gold")
        hfss, _ = assign(["pcb_copper_top"], settings)
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "gold")

    def test_every_material_key_the_rules_use_has_a_fallback(self):
        """Structural: a rule naming a key with no fallback can still
        strand a body on vacuum. A `None` key is exempt -- it means the
        body's material is set where it is created (the connector bead)."""
        used = {rule[2] for rule in model_builder.MATERIAL_RULES if rule[2] is not None}
        missing = used - set(model_builder.MATERIAL_FALLBACKS)
        self.assertEqual(missing, set(), f"no fallback for {missing}")


class PickyObject:
    """A body that, like AEDT's, silently keeps its material when handed
    a name the library does not know."""

    def __init__(self, name, library):
        self.name = name
        self._library = library
        self._material = "vacuum"

    @property
    def material_name(self):
        return self._material

    @material_name.setter
    def material_name(self, value):
        if self._library.knows(value):
            self._material = value


class HfssWithALibrary(FakeHfss):
    def __init__(self, names, project=()):
        super().__init__([])
        self.materials = FakeLibrary(project)
        self._objects = [PickyObject(n, self.materials) for n in names]


class TestMetalsMissingFromTheLibrary(unittest.TestCase):
    """Niobium is not in the stock library, and adding it by name makes a
    blank material -- vacuum under the name "niobium". Every chip
    conductor was that until 2026-09-28."""

    def assign(self, names, settings=None, project=()):
        hfss = HfssWithALibrary(names, project)
        messages = []
        model_builder.assign_materials_and_boundaries(
            hfss, dict(settings or SETTINGS), log=messages.append)
        return hfss, messages

    def test_niobium_is_created_as_a_metal(self):
        hfss, _ = self.assign(["chip_copper_top"])
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "niobium")
        self.assertEqual(hfss.materials.project["niobium"].conductivity,
                         model_builder.MISSING_FROM_LIBRARY["niobium"]["conductivity"])

    def test_it_is_conductive_enough_not_to_be_solved_inside(self):
        """HFSS meshes and solves inside anything below 1e5 S/m -- which
        for a 200 nm film is a layer of elements a fraction of a micron
        thick."""
        self.assertGreater(model_builder.MISSING_FROM_LIBRARY["niobium"]["conductivity"], 1e5)

    def test_a_blank_niobium_left_in_the_project_is_corrected(self):
        """A project an earlier build left behind already carries the
        blank one, and it would be assigned without complaint."""
        hfss, _ = self.assign(["chip_copper_top"], project=[FakeMaterial("niobium")])
        self.assertGreater(hfss.materials.project["niobium"].conductivity, 1e5)

    def test_an_unknown_metal_is_the_stand_in_not_a_blank_material(self):
        settings = dict(SETTINGS, mat_conductor_chip="unobtainium")
        hfss, messages = self.assign(["chip_copper_top"], settings)
        self.assertEqual(hfss.modeler.solid_objects[0].material_name,
                         model_builder.LAST_RESORT_CONDUCTOR)
        self.assertNotIn("unobtainium", hfss.materials.project)
        self.assertTrue(any("WARNING" in m and "unobtainium" in m for m in messages))

    def test_an_unknown_dielectric_is_the_stand_in_too(self):
        settings = dict(SETTINGS, mat_pcb="mystery laminate")
        hfss, _ = self.assign(["pcb_substrate"], settings)
        self.assertEqual(hfss.modeler.solid_objects[0].material_name,
                         model_builder.LAST_RESORT_DIELECTRIC)
        self.assertNotIn("mystery laminate", hfss.materials.project)

    def test_making_it_is_said_in_the_run_log(self):
        """It changes the physics of every model, so it is on record."""
        _, messages = self.assign(["chip_copper_top"])
        self.assertTrue(any("'niobium' is not in the AEDT library" in m for m in messages))

    def test_one_that_cannot_be_made_right_is_not_used(self):
        """The project may hold the blank one under that very name."""
        class Broken(FakeMaterial):
            def update(self):
                raise RuntimeError("AEDT said no")

        hfss, messages = self.assign(["chip_copper_top"], project=[Broken("niobium")])
        self.assertEqual(hfss.modeler.solid_objects[0].material_name,
                         model_builder.LAST_RESORT_CONDUCTOR)
        self.assertTrue(any("WARNING" in m and "niobium" in m for m in messages))

    def test_sic_is_created_as_a_dielectric(self):
        """The library's SiC entries are thermal only: no permittivity."""
        settings = dict(SETTINGS, mat_chip="SiC")
        hfss, _ = self.assign(["chip_substrate"], settings)
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "SiC")
        self.assertGreater(hfss.materials.project["sic"].permittivity, 9)


def _aedt_stock_library():
    """AEDT's stock material library as {lowercase name: its block}, or
    None when there is no AEDT on this machine."""
    roots = [v for k, v in os.environ.items() if k.upper().startswith(("ANSYSEM_ROOT", "ANSYSEMSV_ROOT"))]
    roots += [r"C:\Program Files\ANSYS Inc\ANSYS Student\v252\AnsysEM",
              r"C:\Program Files\ANSYS Inc\v252\AnsysEM"]
    for root in roots:
        path = os.path.join(root, "syslib", "Materials.amat")
        if os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            return {m.group(1).lower(): m.group(2) for m in re.finditer(
                r"^\$begin '([^']+)'\n(.*?)^\$end '\1'", text, re.M | re.S)}
    return None


class TestEveryOfferedMaterialIsReal(unittest.TestCase):
    """Every material the form offers or falls back to is one AEDT's
    library has with electromagnetic properties, or one
    MISSING_FROM_LIBRARY makes. "GaAs", "SiC" and "Rogers RT/duroid
    6010LM (tm)" were offered and were none of those -- each a vacuum
    substrate in the model."""

    def test_against_the_installed_library(self):
        library = _aedt_stock_library()
        if library is None:
            self.skipTest("no AEDT material library on this machine")
        offered = {name for names in model_builder.MATERIAL_OPTIONS.values() for name in names}
        offered |= set(model_builder.MATERIAL_FALLBACKS.values())
        offered |= set(model_builder.WIREBOND_MATERIALS)
        offered |= {model_builder.LAST_RESORT_CONDUCTOR, model_builder.LAST_RESORT_DIELECTRIC}
        for name in sorted(offered):
            if name.lower() in model_builder.MISSING_FROM_LIBRARY:
                continue
            block = library.get(name.lower())
            self.assertIsNotNone(block, f"{name!r} is not in AEDT's library")
            # \b keeps thermal_conductivity out: "_" is a word character.
            self.assertRegex(block, r"\b(permittivity|conductivity)\b",
                             f"{name!r} has no electromagnetic properties in AEDT's library")


class TestRuleTableIsSane(unittest.TestCase):
    def test_defined_exactly_once(self):
        """It was defined twice for a while, and the second, stale copy
        silently won -- which made a fix to the first one do nothing."""
        import inspect

        source = inspect.getsource(model_builder)
        self.assertEqual(source.count("\nMATERIAL_RULES = ["), 1)

    def test_specific_rules_precede_the_bare_ones(self):
        patterns = [rule[0] for rule in model_builder.MATERIAL_RULES]
        for specific, bare in (("chip_copper", "chip"), ("chip_substrate", "chip"),
                               ("pcb_copper", "pcb"), ("pcb_substrate", "pcb"),
                               ("dielectric", "connector")):
            self.assertLess(patterns.index(specific), patterns.index(bare),
                            f"{specific} must be tested before {bare}")

    def test_the_connector_rules_carry_no_material_or_boundary_of_their_own(self):
        """place_connectors sets brass on the conductors and LCP on the
        bead; a real material key here would double-assign and log a
        spurious fallback NOTE every run. Neither body is perfect-E."""
        for pattern in ("dielectric", "connector"):
            rule = next(r for r in model_builder.MATERIAL_RULES if r[0] == pattern)
            self.assertIsNone(rule[2], f"the {pattern} rule must not name a material")
            self.assertIsNone(rule[3], f"the {pattern} rule must not add a perfect-E boundary")


class TestMaterialCatalogsAreSane(unittest.TestCase):
    """The web form's dropdown catalogs (model_builder.MATERIAL_OPTIONS /
    WIREBOND_MATERIALS) -- structural checks so a future edit cannot
    quietly break what webapp.py and index.html both assume about them.
    """

    def test_wirebond_materials_is_exactly_the_two_real_choices(self):
        self.assertEqual(set(model_builder.WIREBOND_MATERIALS), {"gold", "aluminum"})

    def test_the_wirebond_fallback_is_one_of_the_two_choices(self):
        self.assertIn(
            model_builder.MATERIAL_FALLBACKS["mat_wirebond"], model_builder.WIREBOND_MATERIALS
        )

    def test_every_curated_material_key_has_a_fallback_among_its_own_options(self):
        """The fallback is what a blank/missing setting resolves to (see
        TestMaterialFallbacks), so it would be a silent trap for it to be
        something the form's own dropdown doesn't offer."""
        for key, options in model_builder.MATERIAL_OPTIONS.items():
            self.assertIn(
                model_builder.MATERIAL_FALLBACKS[key], options,
                f"{key}'s fallback is not among its own MATERIAL_OPTIONS",
            )

    def test_no_curated_option_list_is_empty(self):
        for key, options in model_builder.MATERIAL_OPTIONS.items():
            self.assertTrue(options, f"{key} has no options")


class TestCavityFrame(unittest.TestCase):
    """Where the board's centre is taken from.

    Computing it as half of (pcb_length + 2*wall_width) ignores the
    mounting wings, which extend the assembly, so everything built from
    that datum lands half a wing out along X. Measuring the cavity
    instead is right whatever else the mechanical design carries.
    """

    class Solid:
        def __init__(self, name, box):
            self.name = name
            self.bounding_box = box
            self.material_name = None

    class Modeler:
        def __init__(self, solids):
            self._solids = solids

        @property
        def modeler(self):
            return self

        @property
        def solid_objects(self):
            return self._solids

    def _frame(self, solids, fx=14.0, fz=6.0, log=lambda _m: None):
        return model_builder.cavity_frame(self.Modeler(solids), fx, fz, log=log)

    def test_the_centre_comes_from_the_cavity_not_the_equations(self):
        """A board with wings: the cavity centre is 19, while the
        equations would have said 14."""
        frame = self._frame([self.Solid("vacuum", [5.0, 1.0, 1.0, 33.0, 2.0, 11.0])])
        self.assertAlmostEqual(frame.x_offset, 19.0)
        self.assertAlmostEqual(frame.z_offset, 6.0)

    def test_the_box_is_used_when_there_is_no_vacuum(self):
        frame = self._frame([self.Solid("box", [0.0, 0.0, 0.0, 38.0, 3.0, 12.0])])
        self.assertAlmostEqual(frame.x_offset, 19.0)
        self.assertAlmostEqual(frame.z_offset, 6.0)

    def test_the_vacuum_wins_over_the_box(self):
        """The cavity is what the board sits in; the box includes walls
        and wings."""
        frame = self._frame([
            self.Solid("box", [0.0, 0.0, 0.0, 38.0, 3.0, 12.0]),
            self.Solid("vacuum", [6.0, 1.0, 1.0, 32.0, 2.0, 11.0]),
        ])
        self.assertAlmostEqual(frame.x_offset, 19.0)

    def test_falling_back_is_loud(self):
        """Silently using the computed value is how the wings were
        missed in the first place."""
        messages = []
        frame = self._frame([self.Solid("lid", [0, 0, 0, 1, 1, 1])],
                            log=messages.append)
        self.assertAlmostEqual(frame.x_offset, 14.0)
        self.assertTrue(any("WARNING" in m for m in messages))
        self.assertTrue(any("wing" in m for m in messages))

    def test_an_unreadable_bounding_box_falls_back(self):
        class Broken:
            name = "vacuum"

            @property
            def bounding_box(self):
                raise RuntimeError("cannot measure")

        messages = []
        frame = self._frame([Broken()], log=messages.append)
        self.assertAlmostEqual(frame.x_offset, 14.0)
        self.assertTrue(any("WARNING" in m for m in messages))


class TestBuildVacuum(unittest.TestCase):
    """The air fill, built from box's own measured envelope now instead
    of an imported vacuum.SLDPRT -- which turned out not to be linked to
    default.txt at all, so nothing kept it in step with a swept box."""

    class Box:
        def __init__(self, box):
            self.bounding_box = list(box)
            self.name = None

    class Modeler:
        def __init__(self, box_solid):
            self._box = box_solid
            self.created = []

        @property
        def modeler(self):
            return self

        def __getitem__(self, name):
            if name == "box":
                return self._box
            raise KeyError(name)

        def create_box(self, origin, sizes):
            obj = TestBuildVacuum.Box([
                origin[0], origin[1], origin[2],
                origin[0] + sizes[0], origin[1] + sizes[1], origin[2] + sizes[2],
            ])
            self.created.append(obj)
            return obj

    @staticmethod
    def _frame_and_stack():
        from microwave_package.design import copper_geometry as cg
        frame = cg.Frame(x_offset=19.0, z_offset=6.0)
        stack = cg.Stack(pcb_bottom=1.0, pcb_top=1.381, chip_bottom=1.0, chip_top=1.381,
                         copper_thickness=0.035, chip_metal_thickness=0.0002)
        return frame, stack

    def test_spans_the_pcb_footprint_and_box_own_height(self):
        modeler = self.Modeler(self.Box([0.0, 0.0, 0.0, 38.0, 1.951, 12.0]))
        frame, stack = self._frame_and_stack()
        name = model_builder.build_vacuum(
            modeler, frame, stack, pcb_half_length=13.0, pcb_half_side=5.0, log=lambda _m: None,
        )
        self.assertEqual(name, "vacuum")
        vac = modeler.created[0]
        self.assertEqual(vac.name, "vacuum")
        x0, y0, z0, x1, y1, z1 = vac.bounding_box
        # X/Z: the PCB's own footprint, centred on the frame -- not box's
        # outer envelope, which also carries the mounting wings.
        self.assertAlmostEqual(x0, 6.0)
        self.assertAlmostEqual(x1, 32.0)
        self.assertAlmostEqual(z0, 1.0)
        self.assertAlmostEqual(z1, 11.0)
        # Y: box's own measured bottom and top, whatever floor or
        # pedestal shape sits inside them -- carve_vacuum cuts that out.
        self.assertAlmostEqual(y0, 0.0)
        self.assertAlmostEqual(y1, 1.951)

    def test_an_unmeasurable_box_is_a_loud_no_op(self):
        class Broken:
            @property
            def bounding_box(self):
                raise RuntimeError("cannot measure")

        class BrokenModeler:
            @property
            def modeler(self):
                return self

            def __getitem__(self, name):
                return Broken()

        frame, stack = self._frame_and_stack()
        messages = []
        result = model_builder.build_vacuum(
            BrokenModeler(), frame, stack,
            pcb_half_length=13.0, pcb_half_side=5.0, log=messages.append,
        )
        self.assertIsNone(result)
        self.assertTrue(any("WARNING" in m for m in messages))


class TestPlaceLidInside(unittest.TestCase):
    """Where lid_inside ends up, with no SolidWorks mate to put it there.

    The AEDT-side equivalent of a mate: measure the imported body's real
    bounding box, then move it onto the reference points the design
    wants -- centred on the board's frame, resting on the board's copper.
    """

    class Solid:
        def __init__(self, name, box, modeler):
            self._name = name
            self.bounding_box = list(box)
            self._modeler = modeler

        @property
        def name(self):
            return self._name

        @name.setter
        def name(self, new_name):
            del self._modeler._by_name[self._name]
            self._name = new_name
            self._modeler._by_name[new_name] = self

    class Modeler:
        def __init__(self, solids_by_name):
            self._by_name = {}
            for name, box in solids_by_name.items():
                self._by_name[name] = TestPlaceLidInside.Solid(name, box, self)
            self.united = []
            self.moved = []

        @property
        def modeler(self):
            return self

        def __getitem__(self, name):
            return self._by_name[name]

        def unite(self, names):
            self.united.append(list(names))
            boxes = [self._by_name[n].bounding_box for n in names]
            merged = [
                min(b[0] for b in boxes), min(b[1] for b in boxes), min(b[2] for b in boxes),
                max(b[3] for b in boxes), max(b[4] for b in boxes), max(b[5] for b in boxes),
            ]
            survivor = self._by_name[names[0]]
            survivor.bounding_box = merged
            for n in names[1:]:
                del self._by_name[n]

        def move(self, names, vector):
            self.moved.append((list(names), list(vector)))
            dx, dy, dz = vector
            for n in names:
                obj = self._by_name[n]
                x0, y0, z0, x1, y1, z1 = obj.bounding_box
                obj.bounding_box = [x0 + dx, y0 + dy, z0 + dz, x1 + dx, y1 + dy, z1 + dz]

    @staticmethod
    def _frame_and_stack():
        from microwave_package.design import copper_geometry as cg
        frame = cg.Frame(x_offset=19.0, z_offset=6.0)
        stack = cg.Stack(pcb_bottom=1.0, pcb_top=1.381, chip_bottom=1.0, chip_top=1.381,
                         copper_thickness=0.035, chip_metal_thickness=0.0002)
        return frame, stack

    def test_centres_on_the_frame_and_rests_on_the_board_copper(self):
        modeler = self.Modeler({
            "box": [0.0, 0.0, 0.0, 38.0, 1.951, 12.0],
            # Imported off in lid_inside's own part coordinates -- nowhere
            # near the board, e.g. centred on its own origin.
            "NewObject_1": [-13.0, 0.0, -5.0, 13.0, 0.5, 5.0],
        })
        frame, stack = self._frame_and_stack()
        name = model_builder.place_lid_inside(
            modeler, frame, stack, ["NewObject_1"], log=lambda _m: None,
        )
        self.assertEqual(name, "lid_inside")
        x0, y0, z0, x1, y1, z1 = modeler["lid_inside"].bounding_box
        self.assertAlmostEqual((x0 + x1) / 2.0, frame.x_offset)
        self.assertAlmostEqual((z0 + z1) / 2.0, frame.z_offset)
        self.assertAlmostEqual(y0, stack.pcb_copper_top)

    def test_multiple_imported_bodies_are_united_first(self):
        modeler = self.Modeler({
            "box": [0.0, 0.0, 0.0, 38.0, 1.951, 12.0],
            "NewObject_1": [-13.0, 0.0, -5.0, 0.0, 0.5, 5.0],
            "NewObject_2": [0.0, 0.0, -5.0, 13.0, 0.5, 5.0],
        })
        frame, stack = self._frame_and_stack()
        model_builder.place_lid_inside(
            modeler, frame, stack, ["NewObject_1", "NewObject_2"], log=lambda _m: None,
        )
        self.assertEqual(modeler.united, [["NewObject_1", "NewObject_2"]])
        x0, _y0, _z0, x1, _y1, _z1 = modeler["lid_inside"].bounding_box
        self.assertAlmostEqual(x1 - x0, 26.0)

    def test_no_imported_body_is_reported_not_raised(self):
        modeler = self.Modeler({"box": [0.0, 0.0, 0.0, 38.0, 1.951, 12.0]})
        frame, stack = self._frame_and_stack()
        messages = []
        result = model_builder.place_lid_inside(modeler, frame, stack, [], log=messages.append)
        self.assertIsNone(result)
        self.assertTrue(any("WARNING" in m for m in messages))

    def test_a_lid_taller_than_the_box_is_not_a_warning(self):
        """Confirmed as the intended design: lid_inside is free to poke
        above box's own rim -- whatever closes over it from there is
        outside this model, and this code must not second-guess it."""
        modeler = self.Modeler({
            "box": [0.0, 0.0, 0.0, 38.0, 1.951, 12.0],
            # 2 mm thick -- well past the 0.535 mm gap above the copper.
            "NewObject_1": [-13.0, 0.0, -5.0, 13.0, 2.0, 5.0],
        })
        frame, stack = self._frame_and_stack()
        messages = []
        model_builder.place_lid_inside(modeler, frame, stack, ["NewObject_1"], log=messages.append)
        self.assertFalse(any("WARNING" in m for m in messages))
        x0, y0, z0, x1, y1, z1 = modeler["lid_inside"].bounding_box
        self.assertAlmostEqual(y0, stack.pcb_copper_top)
        self.assertAlmostEqual(y1 - y0, 2.0)


class TestCarveVacuum(unittest.TestCase):
    """Which bodies get subtracted from vacuum.

    This exists because the FIRST version silently missed bodies:
    several material rules legitimately match more than one solid --
    pcb_copper matches both the top and bottom layer, connector matches
    two conductor bodies per port -- and objects_dict (used elsewhere to
    look something up by a single semantic key like "vacuum" or "chip")
    keeps only the first body per rule key. Building the vacuum-carving
    tool list from objects_dict therefore carved only ONE of two connectors
    and left the second one overlapping vacuum, invisibly. carve_vacuum
    now scans the modeler's raw solid list instead, which has no such
    collapsing.
    """

    def test_every_connector_is_carved_even_though_they_share_a_rule_key(self):
        hfss = FakeHfss([
            "vacuum", "box",
            "connector_1_outer", "connector_1_center", "connector_1_dielectric",
            "connector_2_outer", "connector_2_center", "connector_2_dielectric",
        ])
        # Simulate what assign_materials_and_boundaries actually leaves
        # behind: only the FIRST body per rule key survives objects_dict's
        # setdefault, exactly like the bug that shipped.
        objects_dict = {
            "vacuum": hfss.solid_objects[0],
            "box": hfss.solid_objects[1],
            "connector": hfss.solid_objects[2],
            "connector_dielectric": hfss.solid_objects[4],
        }
        model_builder.carve_vacuum(hfss, objects_dict, log=lambda _m: None)
        _target, tools, _keep = hfss.subtractions[0]
        for body in ("connector_1_outer", "connector_1_center", "connector_1_dielectric",
                     "connector_2_outer", "connector_2_center", "connector_2_dielectric"):
            self.assertIn(body, tools, f"{body} must not be skipped")

    def test_both_copper_layers_are_carved_even_though_they_share_a_rule_key(self):
        hfss = FakeHfss(["vacuum", "box", "pcb_copper_top", "pcb_copper_bottom"])
        objects_dict = {
            "vacuum": hfss.solid_objects[0],
            "box": hfss.solid_objects[1],
            "pcb_copper": hfss.solid_objects[2],
        }
        model_builder.carve_vacuum(hfss, objects_dict, log=lambda _m: None)
        _target, tools, _keep = hfss.subtractions[0]
        self.assertIn("pcb_copper_top", tools)
        self.assertIn("pcb_copper_bottom", tools)

    def test_substrates_are_carved(self):
        """The substrates were never carved at all in the first version
        -- carve_vacuum only ever looked for 'copper'/'via'/'wirebond'
        in the key, which a plain substrate never matches."""
        hfss = FakeHfss(["vacuum", "box", "pcb_substrate", "chip_substrate"])
        objects_dict = {
            "vacuum": hfss.solid_objects[0],
            "box": hfss.solid_objects[1],
            "pcb": hfss.solid_objects[2],
            "chip": hfss.solid_objects[3],
        }
        model_builder.carve_vacuum(hfss, objects_dict, log=lambda _m: None)
        _target, tools, _keep = hfss.subtractions[0]
        self.assertIn("pcb_substrate", tools)
        self.assertIn("chip_substrate", tools)

    def test_only_vacuum_itself_is_never_a_carving_tool(self):
        """vacuum is now a plain block built from box's own measured
        envelope (build_vacuum), with no body pre-excluded from it -- so
        box and lid_inside are legitimate carving tools too, unlike the
        old SolidWorks-hand-fitted vacuum this replaces."""
        hfss = FakeHfss(["vacuum", "box", "lid_inside", "pcb_substrate"])
        objects_dict = {"vacuum": hfss.solid_objects[0]}
        model_builder.carve_vacuum(hfss, objects_dict, log=lambda _m: None)
        _target, tools, _keep = hfss.subtractions[0]
        self.assertNotIn("vacuum", tools)
        self.assertIn("box", tools)
        self.assertIn("lid_inside", tools)
        self.assertIn("pcb_substrate", tools)

    def test_via_posts_are_not_carved_but_everything_else_is(self):
        """The posts stand wholly inside the board, which is carved
        already. Carving them too has AEDT cut every via out of the vacuum
        and fill it straight back in -- minutes of one call on a dense
        board, with the web app frozen for all of it."""
        hfss = FakeHfss(["vacuum", "box", "pcb_substrate", "pcb_copper_top",
                         "pcb_vias", "pcb_vias_2", "chip_substrate", "chip_vias"])
        model_builder.carve_vacuum(hfss, {"vacuum": hfss.solid_objects[0]},
                                   log=lambda _m: None)
        _target, tools, _keep = hfss.subtractions[0]
        self.assertEqual(sorted(tools),
                         ["box", "chip_substrate", "pcb_copper_top", "pcb_substrate"])

    def test_the_carved_bodies_survive(self):
        """keep_originals: these are the things being simulated, not
        scrap left over from cutting the vacuum."""
        hfss = FakeHfss(["vacuum", "pcb_substrate"])
        model_builder.carve_vacuum(hfss, {"vacuum": hfss.solid_objects[0]},
                                   log=lambda _m: None)
        _target, _tools, keep = hfss.subtractions[0]
        self.assertTrue(keep)

    def test_no_vacuum_body_is_a_no_op(self):
        hfss = FakeHfss(["box", "pcb_substrate"])
        model_builder.carve_vacuum(hfss, {}, log=lambda _m: None)
        self.assertEqual(hfss.subtractions, [])

    def test_nothing_to_carve_is_a_no_op(self):
        """The only case with nothing to carve is vacuum on its own."""
        hfss = FakeHfss(["vacuum"])
        model_builder.carve_vacuum(hfss, {"vacuum": hfss.solid_objects[0]},
                                   log=lambda _m: None)
        self.assertEqual(hfss.subtractions, [])

    def test_a_failure_is_loud(self):
        class Failing(FakeHfss):
            def subtract(self, *a, **k):
                raise RuntimeError("nope")

        hfss = Failing(["vacuum", "pcb_substrate"])
        messages = []
        model_builder.carve_vacuum(hfss, {"vacuum": hfss.solid_objects[0]}, log=messages.append)
        self.assertTrue(any("WARNING" in m for m in messages))
        self.assertTrue(any("overlapping" in m for m in messages))


if __name__ == "__main__":
    unittest.main()
