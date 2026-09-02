"""Tests for the layout feasibility check.

These run before Altium is launched. Their job is to turn a geometry
that cannot be built into a sentence naming the parameter to change,
rather than a board that looks fine and is quietly wrong.

Run: python -m unittest discover -s tests
"""
import unittest

from eigen_mode_analyze import constraints, equations as eq, paths


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

    def test_too_many_bonds_for_the_die_are_caught(self):
        values = dict(defaults(), bond_count=40)
        reason = constraints.check_layout(values)
        self.assertIn("bonds", reason)
        self.assertIn("die", reason)

    def test_nonpositive_trace_width_is_rejected(self):
        self.assertIsNotNone(constraints.check_layout(dict(defaults(), cpw_trace_width=0.0)))

    def test_every_message_names_a_parameter_to_change(self):
        """A reason the reader cannot act on is barely better than none."""
        names = set(defaults())
        for override in (
            {"pcb_side": 1.0},
            {"shield_ring_inset": 12.0},
            {"bond_count": 40},
            {"bonding_clearence": 9.0},
        ):
            reason = constraints.check_layout(dict(defaults(), **override))
            self.assertTrue(
                any(name in reason for name in names),
                f"{reason!r} names no parameter the reader could change",
            )
