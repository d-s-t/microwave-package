"""Tests for the die's opening in the board.

This is the one place that decides how big the opening is, how deep, and
whether it goes through -- and it is asked that by two different halves
of the pipeline, the Altium generator and the HFSS builder. So what is
pinned here is mostly that the answer is a single answer: the numbers
that reach the DelphiScript are the numbers the model is built from.

Run: python -m unittest discover -s tests
"""
import unittest

from microwave_package import paths
from microwave_package.design import (
    altium_generation as ag, cavity, equations as eq,
)


def defaults():
    return eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))


class TestWiggleRoom(unittest.TestCase):
    def test_the_opening_is_larger_than_the_die_on_every_side(self):
        """A die and a pocket of exactly equal size do not assemble."""
        values = dict(defaults(), chip_length=6.3, chip_side=4.0,
                      cavity_clearance=0.1)
        opening = cavity.plan(values)
        self.assertAlmostEqual(opening.half_length, 3.25)
        self.assertAlmostEqual(opening.half_side, 2.1)
        self.assertAlmostEqual(opening.length, 6.5)
        self.assertAlmostEqual(opening.side, 4.2)

    def test_the_clearance_is_per_side_not_overall(self):
        """0.1 mm of clearance means 0.1 mm at each edge, so the opening
        grows by 0.2 mm across. The die can sit 0.1 mm off centre in any
        direction, which is what a router's tolerance actually needs."""
        tight = cavity.plan(dict(defaults(), cavity_clearance=0.0))
        loose = cavity.plan(dict(defaults(), cavity_clearance=0.1))
        self.assertAlmostEqual(loose.length - tight.length, 0.2)

    def test_zero_clearance_is_allowed_and_means_exactly_the_die(self):
        opening = cavity.plan(dict(defaults(), chip_length=6.3, cavity_clearance=0.0))
        self.assertAlmostEqual(opening.length, 6.3)

    def test_a_file_without_the_parameter_still_gets_clearance(self):
        """An older run's dimensions.txt has no cavity_clearance. It must
        not silently fall back to an opening the die cannot go into."""
        values = {k: v for k, v in defaults().items() if k != "cavity_clearance"}
        self.assertGreater(cavity.plan(values).length, values["chip_length"])


class TestThroughOrBlind(unittest.TestCase):
    def test_a_die_as_thick_as_the_board_is_cut_through(self):
        """What the shipped defaults do: both are 381 um."""
        opening = cavity.plan(dict(defaults(), pcb_width=0.381, chip_width=0.381))
        self.assertTrue(opening.through)
        self.assertEqual(opening.kind, "through cutout")

    def test_a_die_thicker_than_the_board_is_cut_through(self):
        self.assertTrue(cavity.plan(dict(defaults(), pcb_width=0.381,
                                         chip_width=0.5)).through)

    def test_a_thin_die_gets_a_blind_pocket_of_its_own_depth(self):
        opening = cavity.plan(dict(defaults(), pcb_width=0.8, chip_width=0.3))
        self.assertFalse(opening.through)
        self.assertAlmostEqual(opening.depth, 0.3)
        self.assertEqual(opening.kind, "blind pocket")

    def test_a_through_cutout_is_cut_the_whole_thickness(self):
        opening = cavity.plan(dict(defaults(), pcb_width=0.381, chip_width=0.381))
        self.assertAlmostEqual(opening.depth, 0.381)

    def test_a_floor_too_thin_to_mill_is_cut_through_instead(self):
        """The decision is not chip_width >= pcb_width. A pocket that
        would leave a sliver of laminate under the die is not something a
        fabricator can hold, so it becomes a cutout."""
        values = dict(defaults(), pcb_width=0.381,
                      chip_width=0.381 - cavity.MIN_FLOOR / 2.0)
        self.assertTrue(cavity.plan(values).through)

    def test_a_floor_thick_enough_to_mill_stays_a_pocket(self):
        values = dict(defaults(), pcb_width=0.381,
                      chip_width=0.381 - cavity.MIN_FLOOR * 2.0)
        self.assertFalse(cavity.plan(values).through)

    def test_the_depth_carries_no_allowance_of_its_own(self):
        """Deepening the pocket does not help the die fit -- it drops the
        die below the board's top face and lengthens every bond wire. The
        wiggle room is in plane only, and the two top faces stay flush."""
        opening = cavity.plan(dict(defaults(), pcb_width=1.0, chip_width=0.3,
                                   cavity_clearance=0.15))
        self.assertAlmostEqual(opening.depth, 0.3)


class TestWhatCrossesToAltium(unittest.TestCase):
    """The DelphiScript draws what it is told. These pin that it is told
    everything, in the units the parameter file is written in."""

    def test_the_generator_gets_the_opening_in_its_parameters(self):
        params = ag.build_params(defaults())
        opening = cavity.plan(defaults())
        self.assertAlmostEqual(params["cavity_length"], opening.length)
        self.assertAlmostEqual(params["cavity_side"], opening.side)
        self.assertAlmostEqual(params["cavity_depth"], opening.depth)

    def test_through_or_blind_crosses_as_a_flag_not_a_comparison(self):
        """So the script never re-derives it from two thicknesses and
        reaches a different answer than the model did."""
        through = ag.build_params(dict(defaults(), pcb_width=0.381, chip_width=0.381))
        blind = ag.build_params(dict(defaults(), pcb_width=0.8, chip_width=0.3))
        self.assertEqual(through["cavity_through"], 1.0)
        self.assertEqual(blind["cavity_through"], 0.0)

    def test_full_sizes_cross_not_half_extents(self):
        """chip_length and chip_side already cross as full sizes, and the
        script halves them itself. A half-extent under a whole-size name
        is a factor of two nobody would see."""
        params = ag.build_params(defaults())
        self.assertAlmostEqual(
            params["cavity_length"],
            defaults()["chip_length"] + 2 * defaults()["cavity_clearance"],
        )


if __name__ == "__main__":
    unittest.main()
