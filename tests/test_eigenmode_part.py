"""Tests for the eigenmode part of a driven run: the child run that solves the same
model for its modes, and the lumped RLC boundaries that stand in for the driven
run's lumped ports (an eigenmode solve has no ports).

Fakes in the style of tests/test_ports.py; what HFSS makes of the boundary is only
seen in a real run.

Run: python -m unittest discover -s tests -p test_eigenmode_part.py
"""
import logging
import os
import tempfile
import unittest
from unittest import mock

from microwave_package import paths, pipeline, webapp
from microwave_package.design.sweep import SweepParameter
from microwave_package.hfss import registry
from microwave_package.hfss.driven import ports
from microwave_package.hfss.eigenmode import setup as eigen_setup
from microwave_package.workflow import companion, plotting, run_manager
from microwave_package.workflow.run_manager import Run

from test_ports import FakeHfss, FakeLayout, bead_model

SWEEP = [SweepParameter(name="pedestal_depth", values=[0.0, 1.0])]


class RlcHfss(FakeHfss):
    def __init__(self, objects):
        super().__init__(objects)
        self.rlcs = []

    def assign_lumped_rlc_to_sheet(self, **kwargs):
        self.rlcs.append(kwargs)
        return True


def rlc_model():
    model = bead_model()
    hfss = RlcHfss({})
    hfss.modeler = model.modeler
    return hfss


def quiet(message):
    pass


class TestLumpedTerminations(unittest.TestCase):
    def terminate(self, plane):
        hfss = rlc_model()
        settings = {"port_impedance_ohm": 50.0, "reference_plane": plane}
        names = ports.terminate_lumped_plane(hfss, FakeLayout(), settings, log=quiet)
        return hfss, names

    def test_the_launch_plane_gets_a_50_ohm_rlc_on_each_port_sheet_and_no_port(self):
        hfss, names = self.terminate(ports.REFERENCE_PLANE_LAUNCH)
        self.assertEqual(hfss.lumped_ports, [])
        self.assertEqual(len(hfss.modeler.rectangles), 2)
        self.assertEqual(len(hfss.rlcs), 2)
        self.assertEqual(names, ["term_port_1", "term_port_2"])
        self.assertTrue(all(r["resistance"] == 50.0 and r["rlc_type"] == "Parallel" for r in hfss.rlcs))

    def test_the_integration_line_is_the_ports_ground_to_trace_line(self):
        hfss, _ = self.terminate(ports.REFERENCE_PLANE_LAUNCH)
        port_hfss = bead_model()
        ports.place_ports(port_hfss, FakeLayout(),
                          {"solution_type": "DrivenModal", "reference_plane": ports.REFERENCE_PLANE_LAUNCH})
        for rlc, port in zip(hfss.rlcs, port_hfss.lumped_ports):
            self.assertEqual(rlc["start_direction"], [list(p) for p in port["integration_line"]])

    def test_the_end_ports_sit_at_minus_and_plus_x(self):
        hfss, _ = self.terminate(ports.REFERENCE_PLANE_LAUNCH)
        offset = FakeLayout().frame.x_offset
        xs = sorted(rlc["start_direction"][0][0] - offset for rlc in hfss.rlcs)
        self.assertAlmostEqual(xs[0], -xs[1])
        self.assertGreater(xs[1], 0)

    def test_the_chip_plane_gets_100_ohm_on_each_of_four_gap_sheets(self):
        hfss, names = self.terminate(ports.REFERENCE_PLANE_CHIP)
        self.assertEqual(len(hfss.rlcs), 4)
        self.assertTrue(all(r["resistance"] == 100.0 for r in hfss.rlcs))
        self.assertEqual(hfss.lumped_ports, [])

    def test_the_connector_plane_has_no_lumped_sheet(self):
        hfss, names = self.terminate(ports.REFERENCE_PLANE_CONNECTOR)
        self.assertEqual((names, hfss.rlcs, hfss.modeler.rectangles), ([], [], []))

    def test_a_refused_boundary_refuses_the_model(self):
        hfss = rlc_model()
        hfss.assign_lumped_rlc_to_sheet = lambda **kw: False
        with self.assertRaises(registry.PortPlacementError):
            ports.terminate_lumped_plane(
                hfss, FakeLayout(), {"reference_plane": ports.REFERENCE_PLANE_LAUNCH}, log=quiet)

    def test_the_eigenmode_solver_places_them_when_asked_and_not_otherwise(self):
        solver = registry.get("Eigenmode")
        hfss = rlc_model()
        base = {"reference_plane": ports.REFERENCE_PLANE_LAUNCH, "port_impedance_ohm": 50.0}
        self.assertEqual(solver.place_ports(hfss, FakeLayout(), base, log=quiet), [])
        self.assertEqual(hfss.rlcs, [])
        solver.place_ports(hfss, FakeLayout(), {**base, "eigen_terminate_ports": True}, log=quiet)
        self.assertEqual(len(hfss.rlcs), 2)


class FakeSetup:
    def __init__(self):
        self.props = {}


class FakeSetupHfss:
    def create_setup(self, name):
        self.made = FakeSetup()
        return self.made


class TestEigenSetupFloor(unittest.TestCase):
    SETTINGS = {"modes": 20, "max_passes": 5, "min_passes": 1, "min_converged": 1,
                "max_delta_f": 3.0, "min_freq_safety_factor": 0.1}

    def test_an_explicit_floor_is_used_as_given(self):
        hfss = FakeSetupHfss()
        eigen_setup.create_setup(hfss, "", {**self.SETTINGS, "eigen_min_ghz": 4.0}, log=quiet)
        self.assertEqual(hfss.made.props["MinimumFrequency"], "4.0GHz")
        self.assertEqual(hfss.made.props["NumModes"], 20)


class RunsRoot(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)

    def driven(self, **extra):
        run = Run.create(SWEEP, {**pipeline.DEFAULT_ANALYSIS_SETTINGS, "solution_type": "DrivenModal",
                                 "reference_plane": "launch", "f_start_ghz": 2.0, "f_stop_ghz": 20.0,
                                 **extra}, {})
        for combo in run.state["combos"]:
            run.update_combo(combo, post_status="done")
        return run


class TestCreatingAnEigenmodePart(RunsRoot):
    def setUp(self):
        super().setUp()
        self.parent = self.driven()
        self.combos = list(self.parent.state["combos"])

    def test_an_eigenmode_run_of_the_chosen_combinations_with_terminations_on(self):
        child = companion.create_eigenmode_part(self.parent, self.combos[:1], 12, 4.0)
        s = child.analysis_settings
        self.assertEqual(list(child.state["combos"]), self.combos[:1])
        self.assertEqual((s["solution_type"], s["modes"], s["eigen_min_ghz"]), ("Eigenmode", 12, 4.0))
        self.assertTrue(s["eigen_terminate_ports"])
        self.assertEqual(s["reference_plane"], "launch")

    def test_connectors_kept_are_terminated_in_the_port_impedance_too(self):
        child = companion.create_eigenmode_part(self.parent, self.combos)
        self.assertEqual(child.analysis_settings["connector_termination_ohm"], 50.0)
        self.assertEqual(child.analysis_settings["modes"], companion.MAX_EIGEN_MODES)

    def test_ignored_connectors_have_no_bore_to_terminate(self):
        parent = self.driven(launch_connectors="removed")
        child = companion.create_eigenmode_part(parent, list(parent.state["combos"]))
        self.assertEqual(child.analysis_settings["connector_termination_ohm"], 0.0)

    def test_the_floor_defaults_to_the_parents_band_start(self):
        child = companion.create_eigenmode_part(self.parent, self.combos, min_ghz="")
        self.assertEqual(child.analysis_settings["eigen_min_ghz"], 2.0)

    def test_linked_both_ways_and_apart_from_the_hidden_pair(self):
        child = companion.create_eigenmode_part(self.parent, self.combos)
        reloaded = Run.load(self.parent.run_id)
        self.assertEqual([r.run_id for r in companion.eigenmode_parts(reloaded)], [child.run_id])
        self.assertEqual(companion.eigenmode_parent(Run.load(child.run_id)).run_id, self.parent.run_id)
        self.assertIsNone(companion.prerequisite_of(child))
        self.assertNotIn(companion.EIGEN_LINK, reloaded.config)
        self.assertEqual(run_manager.part_kind(child.config), "eigenmodes")
        self.assertIn(child.run_id, [r.run_id for r in companion.parts_of(reloaded)])

    def test_a_second_part_is_another_run(self):
        a = companion.create_eigenmode_part(self.parent, self.combos)
        b = companion.create_eigenmode_part(Run.load(self.parent.run_id), self.combos)
        self.assertNotEqual(a.run_id, b.run_id)

    def test_what_cannot_be_done_says_why(self):
        for combos, modes, floor, message in (
            ([], 20, 4, "at least one combination"),
            (["nope"], 20, 4, "no results"),
            (self.combos, 0, 4, "between 1 and"),
            (self.combos, 21, 4, "between 1 and"),
            (self.combos, "x", 4, "whole number"),
            (self.combos, 20, "x", "number"),
            (self.combos, 20, -1, "zero or more"),
        ):
            with self.assertRaisesRegex(ValueError, message):
                companion.create_eigenmode_part(self.parent, combos, modes, floor)

    def test_an_eigenmode_run_cannot_be_the_parent(self):
        eigen = Run.create(SWEEP, {**pipeline.DEFAULT_ANALYSIS_SETTINGS, "solution_type": "Eigenmode"}, {})
        with self.assertRaisesRegex(ValueError, "Driven"):
            companion.create_eigenmode_part(eigen, list(eigen.state["combos"]))


class TestTheModesOnThePlot(RunsRoot):
    def setUp(self):
        super().setUp()
        self.parent = self.driven()
        self.combos = list(self.parent.state["combos"])
        self.child = companion.create_eigenmode_part(self.parent, self.combos[:1], 5, 4.0)
        folder = self.child.combo_path(self.combos[0])
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "results.csv"), "w", encoding="utf-8") as handle:
            handle.write("freq_ghz,q" + chr(10) + "6.9,250" + chr(10) + "8.1,40" + chr(10))

    def test_the_parts_modes_are_markers_flagged_as_terminated(self):
        markers = companion.mode_markers(Run.load(self.parent.run_id))
        self.assertEqual([(m["combo_id"], m["freq_ghz"], m["q"], m["terminated"]) for m in markers],
                         [(self.combos[0], 6.9, 250.0, True), (self.combos[0], 8.1, 40.0, True)])

    def test_they_are_a_legend_entry_of_their_own_with_q_on_hover(self):
        import pandas as pd
        frame = pd.DataFrame([{"combo_id": self.combos[0], "freq_ghz": f, "s21_db": -f, "s11_db": -1.0}
                              for f in (4.0, 6.0, 9.0)])
        markers = companion.mode_markers(Run.load(self.parent.run_id))
        html = plotting.figure_html(frame, [], x="freq_ghz", y="s21_db", solution_type="DrivenModal",
                                    mode_markers=markers, plotlyjs_src="plotly.js")
        self.assertIn("Eigenmodes (terminated)", html)
        self.assertIn("terminated eigenmode 6.9000 GHz, Q 250", html)
        self.assertIn("terminated eigenmode 8.1000 GHz, Q 40", html)


class TestTheRoute(RunsRoot):
    def setUp(self):
        super().setUp()
        self.parent = self.driven()
        self.client = webapp.app.test_client()

    def test_the_run_page_offers_the_form_and_posting_makes_the_part_and_starts_it(self):
        page = self.client.get(f"/runs/{self.parent.run_id}").get_data(as_text=True)
        self.assertIn('id="eigenmodes"', page)
        self.assertIn("Solve eigenmodes", page)
        combo = list(self.parent.state["combos"])[0]
        with mock.patch.object(webapp, "_start_pipeline_thread") as start:
            reply = self.client.post(f"/runs/{self.parent.run_id}/eigenmodes",
                                     data={"combo": combo, "modes": "10", "min_ghz": "4"})
        self.assertEqual(reply.status_code, 302)
        start.assert_called_once()
        parts = companion.eigenmode_parts(Run.load(self.parent.run_id))
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].analysis_settings["modes"], 10)

    def test_a_refusal_comes_back_as_an_error_on_the_page(self):
        reply = self.client.post(f"/runs/{self.parent.run_id}/eigenmodes", data={"modes": "10"})
        self.assertIn("error=", reply.headers["Location"])


if __name__ == "__main__":
    unittest.main()
