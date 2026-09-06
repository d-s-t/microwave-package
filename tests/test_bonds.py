"""Tests for where the wire bonds go.

The layout is computed once and shared: Altium places pads from it, HFSS
builds wires from it. These pin the arrangement that was asked for --
two signal wires a side, ground all the way round the die, and ground
bonds kept off the CPW gap.
"""
import os
import tempfile
import unittest

from eigen_mode_analyze import altium_generation as ag, bonds, equations as eq, paths


def defaults():
    return eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))


class TestLayout(unittest.TestCase):
    def setUp(self):
        self.v = defaults()
        self.gap = ag.build_params(self.v)["pcb_cpw_gap"]
        self.bonds = bonds.layout(self.v, self.gap)
        self.gap_edge = self.v["cpw_trace_width"] / 2 + self.gap

    def test_two_signal_wires_at_each_end(self):
        """One wire is an inductance in series with the line; two in
        parallel roughly halve it."""
        for sign in (-1, 1):
            here = [b for b in self.bonds
                    if b.kind == bonds.SIGNAL and (b.pcb_x > 0) == (sign > 0)]
            self.assertEqual(len(here), 2, f"expected 2 signal wires at the {sign} end")

    def test_signal_wires_sit_on_the_centre_conductor(self):
        trace = self.v["cpw_trace_width"]
        for b in self.bonds:
            if b.kind == bonds.SIGNAL:
                self.assertLessEqual(abs(b.pcb_y), trace / 2)

    def test_ground_bonds_go_all_the_way_round(self):
        """The die sits in a pocket surrounded by ground; bonding only
        the feedline ends leaves it referenced through two sides."""
        ground = [b for b in self.bonds if b.kind == bonds.GROUND]
        half_l = self.v["chip_length"] / 2
        half_s = self.v["chip_side"] / 2
        self.assertTrue(any(b.pcb_x < -half_l for b in ground), "no bonds off the -x end")
        self.assertTrue(any(b.pcb_x > half_l for b in ground), "no bonds off the +x end")
        self.assertTrue(any(b.pcb_y < -half_s for b in ground), "no bonds off the -y side")
        self.assertTrue(any(b.pcb_y > half_s for b in ground), "no bonds off the +y side")

    def test_ground_bonds_keep_clear_of_the_gap(self):
        """Landing them at the gap edge puts metal where the field
        between line and ground is strongest."""
        clearance = self.v["via_gnd_clearance"]
        for b in self.bonds:
            if b.kind != bonds.GROUND or abs(b.pcb_x) <= self.v["chip_length"] / 2:
                continue
            self.assertGreaterEqual(abs(b.pcb_y), self.gap_edge + clearance - 1e-9)

    def test_every_bond_spans_from_board_to_die(self):
        half_l = self.v["chip_length"] / 2
        half_s = self.v["chip_side"] / 2
        for b in self.bonds:
            on_board = abs(b.pcb_x) > half_l or abs(b.pcb_y) > half_s
            on_die = abs(b.chip_x) <= half_l + 1e-9 and abs(b.chip_y) <= half_s + 1e-9
            self.assertTrue(on_board, f"{b} does not start off the die")
            self.assertTrue(on_die, f"{b} does not land on the die")

    def test_the_count_follows_from_geometry_not_a_parameter(self):
        """bond_count was removed: a bigger die simply takes more."""
        self.assertNotIn("bond_count", self.v)
        bigger = dict(self.v, chip_side=self.v["chip_side"] * 2)
        self.assertGreater(len(bonds.layout(bigger, self.gap)), len(self.bonds))

    def test_a_finer_pitch_fits_more_bonds(self):
        finer = dict(self.v, bond_pad_pitch=self.v["bond_pad_pitch"] / 2)
        self.assertGreater(len(bonds.layout(finer, self.gap)), len(self.bonds))

    def test_a_wider_gap_pushes_the_ground_bonds_out(self):
        wide = bonds.layout(self.v, self.gap * 2)
        nearest_now = min(abs(b.pcb_y) for b in self.bonds
                          if b.kind == bonds.GROUND and abs(b.pcb_x) > 3)
        nearest_wide = min(abs(b.pcb_y) for b in wide
                           if b.kind == bonds.GROUND and abs(b.pcb_x) > 3)
        self.assertGreater(nearest_wide, nearest_now)


class TestFileRoundTrip(unittest.TestCase):
    def test_written_and_read_back_unchanged(self):
        """Altium and HFSS both read this file; a lossy round trip would
        put the pads and the wires in different places."""
        v = defaults()
        gap = ag.build_params(v)["pcb_cpw_gap"]
        original = bonds.layout(v, gap)
        path = os.path.join(tempfile.mkdtemp(), "bonds.txt")
        bonds.write_file(path, original)
        self.assertEqual(bonds.read_file(path), original)

    def test_comments_and_blanks_are_ignored(self):
        path = os.path.join(tempfile.mkdtemp(), "bonds.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# a comment\n\nsignal|1.0|2.0|3.0|4.0\n")
        got = bonds.read_file(path)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].kind, "signal")
        self.assertEqual(got[0].chip_y, 4.0)


if __name__ == "__main__":
    unittest.main()
