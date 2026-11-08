"""The PCB and die the 3D view draws, against the numbers the HFSS model uses.

The view has no model of its own: `hfss.viewer_parts` asks the same
functions the model builder asks (`layout_stack`, `cavity.plan`,
`pcb_outline`), so these tests check that it passes their answers on
unchanged. The values are the sample run's (a through cutout), plus the
two cases the sample does not have: a blind pocket and a bare board.

Run: python -m pytest tests/test_viewer_parts.py
"""
import unittest

from microwave_package.design import cavity as cavity_plan
from microwave_package.design import pcb_outline
from microwave_package.hfss import model_builder, viewer_parts

# The sample run's dimensions (runs/run_20261030_023845_fine3), the values the
# model's own arithmetic turns into a 3.0 mm board floor and a 6.5 mm opening.
SAMPLE = {
    "pcb_length": 50.0, "pcb_side": 25.0, "pcb_width": 0.381, "pcb_corner_radius": 1.0,
    "wall_width": 1.0, "chip_length": 6.3, "chip_side": 6.3, "chip_width": 0.381,
    "cavity_clearance": 0.1, "cavity_corner_radius": 1.0, "pedestal_depth": 0.5,
    "screws_on": 1.0, "screw_engagement": 3.0, "copper_thickness": 0.035,
    "chip_metal_thickness": 0.0002, "no_chip": 0.0,
}


class TestBoardAndDie(unittest.TestCase):
    def test_the_board_sits_on_the_floor_and_the_pedestal(self):
        board = viewer_parts.board(SAMPLE)
        # floor_width is 2.5 (screw_engagement less the pedestal), the
        # pedestal adds 0.5, and the board is 0.381 thick on that.
        self.assertAlmostEqual(board["pcb"]["bottom"], 3.0)
        self.assertAlmostEqual(board["pcb"]["top"], 3.381)
        # The lid rests on the copper, 35 um above the board.
        self.assertAlmostEqual(board["lid_bottom"], 3.416)

    def test_the_numbers_are_the_models_own(self):
        board = viewer_parts.board(SAMPLE)
        stack = model_builder.layout_stack(SAMPLE)
        self.assertEqual(board["pcb"]["bottom"], stack.pcb_bottom)
        self.assertEqual(board["pcb"]["top"], stack.pcb_top)
        self.assertEqual(board["die"]["bottom"], stack.chip_bottom)
        self.assertEqual(board["die"]["top"], stack.chip_top)
        self.assertEqual(board["lid_bottom"], stack.pcb_copper_top)
        self.assertEqual(board["centre_fallback"], list(model_builder.board_centre_fallback(SAMPLE)))
        opening = cavity_plan.plan(SAMPLE)
        self.assertAlmostEqual(opening.half_length, 3.25)
        self.assertEqual(board["pcb"]["opening"][0], [opening.half_length, opening.half_side])

    def test_a_through_cutout_has_no_floor_and_the_die_is_flush_with_the_board(self):
        board = viewer_parts.board(SAMPLE)
        pcb, die = board["pcb"], board["die"]
        self.assertIsNone(pcb["floor_top"])
        self.assertEqual(len(pcb["opening"]), 4)
        self.assertAlmostEqual(max(u for u, _ in pcb["opening"]), 3.25)
        self.assertAlmostEqual(max(u for u, _ in die["outline"]), 3.15)
        self.assertEqual(die["top"], pcb["top"])

    def test_a_blind_pocket_leaves_a_floor_under_the_die(self):
        blind = dict(SAMPLE, pcb_width=0.5)      # 0.5 - 0.381 is over the 0.1 mm floor
        board = viewer_parts.board(blind)
        pcb, die = board["pcb"], board["die"]
        # The pocket is cut from the top face down to the die's own thickness,
        # so the floor it leaves is the die's bottom.
        self.assertAlmostEqual(pcb["floor_top"], pcb["top"] - 0.381)
        self.assertAlmostEqual(pcb["floor_top"], die["bottom"])

    def test_a_bare_board_has_no_opening_and_no_die(self):
        board = viewer_parts.board(dict(SAMPLE, no_chip=1.0))
        self.assertIsNone(board["pcb"]["opening"])
        self.assertIsNone(board["pcb"]["floor_top"])
        self.assertIsNone(board["die"])
        self.assertEqual(len(board["pcb"]["outline"]), len(pcb_outline.outline_points(25.0, 12.5, 1.0)))

    def test_the_board_outline_is_the_rounded_board(self):
        outline = viewer_parts.board(SAMPLE)["pcb"]["outline"]
        self.assertEqual(len(outline), 4 * 17)
        self.assertAlmostEqual(max(u for u, _ in outline), 25.0)
        self.assertAlmostEqual(max(v for _, v in outline), 12.5)
        # A corner is cut back: the board's corner point itself is not on it.
        self.assertNotIn([25.0, 12.5], [list(p) for p in outline])

    def test_a_missing_size_gets_the_models_default(self):
        # build_layout falls back to these when dimensions.txt has no such line.
        board = viewer_parts.board({})
        self.assertAlmostEqual(max(u for u, _ in board["pcb"]["outline"]), 13.0)
        self.assertAlmostEqual(max(v for _, v in board["pcb"]["outline"]), 5.0)


if __name__ == "__main__":
    unittest.main()
