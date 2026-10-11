"""A bare board (no_chip = 1) has no pedestal unless one is asked for.

The web form used to be the only place that pinned pedestal_depth = 0 for a
bare board; the command line did not, so a CLI bare-board run kept the 0.5 mm
recess under a whole ground plane and failed HFSS validation ("pcb_copper_bottom
and box intersect"). The rule now lives in `sweep.pin_bare_board_pedestal`, applied
by `pipeline.create_run`, and the layout check refuses what slips past it.

Run: python -m unittest discover -s tests
"""
import logging
import unittest

from microwave_package import paths
from microwave_package.design import constraints, equations as eq
from microwave_package.design.sweep import SweepParameter, pin_bare_board_pedestal


def setUpModule():
    logging.disable(logging.INFO)


def tearDownModule():
    logging.disable(logging.NOTSET)


BASE = {"pedestal_depth": 0.5, "no_chip": 0.0}


def sweep(name, *values):
    return SweepParameter(name=name, values=list(values))


class TestThePin(unittest.TestCase):
    def test_a_pinned_bare_board_gets_no_pedestal(self):
        self.assertEqual(pin_bare_board_pedestal([], {"no_chip": 1.0}, BASE),
                         {"no_chip": 1.0, "pedestal_depth": 0.0})

    def test_an_ordinary_board_is_left_alone(self):
        self.assertEqual(pin_bare_board_pedestal([], {}, BASE), {})
        self.assertEqual(pin_bare_board_pedestal([], {"no_chip": 0.0}, BASE), {"no_chip": 0.0})

    def test_a_typed_pedestal_wins(self):
        self.assertEqual(pin_bare_board_pedestal([], {"no_chip": 1.0, "pedestal_depth": 0.3}, BASE),
                         {"no_chip": 1.0, "pedestal_depth": 0.3})
        out = pin_bare_board_pedestal([sweep("pedestal_depth", 0, 0.5)], {"no_chip": 1.0}, BASE)
        self.assertNotIn("pedestal_depth", out)

    def test_a_run_mixing_bare_and_ordinary_boards_is_left_alone(self):
        self.assertNotIn("pedestal_depth", pin_bare_board_pedestal([sweep("no_chip", 0, 1)], {}, BASE))

    def test_a_swept_all_bare_board_still_counts(self):
        # two values, both bare: nothing is an ordinary board
        out = pin_bare_board_pedestal([sweep("no_chip", 1, 1)], {}, BASE)
        self.assertEqual(out["pedestal_depth"], 0.0)

    def test_the_equation_files_own_no_chip_counts_when_nothing_pins_it(self):
        self.assertEqual(pin_bare_board_pedestal([], {}, {**BASE, "no_chip": 1.0})["pedestal_depth"], 0.0)

    def test_the_callers_dict_is_not_changed(self):
        given = {"no_chip": 1.0}
        pin_bare_board_pedestal([], given, BASE)
        self.assertEqual(given, {"no_chip": 1.0})

    def test_a_file_without_a_pedestal_is_left_alone(self):
        self.assertEqual(pin_bare_board_pedestal([], {"no_chip": 1.0}, {"no_chip": 0.0}), {"no_chip": 1.0})


class TestTheCreatePath(unittest.TestCase):
    def test_create_run_hands_the_pin_to_the_run(self):
        from microwave_package import pipeline

        seen = {}

        class Boom(Exception):
            pass

        def fake_create(sweep_params, settings, defaults=None, **kw):
            seen["defaults"] = defaults
            return "run"

        original = pipeline.Run.create
        pipeline.Run.create = staticmethod(fake_create)
        try:
            pipeline.create_run([], {"solution_type": "Eigenmode"}, {"no_chip": 1.0})
        finally:
            pipeline.Run.create = original
        self.assertEqual(seen["defaults"].get("pedestal_depth"), 0.0)


class TestTheLayoutCheck(unittest.TestCase):
    def values(self, **kw):
        base = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
        return dict(base, **kw)

    def test_a_bare_board_with_a_pedestal_is_refused_saying_why(self):
        reason = constraints.check_layout(self.values(no_chip=1, pedestal_depth=0.5))
        self.assertIn("pedestal_depth", reason)
        self.assertIn("bare board", reason)

    def test_a_bare_board_without_one_is_not_refused_for_it(self):
        reason = constraints.check_layout(self.values(no_chip=1, pedestal_depth=0))
        self.assertTrue(reason is None or "pedestal_depth" not in reason)

    def test_an_ordinary_board_keeps_its_pedestal(self):
        reason = constraints.check_layout(self.values(no_chip=0, pedestal_depth=0.5))
        self.assertTrue(reason is None or "bare board" not in reason)


if __name__ == "__main__":
    unittest.main()
