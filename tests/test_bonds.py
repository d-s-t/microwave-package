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


class TestPadsStayOnTheDiesCopper(unittest.TestCase):
    """The die's copper has to come out a clean rectangle.

    Rows used to be grown from one end and stopped at the die edge,
    which put the last pad of each row over the edge of the die's
    METAL -- the copper stops CHIP_COPPER_INSET short of the substrate,
    so "inside the die" is not the same test. The pads nearest each
    corner overhung by 0.07-0.20 mm and one reached past the die
    outline itself. United with the ground plane that is four ragged,
    notched corners; and a wire on an overhanging pad lands on bare
    silicon rather than metal.
    """

    def setUp(self):
        from eigen_mode_analyze import constraints

        self.v = defaults()
        self.gap = ag.build_params(self.v)["pcb_cpw_gap"]
        self.bonds = bonds.layout(self.v, self.gap)
        self.edge = self.v["chip_side"] / 2.0 - constraints.CHIP_COPPER_INSET

    def _pads(self):
        """Each die-side pad as (x0, x1, y0, y1, kind), the way Altium
        draws it: bond_pad_length along x by bond_pad_width along y,
        except signal pads, which are capped to half the trace."""
        half_x = self.v["bond_pad_length"] / 2.0
        for b in self.bonds:
            half_y = (self.v["cpw_trace_width"] / 2.0 if b.kind == bonds.SIGNAL
                      else self.v["bond_pad_width"]) / 2.0
            yield (b.chip_x - half_x, b.chip_x + half_x,
                   b.chip_y - half_y, b.chip_y + half_y, b.kind)

    def test_no_pad_hangs_over_the_edge_of_the_dies_copper(self):
        for x0, x1, y0, y1, kind in self._pads():
            for name, value in (("x", max(abs(x0), abs(x1))),
                                ("y", max(abs(y0), abs(y1)))):
                self.assertLessEqual(
                    value, self.edge + 1e-9,
                    f"a {kind} pad reaches {name}={value:.4f}, past the die's "
                    f"copper at {self.edge:.4f}",
                )

    def test_no_pad_reaches_past_the_die_itself(self):
        die = self.v["chip_side"] / 2.0
        for x0, x1, y0, y1, _kind in self._pads():
            self.assertLess(max(abs(x0), abs(x1), abs(y0), abs(y1)), die)

    def test_pads_from_different_edges_do_not_collide_at_the_corners(self):
        """Bounding each row only by the copper edge is not enough: the
        rows on the two axes then both reach the corner and their pads
        overlap. The only pads that may touch are the two signal wires
        at each end, which meet on the centre line by design."""
        pads = list(self._pads())
        for i in range(len(pads)):
            for j in range(i + 1, len(pads)):
                a, b = pads[i], pads[j]
                overlap = (a[0] < b[1] - 1e-9 and b[0] < a[1] - 1e-9
                           and a[2] < b[3] - 1e-9 and b[2] < a[3] - 1e-9)
                if overlap:
                    self.assertEqual(
                        (a[4], b[4]), (bonds.SIGNAL, bonds.SIGNAL),
                        f"ground pads overlap: {a} and {b}",
                    )

    def test_the_rows_along_the_die_sides_are_centred(self):
        half = self.v["chip_side"] / 2.0
        side = [b for b in self.bonds if abs(b.pcb_y) > half]
        self.assertTrue(side)
        xs = sorted({round(b.chip_x, 9) for b in side})
        self.assertAlmostEqual(xs[0], -xs[-1], places=9,
                               msg="the row is off centre, so one end runs long")

    def test_the_die_is_still_bonded_all_the_way_round(self):
        """Bounding the rows must not bound them out of existence."""
        half = self.v["chip_side"] / 2.0
        for name, picked in (
            ("-x", [b for b in self.bonds if b.pcb_x < -half]),
            ("+x", [b for b in self.bonds if b.pcb_x > half]),
            ("-y", [b for b in self.bonds if b.pcb_y < -half]),
            ("+y", [b for b in self.bonds if b.pcb_y > half]),
        ):
            self.assertGreater(len(picked), 4, f"the {name} side lost its bonds")


if __name__ == "__main__":
    unittest.main()
