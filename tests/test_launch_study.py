"""The launch study as a solution type: what it registers, how a run of it is
set up, what it draws (against a recording stand-in for HFSS, as the
line-impedance tests do), and how its results are summarised. No AEDT.

Run: python -m unittest discover -s tests
"""
import cmath
import math
import os
import tempfile
import unittest
from unittest import mock

import numpy as np

from microwave_package import paths, pipeline, webapp
from microwave_package.design import copper_geometry as cg
from microwave_package.design.sweep import SweepParameter
from microwave_package.hfss import model_builder, registry
from microwave_package.hfss.driven import ports as driven_ports
from microwave_package.hfss.launch import LAUNCH_STUDY, build, results
from tests.test_line_check import MATERIALS, RecordingHfss

VALUES = {"cpw_trace_width": 0.2, "launch_clearance": 0.2, "port_space": 6.0, "pcb_length": 50.0,
          "pcb_width": 0.381, "copper_thickness": 0.035, "lid_width": 1.5, "route_width": 1.2,
          "route_clearence": 0.3, "port_length": 5.1, "smp_window_extra": 0.25}
SETTINGS = {"materials": MATERIALS, "launch_trace_length_mm": 8.0, "launch_half_width_mm": 3.5,
            "launch_port_ohm": 50.0, "launch_modes": 1}
TOP = 0.5 + 0.381 + 0.035          # the copper's top face: floor, board, copper


def draw(extra=None, **overrides):
    values = {**VALUES, **(extra or {})}
    settings = {**SETTINGS, **overrides}
    hfss = RecordingHfss()
    launch, eps_r, _gap = build.make_launch(values, settings)
    with mock.patch.object(cg, "build", return_value={}), \
            mock.patch.object(model_builder, "place_connectors", return_value=["connector_1_outer"]) as place, \
            mock.patch.object(driven_ports, "_connector_wave_ports", return_value=["port_1"]):
        names, ports, heights = build.draw_model(hfss, launch, values, settings, eps_r, log=lambda _m: None)
    return launch, hfss, names, ports, heights, place


class TestRegistration(unittest.TestCase):
    def setUp(self):
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)

    def test_it_draws_its_own_model_and_copper(self):
        solver = registry.get(LAUNCH_STUDY)
        self.assertFalse(solver.needs_solid)
        self.assertFalse(solver.needs_altium)
        self.assertTrue(solver.standalone)
        self.assertEqual(solver.hfss_type, "DrivenModal")
        self.assertIsNotNone(solver.build_model)
        self.assertIn(LAUNCH_STUDY, registry.standalone_types())

    def test_the_other_solvers_still_need_altium_and_can_pair(self):
        for value in ("Eigenmode", "DrivenModal"):
            self.assertTrue(registry.get(value).needs_altium)
            self.assertFalse(registry.get(value).standalone)

    def test_its_settings_are_in_every_runs_defaults(self):
        defaults = registry.all_defaults()
        self.assertEqual((defaults["launch_f_start_ghz"], defaults["launch_f_stop_ghz"]), (2.0, 20.0))

    def test_a_run_never_starts_altium_or_pairs(self):
        run = pipeline.create_run(
            [SweepParameter(name="launch_taper_length", values=[0.0, 0.5])],
            {**pipeline.DEFAULT_ANALYSIS_SETTINGS, "solution_type": LAUNCH_STUDY, "run_both": True,
             "use_altium": True}, {})
        self.assertFalse(run.analysis_settings["use_altium"])
        self.assertEqual(os.listdir(paths.RUNS_ROOT), [run.run_id])

    def test_the_form_offers_it_and_the_launch_levers(self):
        page = webapp.app.test_client().get("/").get_data(as_text=True)
        self.assertIn('value="LaunchStudy"', page)
        self.assertIn('name="launch_f_stop_ghz"', page)
        self.assertIn("Launch taper length", page)


class TestRunPage(unittest.TestCase):
    def test_the_page_lists_a_finished_combination_and_not_another_kinds_run(self):
        import json
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)
        run = pipeline.create_run(
            [SweepParameter(name="launch_taper_length", values=[0.5])],
            {**pipeline.DEFAULT_ANALYSIS_SETTINGS, "solution_type": LAUNCH_STUDY}, {})
        combo = next(iter(run.state["combos"]))
        self.assertEqual(webapp._launch_summaries(run), [])              # nothing read back yet
        os.makedirs(run.combo_path(combo), exist_ok=True)
        with open(os.path.join(run.combo_path(combo), "launch_summary.json"), "w") as handle:
            json.dump({"s11_max_db": -9.0, "tdr_peaks": [{"distance_mm": 2.0, "gamma": 0.3}]}, handle)
        found = webapp._launch_summaries(run)
        self.assertEqual([c["combo_id"] for c in found], [combo])
        self.assertEqual(found[0]["first_peak"]["gamma"], 0.3)


class TestWhatIsDrawn(unittest.TestCase):
    def test_the_board_stack_and_vacuum(self):
        launch, hfss, names, _ports, heights, _ = draw()
        self.assertEqual(hfss.made["vacuum"]["sizes"][0], 2 * launch.half_length)
        self.assertAlmostEqual(hfss.made["vacuum"]["sizes"][1], heights["lid_top"])
        self.assertEqual(hfss.made["pcb_substrate"]["origin"][1], build.FLOOR_MM)
        self.assertAlmostEqual(heights["copper_top"], TOP)
        self.assertEqual(names["pcb"], "pcb_substrate")

    def test_the_lid_has_its_channel_and_the_connectors_window_cut_out(self):
        launch, hfss, *_ = draw()
        cut = [s for s in hfss.subtractions if s[0] == ["lid_inside"] or s[0] == "lid_inside"]
        self.assertEqual(len(cut), 1)
        self.assertEqual(list(cut[0][1]), ["lid_channel_cut", "lid_end_relief_cut", "lid_window_cut"])
        window = hfss.made["lid_window_cut"]
        self.assertAlmostEqual(window["sizes"][0], 5.1 + 0.5)
        self.assertAlmostEqual(window["origin"][0] + window["sizes"][0] / 2,
                               launch.half_length + launch.origin_u)

    def test_the_lid_is_cut_deeper_over_the_pads_inboard_end_as_the_real_lid_is(self):
        """The centre-pin tab runs under the last millimetre before the window to the pad. The real
        lid's ceiling there is 0.4 mm, not the channel's 0.3, and a plain 0.3 mm ceiling shorts the
        tab to the lid (the first baseline reflected everything)."""
        for taper in (0.0,):
            launch, hfss, _n, _p, heights, _ = draw({"launch_taper_length": taper})
            deep = hfss.made["lid_end_relief_cut"]
            pad_end = launch.half_length + launch.pin_u + launch.pad_length / 2.0
            window = hfss.made["lid_window_cut"]
            window_inboard_wall = window["origin"][0] + window["sizes"][0]
            self.assertGreaterEqual(deep["origin"][0] + deep["sizes"][0], pad_end)    # covers the pad's inboard end
            self.assertLessEqual(deep["origin"][0], window_inboard_wall)             # and starts at the window
            self.assertAlmostEqual(deep["origin"][1] + deep["sizes"][1], heights["copper_top"] + 0.4)
            self.assertAlmostEqual(deep["sizes"][2], 1.2)                           # the channel's width
            self.assertAlmostEqual(deep["sizes"][0], 1.0 + 0.05)

    def test_a_deeper_channel_than_the_end_relief_needs_no_extra_cut(self):
        _l, hfss, *_ = draw({"route_clearence": 0.5})
        self.assertNotIn("lid_end_relief_cut", hfss.made)

    def test_a_relief_over_the_launch_adds_a_pocket(self):
        _l, hfss, *_ = draw({"launch_relief_depth": 0.5})
        cut = next(s for s in hfss.subtractions if "lid_inside" in (s[0] if isinstance(s[0], list) else [s[0]]))
        self.assertIn("lid_launch_pocket_cut", cut[1])
        pocket = hfss.made["lid_launch_pocket_cut"]
        self.assertAlmostEqual(pocket["origin"][1] + pocket["sizes"][1], 0.5 + TOP)

    def test_the_pocket_reaches_past_the_pad_step_and_taper_not_just_the_connector(self):
        for taper in (0.0, 1.0):
            launch, hfss, *_ = draw({"launch_relief_depth": 0.5, "launch_taper_length": taper})
            pocket = hfss.made["lid_launch_pocket_cut"]
            pad_end = launch.half_length + launch.pin_u + launch.pad_length / 2.0
            step_end = launch.half_length + launch.origin_u + launch.pull_reach
            end = pocket["origin"][0] + pocket["sizes"][0]
            self.assertGreater(end, pad_end + taper)
            self.assertGreater(end, step_end + taper)
            window = hfss.made["lid_window_cut"]
            self.assertGreater(end, window["origin"][0] + window["sizes"][0])      # past the window's far edge

    def test_a_bottom_cutout_takes_the_copper_and_the_floor_from_under_the_pad(self):
        values = {**VALUES, "launch_bottom_cutout_radius": 1.0}
        hfss = RecordingHfss()
        launch, eps_r, _gap = build.make_launch(values, SETTINGS)
        made = {"pcb_copper_bottom": ["pcb_copper_bottom"]}
        with mock.patch.object(cg, "build", return_value=made), \
                mock.patch.object(model_builder, "place_connectors", return_value=[]), \
                mock.patch.object(driven_ports, "_connector_wave_ports", return_value=["port_1"]):
            build.draw_model(hfss, launch, values, SETTINGS, eps_r, log=lambda _m: None)
        blanks = [s[0] for s in hfss.subtractions]
        self.assertIn(["pcb_copper_bottom"], blanks)
        self.assertIn(["box_floor"], blanks)
        discs = [v for k, v in hfss.made.items() if v["kind"] == "cylinder" and v["radius"] == 1.0]
        self.assertTrue(discs)
        self.assertAlmostEqual(discs[0]["origin"][0], launch.half_length + launch.pin_u)

    def test_no_via_stands_in_a_bottom_cutout(self):
        from microwave_package.hfss.launch import geometry
        plain = geometry.from_values(VALUES, 0.2474)
        cut = geometry.from_values({**VALUES, "launch_bottom_cutout_radius": 1.6}, 0.2474)
        self.assertGreater(len(geometry.vias(plain)), len(geometry.vias(cut)))
        for via in geometry.vias(cut):
            self.assertGreaterEqual(math.hypot(via.x - cut.pin_u, via.y), 1.6 + 0.15)

    def test_the_cpw_port_is_a_narrow_face_on_the_cut_not_the_whole_end(self):
        launch, hfss, *_ = draw()
        sheet = hfss.made["port_2_sheet"]
        self.assertAlmostEqual(sheet["origin"][2], -build.DEFAULT_PORT_HALF_WIDTH)
        self.assertAlmostEqual(sheet["sizes"][1], 2 * build.DEFAULT_PORT_HALF_WIDTH)
        self.assertLess(sheet["sizes"][1], 2 * launch.half_width)

    def test_a_port_face_that_would_cut_the_lids_channel_is_refused(self):
        with self.assertRaises(registry.PortPlacementError):
            draw(launch_port_half_width_mm=0.5)

    def test_one_connector_on_the_near_launch(self):
        launch, _h, _n, _p, heights, place = draw()
        kwargs = place.call_args.kwargs
        self.assertEqual(kwargs["count"], 1)
        self.assertEqual(kwargs["half_length"], launch.half_length)
        self.assertEqual(kwargs["port_space"], 6.0)
        self.assertAlmostEqual(kwargs["board_top"], heights["copper_top"])

    def test_port_one_is_the_connector_bore_and_port_two_a_matched_cpw_on_the_cut(self):
        launch, hfss, _n, ports, _h, _p = draw()
        self.assertEqual(ports, ["port_1", "port_2"])
        port = hfss.ports[0]
        self.assertEqual((port["name"], port["impedance"], port["modes"]), ("port_2", 50.0, 1))
        (x1, _y1, z1), (_x2, _y2, z2) = port["line"]
        self.assertEqual(x1, 2 * launch.half_length)
        self.assertAlmostEqual(z1, launch.trace / 2)
        self.assertAlmostEqual(z2 - z1, launch.gap)
        self.assertEqual(hfss.made["port_2_sheet"]["orientation"], "YZ")

    def test_a_void_wider_than_the_land_is_refused_before_anything_is_drawn(self):
        with self.assertRaises(registry.PortPlacementError) as caught:
            build.make_launch({**VALUES, "launch_void_radius": 2.8}, SETTINGS)
        self.assertIn("land", str(caught.exception))

    def test_a_wider_trace_gets_a_different_gap_for_the_same_impedance(self):
        settings = {**SETTINGS, "materials": {**MATERIALS, "mat_pcb": "Rogers RO3003 (tm)"}}
        _l, _e, narrow = build.make_launch({**VALUES, "cpw_trace_width": 0.4}, settings)
        _l, _e, wide = build.make_launch({**VALUES, "cpw_trace_width": 0.48}, settings)
        self.assertLess(narrow, wide)

    def test_a_gap_under_the_layout_checks_minimum_is_refused(self):
        """On RO3003 a 0.2 mm trace needs a 48 um gap: AEDT cannot unite copper that close."""
        settings = {**SETTINGS, "materials": {**MATERIALS, "mat_pcb": "Rogers RO3003 (tm)"}}
        with self.assertRaises(registry.PortPlacementError) as caught:
            build.make_launch({**VALUES, "cpw_trace_width": 0.2}, settings)
        self.assertIn("um gap", str(caught.exception))

    def test_the_boards_own_traces_are_buildable_baselines(self):
        for material, trace in (("Rogers RO3003 (tm)", 0.4), ("Rogers RO4003 (tm)", 0.35),
                                ("Rogers TMM 10i (tm)", 0.2)):
            settings = {**SETTINGS, "materials": {**MATERIALS, "mat_pcb": material}}
            launch, _eps, _gap = build.make_launch({**VALUES, "cpw_trace_width": trace}, settings)
            self.assertEqual(launch.problems(), [], material)

    def test_a_trace_no_gap_can_make_50_ohm_is_refused_with_the_reason(self):
        """On TMM 10i under the 0.3 mm relief nothing wider than the 0.2 mm trace reaches 50 ohm."""
        with self.assertRaises(registry.PortPlacementError) as caught:
            build.make_launch({**VALUES, "cpw_trace_width": 0.4}, SETTINGS)
        self.assertIn("unreachable", str(caught.exception))


class TestSummary(unittest.TestCase):
    def reflection(self, gamma, distance_mm, eps_r):
        freq = np.arange(2.0, 20.01, 0.25)
        eps_eff = (eps_r + 1.0) / 2.0
        delay_ns = 2.0 * distance_mm * math.sqrt(eps_eff) / 299.792458
        return freq, gamma * np.exp(-2j * math.pi * freq * delay_ns)

    def test_a_single_reflection_is_found_where_it_is_with_its_size(self):
        freq, s11 = self.reflection(0.3, 2.0, 9.8)
        out = results.summarize(freq, s11, 9.8)
        peak = out["tdr_peaks"][0]
        self.assertAlmostEqual(peak["distance_mm"], 2.0, delta=0.15)
        self.assertAlmostEqual(peak["gamma"], 0.3, delta=0.03)
        self.assertAlmostEqual(out["s11_max_db"], 20 * math.log10(0.3), delta=0.01)
        self.assertEqual(sorted(out["s11_mean_db"]), ["10-17", "17-20", "2-10"])

    def test_an_uneven_grid_gets_the_levels_but_no_time_domain_read(self):
        freq = np.array([2.0, 3.0, 5.0, 8.0, 9.0, 13.0, 14.0, 20.0])
        out = results.summarize(freq, np.full(8, 0.1 + 0j), 3.0)
        self.assertIn("tdr_note", out)
        self.assertNotIn("tdr_peaks", out)
        self.assertAlmostEqual(out["s11_max_db"], -20.0, places=3)

    def test_rows_give_the_complex_s11(self):
        rows = [{"freq_ghz": 2.0 + i, "s11_db": -20.0, "s11_deg": 90.0} for i in range(10)]
        _freq, s11 = results.s11_of(rows)
        self.assertAlmostEqual(abs(s11[0]), 0.1)
        self.assertAlmostEqual(cmath.phase(s11[0]), math.pi / 2)


if __name__ == "__main__":
    unittest.main()
