"""Every on/off switch is pinned explicitly, and the model stage checks that
the geometry it was given is the one the run asked for.

Run: python -m unittest discover -s tests
"""
import logging
import os
import tempfile
import unittest
from unittest import mock

from microwave_package import paths, webapp
from microwave_package.design import equations as eq, parameter_meta, step_generation as sg
from microwave_package.design.sweep import SweepParameter, pin_switches
from microwave_package.workflow import stages


def setUpModule():
    logging.disable(logging.INFO)


def tearDownModule():
    logging.disable(logging.NOTSET)


def committed_values():
    return eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))


class TestTheSwitchPins(unittest.TestCase):
    def test_the_pins_are_the_committed_values(self):
        values = committed_values()
        for name, pinned in parameter_meta.SWITCH_PINS.items():
            self.assertEqual(values[name], pinned, name)

    def test_an_unpinned_switch_is_pinned(self):
        pinned = pin_switches([], None, committed_values())
        self.assertEqual(pinned, parameter_meta.SWITCH_PINS)

    def test_a_switch_the_run_sweeps_or_names_is_left_alone(self):
        sweep = [SweepParameter(name="no_chip", values=[0.0, 1.0])]
        pinned = pin_switches(sweep, {"screws_on": 0.0}, committed_values())
        self.assertNotIn("no_chip", pinned)
        self.assertEqual(pinned["screws_on"], 0.0)
        self.assertEqual(pinned["vents_on"], 1.0)

    def test_a_file_without_the_switch_gets_no_pin(self):
        pinned = pin_switches([], None, {"pedestal_depth": 0.2})
        self.assertEqual(pinned, {})

    def test_the_pedestal_rule_now_sees_an_explicit_chip_run(self):
        """A chip run used to take its no_chip from the file; with the pin in
        place the pedestal rule sees 0 and does not zero the chip's pedestal."""
        from microwave_package.design.sweep import pin_bare_board_pedestal
        base = dict(committed_values(), pedestal_depth=0.2)
        defaults = pin_switches([], None, base)
        out = pin_bare_board_pedestal([], defaults, dict(base, no_chip=1.0))
        self.assertNotIn("pedestal_depth", out)


class TestTheFormPins(unittest.TestCase):
    def test_a_legacy_run_without_a_pin_opens_the_bare_board_tick_unticked(self):
        """Even if the equations file says no_chip = 1, a run that never
        pinned it does not open the form ticked."""
        self.assertFalse(webapp._ticked(parameter_meta.NO_CHIP_PARAM, {}, {"no_chip": 1.0}))

    def test_a_pinned_bare_board_opens_ticked(self):
        self.assertTrue(webapp._ticked(parameter_meta.NO_CHIP_PARAM, {"no_chip": 1.0}, {}))
        self.assertFalse(webapp._ticked(parameter_meta.NO_CHIP_PARAM, {"no_chip": 0.0}, {"no_chip": 1.0}))


class FakeRun:
    def __init__(self, root, params, defaults):
        self.root = root
        self.state = {"combos": {"c1": {"params": params}}}
        self.parameter_defaults = defaults
        self.updates = []

    def combo_dir(self, combo_id):
        path = os.path.join(self.root, combo_id)
        os.makedirs(path, exist_ok=True)
        return path

    def update_combo(self, combo_id, **fields):
        self.state["combos"][combo_id].update(fields)
        self.updates.append(fields)


class TestTheModelStageCheck(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def stage(self, run, handoff=None):
        return stages._AedtStage(run, {}, handoff or mock.Mock(), lambda _m: None)

    def write_dims(self, run, text):
        with open(os.path.join(run.combo_dir("c1"), "dimensions.txt"), "w", encoding="utf-8") as f:
            f.write(text)

    def test_a_chip_run_built_as_a_bare_board_is_refused_before_the_handoff(self):
        run = FakeRun(self._tmp.name, {}, {})   # no pin: expects no_chip = 0
        self.write_dims(run, '"no_chip"= 1\n')
        handoff = mock.Mock()
        with mock.patch.object(stages.model_builder, "build_model") as build:
            self.assertFalse(self.stage(run, handoff).build("c1"))
        build.assert_not_called()
        handoff.assert_not_called()
        self.assertEqual(run.state["combos"]["c1"]["model_status"], "failed")
        self.assertIn("no_chip", run.state["combos"]["c1"]["model_error"])

    def test_a_pinned_bare_board_passes_the_check(self):
        run = FakeRun(self._tmp.name, {}, {"no_chip": 1.0})
        self.write_dims(run, '"no_chip"= 1\n')
        with mock.patch.object(stages.model_builder, "build_model", return_value={"a": "b"}) as build:
            self.assertTrue(self.stage(run).build("c1"))
        build.assert_called_once()


if __name__ == "__main__":
    unittest.main()
