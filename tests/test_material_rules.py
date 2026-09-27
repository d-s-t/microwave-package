"""Tests for how an imported solid's name picks its material.

This is worth testing precisely because getting it wrong is invisible.
The old if/elif chain would have handed `pcb_copper` the Rogers
permittivity, and nothing would have complained -- the model would just
have solved to the wrong frequencies.

HFSS is not involved: a tiny stand-in records what would have been
assigned, so the rules can be checked without AEDT.

Run: python -m unittest discover -s tests
"""
import unittest

from microwave_package.hfss import model_builder


class FakeObject:
    def __init__(self, name):
        self.name = name
        self.material_name = None


class FakeHfss:
    """Records assignments instead of performing them."""

    def __init__(self, names):
        self._objects = [FakeObject(n) for n in names]
        self.perfect_e = []
        self.subtractions = []

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
        hfss, _ = assign(["vacuum", "PCB", "chip", "box", "lid", "chip_lid"])
        got = {o.name: o.material_name for o in hfss.modeler.solid_objects}
        self.assertEqual(got["vacuum"], "vacuum")
        self.assertEqual(got["PCB"], "Rogers TMM 10i (tm)")
        self.assertEqual(got["chip"], "silicon")
        self.assertEqual(got["box"], "aluminum")
        self.assertEqual(got["lid"], "aluminum")
        self.assertEqual(got["chip_lid"], "aluminum")

    def test_chip_lid_is_metal_not_silicon(self):
        """`chip_lid` contains `chip`. The specific rule has to win, or
        the lid becomes silicon."""
        hfss, _ = assign(["chip_lid"])
        self.assertEqual(hfss.modeler.solid_objects[0].material_name, "aluminum")

    def test_perfect_e_on_the_enclosure(self):
        hfss, _ = assign(["box", "lid", "chip_lid", "PCB"])
        self.assertEqual(sorted(hfss.perfect_e), ["box", "chip_lid", "lid"])


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
            "vacuum", "box", "lid", "chip_lid",
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

    def test_the_chip_lid_survives(self):
        """It is enclosure, not die, and shares the prefix."""
        self.assertEqual(self._drop(["chip_lid", "lid", "box"]), [])

    def test_the_enclosure_and_vacuum_survive(self):
        self.assertEqual(self._drop(["box", "lid", "vacuum"]), [])

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
                               ("chip_lid", "chip"), ("dielectric", "connector")):
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

    def test_the_enclosure_is_never_a_carving_tool(self):
        """box/lid/chip_lid come straight from the STEP, where the
        SolidWorks-built vacuum already accounts for them -- carving
        them again would be wrong, not merely redundant."""
        hfss = FakeHfss(["vacuum", "box", "lid", "chip_lid", "pcb_substrate"])
        objects_dict = {"vacuum": hfss.solid_objects[0]}
        model_builder.carve_vacuum(hfss, objects_dict, log=lambda _m: None)
        _target, tools, _keep = hfss.subtractions[0]
        self.assertNotIn("box", tools)
        self.assertNotIn("lid", tools)
        self.assertNotIn("chip_lid", tools)
        self.assertIn("pcb_substrate", tools)

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
        hfss = FakeHfss(["vacuum", "box", "lid", "chip_lid"])
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
