"""Tests for the layout feasibility check.

These run before Altium is launched. Their job is to turn a geometry
that cannot be built into a sentence naming the parameter to change,
rather than a board that looks fine and is quietly wrong.

Run: python -m unittest discover -s tests
"""
import unittest

from microwave_package import paths
from microwave_package.design import constraints, equations as eq


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

    def test_shield_ring_off_the_end_of_the_board_is_caught(self):
        values = dict(defaults(), shield_ring_inset=12.0)
        reason = constraints.check_layout(values)
        self.assertIn("shield ring", reason)

    def test_shield_ring_too_wide_is_caught_on_its_own_axis(self):
        values = dict(defaults(), chip_side=9.5)
        reason = constraints.check_layout(values)
        self.assertIsNotNone(reason)
        self.assertIn("across the feedline", reason)

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
        """bonding_clearence is measured from the DIE edge, so a
        clearance that is wide enough for the die can still put half the
        finger over the hole once the opening is cut past it."""
        values = dict(defaults(), bonding_clearence=0.2, cavity_clearance=0.15)
        reason = constraints.check_layout(values)
        self.assertIsNotNone(reason)
        self.assertIn("bond fingers", reason)
        self.assertIn("cavity_clearance", reason)

    def test_the_same_clearance_is_fine_once_the_opening_is_tight(self):
        """The pairing is what fails, not either number on its own."""
        values = dict(defaults(), bonding_clearence=0.2, cavity_clearance=0.0)
        reason = constraints.check_layout(values)
        self.assertNotIn("bond fingers", reason or "")

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
            {"shield_ring_inset": 12.0},
            {"bond_pad_length": constraints.CHIP_COPPER_INSET / 2},
            {"bonding_clearence": 9.0},
        ):
            reason = constraints.check_layout(dict(defaults(), **override))
            self.assertTrue(
                any(name in reason for name in names),
                f"{reason!r} names no parameter the reader could change",
            )
