"""Tests for the layout feasibility check.

These run before Altium is launched. Their job is to turn a geometry
that cannot be built into a sentence naming the parameter to change,
rather than a board that looks fine and is quietly wrong.

Run: python -m unittest discover -s tests
"""
import tempfile
import unittest

from microwave_package import paths, pipeline
from microwave_package.design import constraints, equations as eq
from microwave_package.design.sweep import SweepParameter
from microwave_package.workflow.run_manager import Run


def defaults():
    return eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))


class TestCheckLayout(unittest.TestCase):
    def test_the_shipped_defaults_are_buildable(self):
        """If this ever fails, default.txt describes a board that cannot
        be made -- which would block every run."""
        self.assertIsNone(constraints.check_layout(defaults()))

    def test_via_fence_that_overruns_the_board_is_caught(self):
        values = dict(defaults(), pcb_side=1.0)
        reason = constraints.check_layout(values)
        self.assertIsNotNone(reason)
        self.assertIn("via fence", reason)
        self.assertIn("pcb_side", reason)

    def test_via_fence_with_no_room_for_the_edge_margin_is_caught(self):
        """The generator keeps every via via_min_spacing from the board
        edge, and the fence is the first row of the lattice the rest are
        laid out from -- so a board the fence fits on by less than that
        gets no vias at all. Say so up front rather than build it."""
        from microwave_package.design import altium_generation as ag

        v = defaults()
        fence_edge = (v["cpw_trace_width"] / 2 + ag.build_params(v)["pcb_cpw_gap"]
                      + v["via_gnd_clearance"] + v["via_diameter"])
        # Wide enough for the fence itself, and half the margin short of it.
        values = dict(v, pcb_side=2 * (fence_edge + v["via_min_spacing"] / 2))
        reason = constraints.check_layout(values)
        self.assertIsNotNone(reason)
        self.assertIn("via fence", reason)
        self.assertIn("via_min_spacing", reason)

    def test_via_min_spacing_wider_than_the_grid_step_is_caught(self):
        values = dict(defaults(), via_min_spacing=1.0)
        reason = constraints.check_layout(values)
        self.assertIsNotNone(reason)
        self.assertIn("via_min_spacing", reason)
        self.assertIn("via_max_spacing", reason)

    def test_a_via_step_of_nothing_is_caught(self):
        """It would leave no vias at all, and the generator reports that
        as a board like any other. Not caught by the spacing sum when the
        via has no size either."""
        values = dict(defaults(), via_max_spacing=0.0, via_diameter=0.0, via_min_spacing=0.0)
        reason = constraints.check_layout(values)
        self.assertIsNotNone(reason)
        self.assertIn("via_max_spacing", reason)

    def test_unreachable_impedance_is_reported_as_such(self):
        """A trace too wide for the substrate is microstrip-dominated and
        has no 50 ohm gap. Caught here rather than raising mid-sweep."""
        values = dict(defaults(), cpw_trace_width=0.5)
        reason = constraints.check_layout(values)
        self.assertIn("50 ohm", reason)

    def test_bond_fingers_colliding_with_the_port_are_caught(self):
        values = dict(defaults(), bonding_clearence=9.0)
        reason = constraints.check_layout(values)
        self.assertIn("bond fingers", reason)

    def test_a_bond_pad_too_small_to_clear_the_chip_inset_is_caught(self):
        """The die's own copper is pulled back CHIP_COPPER_INSET from
        its edge (GenerateFromParams.pas's ChipCopperInset). A pad
        smaller than that inset lands the wire on bare substrate rather
        than on copper."""
        values = dict(defaults(), bond_pad_length=constraints.CHIP_COPPER_INSET / 2)
        reason = constraints.check_layout(values)
        self.assertIsNotNone(reason)
        self.assertIn("bond_pad_length", reason)
        self.assertIn("substrate", reason)

    def test_a_bond_pad_exactly_at_the_inset_is_still_caught(self):
        """The pad must be LARGER than the inset, not merely equal to
        it -- equal still lands the wire right on the boundary."""
        values = dict(defaults(), bond_pad_length=constraints.CHIP_COPPER_INSET)
        self.assertIsNotNone(constraints.check_layout(values))

    def test_nonpositive_trace_width_is_rejected(self):
        self.assertIsNotNone(constraints.check_layout(dict(defaults(), cpw_trace_width=0.0)))


class TestTheDiesOpening(unittest.TestCase):
    """The opening is cut larger than the die, so everything that used
    to be checked against the die edge has to be checked against the
    opening instead. A feature can be comfortably clear of the die and
    still hanging over the hole it sits in."""

    def test_a_negative_clearance_is_rejected(self):
        """It would cut the opening smaller than the die."""
        reason = constraints.check_layout(dict(defaults(), cavity_clearance=-0.05))
        self.assertIsNotNone(reason)
        self.assertIn("cavity_clearance", reason)

    def test_zero_clearance_is_allowed(self):
        """A tight fit is a choice, not a mistake -- the check is only
        that the opening is not made smaller than the part."""
        self.assertIsNone(constraints.check_layout(dict(defaults(), cavity_clearance=0.0)))

    def test_bond_fingers_hanging_over_the_opening_are_caught(self):
        """The finger sits between the PCB's opening and the lid's
        limit, so what has to be checked is whether that strip of board
        was ever as wide as the pad. Cutting the opening further out
        eats into it from the inside."""
        values = dict(defaults(), bonding_clearence=0.35, cavity_clearance=0.15)
        reason = constraints.check_layout(values)
        self.assertIsNotNone(reason)
        self.assertIn("bond fingers", reason)
        self.assertIn("cavity_clearance", reason)

    def test_the_same_clearance_is_fine_once_the_opening_is_tight(self):
        """The pairing is what fails, not either number on its own."""
        values = dict(defaults(), bonding_clearence=0.35, cavity_clearance=0.0)
        reason = constraints.check_layout(values)
        self.assertNotIn("bond fingers", reason or "")

    def test_the_clearance_the_finger_needs_is_named_in_the_message(self):
        """The message says the number outright rather than leaving it
        to be worked back out of the geometry -- this check is the one
        that turned a working run into a failed one, so the way out of
        it should not need deriving."""
        values = dict(defaults(), bonding_clearence=0.35, cavity_clearance=0.15)
        reason = constraints.check_layout(values)
        # bond_pad_length + cavity_clearance = 0.3 + 0.15
        self.assertIn("0.45", reason)

    def test_an_opening_wider_than_the_board_is_caught(self):
        """The coplanar planes are each one region with the opening
        stepped out of their inner edge. An opening reaching the copper
        edge would cut a plane in two, which one region cannot be."""
        values = dict(defaults(), chip_side=9.9, cavity_clearance=0.2)
        reason = constraints.check_layout(values)
        self.assertIsNotNone(reason)
        self.assertIn("opening", reason)
        self.assertIn("across the feedline", reason)

    def test_every_message_names_a_parameter_to_change(self):
        """A reason the reader cannot act on is barely better than none."""
        names = set(defaults())
        for override in (
            {"pcb_side": 1.0},
            {"via_min_spacing": 1.0},
            {"bond_pad_length": constraints.CHIP_COPPER_INSET / 2},
            {"bonding_clearence": 9.0},
        ):
            reason = constraints.check_layout(dict(defaults(), **override))
            self.assertTrue(
                any(name in reason for name in names),
                f"{reason!r} names no parameter the reader could change",
            )


def via_grid(diameter, max_spacing, min_spacing):
    return dict(defaults(), via_diameter=diameter, via_max_spacing=max_spacing,
                via_min_spacing=min_spacing)


class TestViaGrid(unittest.TestCase):
    """The fill and the fence place a via every via_max_spacing and refuse
    any that would sit closer than via_min_spacing to one already placed.
    If a via plus that gap does not fit in one step, the generator skips
    every other via without complaint, and the board has twice the
    spacing its parameters say."""

    def test_the_sweep_that_found_it_is_refused(self):
        """0.4 mm vias 0.2 mm apart need 0.6 mm, and were asked for every
        0.5 mm (runs/run_20260927_125850)."""
        reason = constraints.check_via_grid(via_grid(0.4, 0.5, 0.2))
        self.assertIsNotNone(reason)
        self.assertIn("0.6 mm", reason)

    def test_the_message_names_all_three_parameters(self):
        """Any of the three can fix it, so the reader is told about all of them."""
        reason = constraints.check_via_grid(via_grid(0.4, 0.5, 0.2))
        for name in ("via_diameter", "via_max_spacing", "via_min_spacing"):
            self.assertIn(name, reason)

    def test_a_step_that_exactly_fits_is_allowed(self):
        """The generator places a via exactly via_min_spacing from its
        neighbour (GapTolerance), so this has to allow it too. Several of
        these are a hair over the step in floating point -- 0.4 + 0.2
        is more than 0.6 -- which a plain comparison refuses."""
        for diameter, max_spacing in ((0.1, 0.3), (0.2, 0.4), (0.3, 0.5), (0.4, 0.6),
                                      (0.5, 0.7), (0.6, 0.8), (0.7, 0.9), (0.8, 1.0)):
            with self.subTest(via_diameter=diameter, via_max_spacing=max_spacing):
                self.assertIsNone(
                    constraints.check_via_grid(via_grid(diameter, max_spacing, 0.2)))

    def test_a_shortfall_inside_the_tolerance_is_allowed(self):
        tolerance = constraints.VIA_GAP_TOLERANCE
        self.assertIsNone(constraints.check_via_grid(via_grid(0.3, 0.5 - tolerance / 2, 0.2)))

    def test_a_shortfall_past_the_tolerance_is_refused(self):
        tolerance = constraints.VIA_GAP_TOLERANCE
        self.assertIsNotNone(constraints.check_via_grid(via_grid(0.3, 0.5 - tolerance * 2, 0.2)))

    def test_a_generous_step_is_allowed(self):
        self.assertIsNone(constraints.check_via_grid(via_grid(0.3, 1.0, 0.2)))

    def test_check_layout_refuses_it_too(self):
        """The Altium stage's own check, which still catches it for a run
        created before the check at run creation existed."""
        values = via_grid(0.4, 0.5, 0.2)
        self.assertEqual(constraints.check_layout(values), constraints.check_via_grid(values))

    def test_check_layout_allows_an_exact_fit(self):
        """It used to compare without any tolerance, and refused this."""
        self.assertIsNone(constraints.check_layout(via_grid(0.4, 0.6, 0.2)))


class TestViaGridAtRunCreation(unittest.TestCase):
    """A combination whose via grid would come out sparse is skipped when
    the run is created, like a distortion that does not fit. Before, it
    was only refused once the Altium stage reached it, by which time
    SolidWorks had already built its solid."""

    def setUp(self):
        # A temporary runs root: nothing here should touch the
        # developer's own runs/. Every module finds it through `paths`.
        runs_root = tempfile.TemporaryDirectory(prefix="runs_")
        self.addCleanup(runs_root.cleanup)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = runs_root.name
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)

    def skip_reasons(self, **settings):
        run = Run.create(
            [SweepParameter(name="via_max_spacing", values=[0.5, 0.6])],
            dict(pipeline.DEFAULT_ANALYSIS_SETTINGS, **settings),
            parameter_defaults={"via_diameter": 0.4, "via_min_spacing": 0.2},
        )
        return {
            combo["params"]["via_max_spacing"]: combo["skip_reason"]
            for combo in run.state["combos"].values()
        }

    def test_the_sparse_combination_is_skipped(self):
        reasons = self.skip_reasons(use_altium=True)
        self.assertIsNotNone(reasons[0.5])
        self.assertIn("via_max_spacing", reasons[0.5])

    def test_the_combination_that_fits_is_built(self):
        self.assertIsNone(self.skip_reasons(use_altium=True)[0.6])

    def test_a_run_without_altium_skips_nothing(self):
        """No layout is drawn, so there are no vias to be sparse."""
        reasons = self.skip_reasons(use_altium=False)
        self.assertEqual(reasons, {0.5: None, 0.6: None})

    def test_a_distortion_that_does_not_fit_is_still_the_reason_given(self):
        """Both are wrong here. The distortion check comes first, as it
        always has, so its message is the one that shows."""
        run = Run.create(
            [SweepParameter(name="via_max_spacing", values=[0.5])],
            dict(pipeline.DEFAULT_ANALYSIS_SETTINGS, use_altium=True),
            parameter_defaults={"via_diameter": 0.4, "via_min_spacing": 0.2,
                                "no_distortion": 0, "distortion_radius": 100.0},
        )
        (combo,) = run.state["combos"].values()
        self.assertIn("distortion", combo["skip_reason"].lower())
        self.assertNotIn("via_max_spacing", combo["skip_reason"])
