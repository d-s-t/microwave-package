"""Tests for drawing each distinct board once per run.

A sweep over something the boards do not depend on -- the pedestal
belongs to box -- used to have Altium redraw identical boards for every
combination. These pin what makes two boards "the same" and that the
Altium stage copies instead of redrawing when they are.

Run: python -m unittest discover -s tests
"""
import os
import tempfile
import unittest
from unittest import mock

from microwave_package import paths
from microwave_package.design import altium_generation as ag
from microwave_package.design import equations as eq
from microwave_package.workflow import stages


def default_values(**changes):
    values = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    values.update(changes)
    return values


class TestBoardFingerprint(unittest.TestCase):
    def test_the_generator_is_read_for_the_names_it_uses(self):
        names = ag._names_the_generator_reads(str(paths.ALTIUM_GENERATOR))
        self.assertIn("via_max_spacing", names)
        self.assertIn("pcb_cpw_gap", names)
        self.assertNotIn("pedestal_depth", names)

    def test_the_pedestal_does_not_change_the_boards(self):
        self.assertEqual(ag.board_fingerprint(default_values(pedestal_depth=0.0)),
                         ag.board_fingerprint(default_values(pedestal_depth=4.0)))

    def test_a_parameter_the_generator_reads_changes_them(self):
        self.assertNotEqual(ag.board_fingerprint(default_values(via_max_spacing=1.5)),
                            ag.board_fingerprint(default_values(via_max_spacing=2.0)))

    def test_a_parameter_only_the_bond_layout_reads_changes_them(self):
        """bond_pad_pitch never reaches the script by name, but it moves
        every pad -- and goes into bonds.txt."""
        self.assertNotEqual(ag.board_fingerprint(default_values(bond_pad_pitch=0.4)),
                            ag.board_fingerprint(default_values(bond_pad_pitch=0.5)))

    def test_the_die_material_changes_them(self):
        """It sets the die's solved CPW gap."""
        self.assertNotEqual(ag.board_fingerprint(default_values(), {"mat_chip": "silicon"}),
                            ag.board_fingerprint(default_values(), {"mat_chip": "sapphire"}))

    def test_an_unreadable_generator_gives_no_fingerprint(self):
        """Then nothing is known to be the same, and everything is drawn."""
        with mock.patch.object(ag, "_names_the_generator_reads", return_value=set()):
            self.assertIsNone(ag.board_fingerprint(default_values()))


class FakeRun:
    def __init__(self, root, combos):
        self.root = root
        self.state = {"combos": {cid: {"params": params} for cid, params in combos.items()}}

    def combo_dir(self, combo_id):
        path = os.path.join(self.root, combo_id)
        os.makedirs(path, exist_ok=True)
        return path

    combo_path = combo_dir

    def update_combo(self, combo_id, **fields):
        self.state["combos"][combo_id].update(fields)


class TestTheAltiumStageReuses(unittest.TestCase):
    def _stage(self, root, combos):
        run = FakeRun(root, combos)
        original = eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE))
        stage = stages._AltiumStage(run, original, {}, {}, log=lambda _m: None)
        drawn = []

        def generate(combo_dir, values, materials=None, log=None):
            drawn.append(os.path.basename(combo_dir))
            with open(os.path.join(combo_dir, "copper.txt"), "w") as f:
                f.write(f"board for via {values['via_max_spacing']}\n")
            with open(os.path.join(combo_dir, "bonds.txt"), "w") as f:
                f.write("bonds\n")
            return True, ""

        return run, stage, drawn, generate

    def test_a_pedestal_sweep_draws_the_boards_once(self):
        with tempfile.TemporaryDirectory() as root:
            combos = {f"p{d}": {"pedestal_depth": d} for d in (0.0, 0.5, 1.0)}
            run, stage, drawn, generate = self._stage(root, combos)
            with mock.patch.object(ag, "generate_for_combo", side_effect=generate):
                for combo_id in combos:
                    self.assertTrue(stage.build(combo_id))
            self.assertEqual(drawn, ["p0.0"])
            for combo_id in ("p0.5", "p1.0"):
                self.assertEqual(run.state["combos"][combo_id]["altium_status"], "done")
                self.assertEqual(run.state["combos"][combo_id]["altium_reused_from"], "p0.0")
                with open(os.path.join(root, combo_id, "copper.txt")) as f:
                    self.assertIn("board for via", f.read())
                self.assertTrue(os.path.isfile(os.path.join(root, combo_id, "bonds.txt")))

    def test_different_boards_are_each_drawn(self):
        with tempfile.TemporaryDirectory() as root:
            combos = {f"v{s}": {"via_max_spacing": s} for s in (1.0, 1.5, 2.0)}
            _run, stage, drawn, generate = self._stage(root, combos)
            with mock.patch.object(ag, "generate_for_combo", side_effect=generate):
                for combo_id in combos:
                    stage.build(combo_id)
            self.assertEqual(drawn, ["v1.0", "v1.5", "v2.0"])

    def test_a_resumed_run_reuses_a_board_drawn_before_it_stopped(self):
        with tempfile.TemporaryDirectory() as root:
            combos = {"p0": {"pedestal_depth": 0.0}, "p1": {"pedestal_depth": 1.0}}
            run, stage, drawn, generate = self._stage(root, combos)
            generate(run.combo_dir("p0"), {"via_max_spacing": 0.8})
            drawn.clear()
            run.update_combo("p0", altium_status="done")
            with mock.patch.object(ag, "generate_for_combo", side_effect=generate):
                stage.build("p0")
                stage.build("p1")
            self.assertEqual(drawn, [])
            self.assertEqual(run.state["combos"]["p1"]["altium_reused_from"], "p0")


if __name__ == "__main__":
    unittest.main()
