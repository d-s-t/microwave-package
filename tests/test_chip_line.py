"""The die's line has a trace of its own (`chip_trace_width`).

The CPW gap is solved on each substrate for its own trace. The board's
trace is set by the board (0.4 mm on RT/duroid 5880, say), and on the 0.381
mm silicon die the same width is no CPW at all -- so the die cannot simply
follow it. These pin what depends on the two being different: the gap, the
bond pads, the layout check, and the die's gap ports.
"""
import unittest
from unittest import mock

from microwave_package import paths
from microwave_package.design import (
    altium_generation as ag, bonds, constraints, equations as eq, parameter_meta,
)

DUROID = "Rogers RT/duroid 5880 (tm)"


def defaults(**override):
    base = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    return {**base, **override}


class TestTheParameter(unittest.TestCase):
    def test_it_defaults_to_the_0_2_mm_the_die_always_had(self):
        self.assertEqual(defaults()["chip_trace_width"], 0.2)

    def test_the_form_knows_it(self):
        self.assertIn("chip_trace_width", parameter_meta.LABELS)
        listed = [n for group in parameter_meta.GROUPS for n in group["names"]]
        self.assertIn("chip_trace_width", listed)

    def test_values_without_it_use_the_boards_trace(self):
        self.assertEqual(constraints.chip_trace({"cpw_trace_width": 0.3}), 0.3)
        self.assertEqual(constraints.chip_trace({"cpw_trace_width": 0.3, "chip_trace_width": 0.2}), 0.2)


class TestBondsFollowEachLine(unittest.TestCase):
    def test_each_signal_pad_is_on_its_own_centre_conductor(self):
        v = defaults(cpw_trace_width=0.4, chip_trace_width=0.2)
        for b in bonds.layout(v, 0.094, 0.169):
            if b.kind == bonds.SIGNAL:
                self.assertAlmostEqual(abs(b.pcb_y), 0.1)      # board: trace / 4
                self.assertAlmostEqual(abs(b.chip_y), 0.05)    # die: its trace / 4

    def test_the_board_and_die_pads_coincide_when_the_traces_do(self):
        v = defaults()
        for b in bonds.layout(v, 0.247, 0.169):
            if b.kind == bonds.SIGNAL:
                self.assertEqual(b.pcb_y, b.chip_y)

    def test_ground_bonds_start_beyond_the_wider_of_the_two_lines(self):
        clearance = defaults()["via_gnd_clearance"]
        # a die line wider than the board's: its ground edge sets the start
        v = defaults(cpw_trace_width=0.2, chip_trace_width=0.3)
        start = constraints.ground_bond_start(v, board_gap=0.1, chip_gap=0.58)
        self.assertAlmostEqual(start, 0.3 / 2 + 0.58 + clearance)
        # ... and the board's when that is wider
        self.assertAlmostEqual(constraints.ground_bond_start(v, 0.3, 0.1), 0.1 + 0.3 + clearance)
        ends = [abs(b.pcb_y) for b in bonds.layout(v, 0.1, 0.58)
                if b.kind == bonds.GROUND and abs(b.pcb_x) > v["chip_length"] / 2]
        self.assertGreaterEqual(min(ends), start + v["bond_pad_width"] / 2 - 1e-9)

    def test_the_layout_without_a_die_gap_is_the_one_from_before(self):
        v = defaults()
        self.assertEqual(bonds.layout(v, 0.247), bonds.layout(v, 0.247, 0.169))


class TestTheDieHasRoomForItsLine(unittest.TestCase):
    def test_the_defaults_fit(self):
        self.assertIsNone(constraints.check_layout(defaults()))

    def test_a_die_trace_that_is_no_cpw_on_silicon_is_refused_with_its_name(self):
        reason = constraints.check_layout(defaults(chip_trace_width=0.45))
        self.assertIn("no 50 ohm CPW for a 0.45 mm trace on the 0.381 mm die", reason)
        self.assertIn("reduce chip_trace_width", reason)

    def test_a_die_too_narrow_for_its_ground_bonds_is_refused(self):
        """The line and its grounds' bond pads have to fit the die's copper:
        6.3 mm of die leaves 3.0 mm a side, 1.2 mm of die leaves 0.45."""
        reason = constraints.check_layout(defaults(chip_side=1.2))
        self.assertIn("ground bonds no room", reason or "")
        self.assertIn("widen chip_side", reason)

    def test_the_boards_trace_is_not_the_dies(self):
        """5880 with the candidate lid, board trace 0.4 mm: the layout is refused for the board's
        100 um gap rule only (relaxed here), not for the die."""
        v = defaults(cpw_trace_width=0.4, route_width=1.0, route_clearence=0.2, no_chip=0.0)
        with mock.patch.object(constraints, "MIN_CPW_GAP", 0.05):
            self.assertIsNone(constraints.check_layout(v, DUROID))

    def test_the_materials_die_is_the_one_checked(self):
        """Sapphire (eps 10) carries a wider 50 ohm CPW trace than silicon (eps 11.9):
        0.34 mm is no CPW on silicon and one on sapphire."""
        v = defaults(chip_trace_width=0.34)
        self.assertIn("no 50 ohm CPW", constraints.check_layout(v, None, "silicon"))
        self.assertIsNone(constraints.check_layout(v, None, "sapphire"))


class TestTheDieGapPortsUseTheDiesTrace(unittest.TestCase):
    def test_the_sheet_spans_the_die_gap_not_the_board_gap(self):
        from tests import test_ports as tp

        hfss = tp.bead_model()
        layout = tp.FakeLayout(chip_length=6.3, chip_side=6.3, bonding_clearence=0.6,
                               cpw_trace_width=0.4, chip_trace_width=0.2)
        from microwave_package.hfss.driven import ports

        ports.place_ports(hfss, layout, {**tp.DRIVEN, "reference_plane": ports.REFERENCE_PLANE_CHIP})
        sheet = hfss.modeler.rectangles[0]
        # the gap is from the die's trace edge (0.1) to the ground (0.26981), not from the board's (0.2)
        self.assertAlmostEqual(sheet["sizes"][0], layout.chip_ground_offset - 0.1)


if __name__ == "__main__":
    unittest.main()
