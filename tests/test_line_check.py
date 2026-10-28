"""Tests for the line-impedance check: the section's geometry (taken from the
copper file where there is one), the impedance maths, what is drawn, and the
plumbing that makes it a solution type of its own. No AEDT: HFSS is a recording
stand-in.

Run: python -m unittest discover -s tests
"""
import cmath
import json
import logging
import math
import os
import tempfile
import unittest
from unittest import mock

from microwave_package import paths, pipeline, webapp
from microwave_package.design import copper_geometry as cg, equations as eq
from microwave_package.design.sweep import SweepParameter
from microwave_package.hfss import model_builder, registry
from microwave_package.hfss.line import LINE_IMPEDANCE, build, impedance, results, section as sec
from microwave_package.workflow import postprocess, stages
from microwave_package.workflow.run_manager import Run
from tests.test_material_rules import FakeHfss, FakeObject

COPPER = """\
board=package
track|top|-13.5|0|13.5|0|0.4
region|top|-25,0.31,25,0.31,25,12.5,-25,12.5
region|top|-25,-12.5,25,-12.5,25,-0.31,-25,-0.31
region|bottom|-25,-12.5,25,-12.5,25,12.5,-25,12.5
via|0|1.0|0.3|0.15
via|2|-1.0|0.3|0.15
via|2|0.0|0.3|0.15
via|10|1.0|0.3|0.15
board=chip
track|top|-3|0|3|0|0.4
region|top|-3,3,3,3,3,29.5,-3,29.5
region|top|-3,-29.5,3,-29.5,3,-3,-3,-3
"""

TMM = "Rogers TMM 10i (tm)"


def values(**over):
    base = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    return {**base, "cpw_trace_width": 0.4, "route_width": 1.0, "route_clearence": 0.2, **over}


def boards():
    return cg.parse(COPPER)


class TestMeasuringTheBuiltLine(unittest.TestCase):
    def test_the_trace_the_gap_and_the_ground_are_read_from_the_copper(self):
        m = sec.measure_line(boards()["package"], length=6.0, half_width=3.0)
        self.assertEqual(m.trace_width, 0.4)
        self.assertAlmostEqual(m.gap, 0.11)             # 0.31 pour edge - 0.2 half trace
        self.assertTrue(m.ground_below)
        self.assertEqual(m.x_range, (-3.0, 3.0))        # the window, centred on the track

    def test_the_window_is_clipped_to_the_track(self):
        m = sec.measure_line(boards()["chip"], length=20.0, half_width=3.0)
        self.assertEqual(m.x_range, (-3.0, 3.0))

    def test_only_the_vias_inside_the_window_and_the_width_count(self):
        m = sec.measure_line(boards()["package"], length=6.0, half_width=3.0)
        self.assertEqual({(v[0], v[1]) for v in m.vias}, {(0.0, 1.0), (2.0, -1.0), (2.0, 0.0)})

    def test_a_board_with_no_track_has_no_line(self):
        self.assertIsNone(sec.measure_line(cg.BoardCopper(name="empty"), 6.0, 3.0))

    def test_the_chip_line_has_its_own_wide_gap_and_no_ground_below(self):
        m = sec.measure_line(boards()["chip"], length=6.0, half_width=3.0)
        self.assertAlmostEqual(m.gap, 2.8)
        self.assertFalse(m.ground_below)


class TestTheSection(unittest.TestCase):
    def board_section(self):
        return sec.from_copper(boards()["package"], sec.BOARD, values(), TMM, 9.8, 6.0, 3.0, analytic_gap=0.2)

    def test_from_copper_uses_the_drawn_numbers_not_the_analytic_ones(self):
        s = self.board_section()
        self.assertEqual(s.source, "copper")
        self.assertAlmostEqual(s.gap, 0.11)
        self.assertEqual(s.trace_width, 0.4)
        self.assertEqual(s.length, 6.0)

    def test_the_vias_are_in_the_sections_own_frame_and_clear_of_the_gap(self):
        s = self.board_section()
        # the via at y = 0 sits on the trace: it is not a ground post and is dropped
        self.assertEqual(sorted((round(x, 6), round(z, 6)) for x, z, _d in s.vias), [(3.0, 1.0), (5.0, -1.0)])

    def test_the_stack_heights(self):
        s = self.board_section()
        t, h = s.copper_thickness, s.substrate_height
        self.assertEqual((s.floor, s.substrate_bottom), (0.0, t))
        self.assertAlmostEqual(s.substrate_top, t + h)
        self.assertAlmostEqual(s.copper_top, 2 * t + h)
        self.assertAlmostEqual(s.top, 2 * t + h + s.lid_width)

    def test_the_gap_between_trace_and_pour_is_the_gap(self):
        parts = {p.name: p for p in self.board_section().parts()}
        trace, pour_a, pour_b = (parts["pcb_copper_top"], parts["pcb_copper_top_ground_a"],
                                 parts["pcb_copper_top_ground_b"])
        self.assertAlmostEqual(pour_a.lo[2] - trace.hi[2], 0.11)
        self.assertAlmostEqual(trace.lo[2] - pour_b.hi[2], 0.11)

    def test_the_lid_sits_on_the_copper_with_the_channel_over_the_trace(self):
        s = self.board_section()
        parts = {p.name: p for p in s.parts()}
        for name in ("lid_inside_a", "lid_inside_b"):
            self.assertAlmostEqual(parts[name].lo[1], s.copper_top)     # on the ground copper
        roof = parts["lid_inside_c"]
        self.assertAlmostEqual(roof.lo[1], s.copper_top + s.route_clearence)
        self.assertAlmostEqual(roof.lo[2], -s.route_width / 2.0)
        self.assertAlmostEqual(roof.hi[2], s.route_width / 2.0)
        self.assertAlmostEqual(parts["lid_inside_a"].lo[2], s.route_width / 2.0)

    def test_no_relief_means_one_flat_lid_on_the_trace(self):
        s = sec.from_parameters(sec.BOARD, values(route_width=0.0), TMM, 9.8, 6.0, 3.0, 0.11)
        self.assertIn("lid_inside", [p.name for p in s.parts()])

    def test_the_chip_line_has_no_lower_copper_and_air_below_the_die(self):
        s = sec.from_copper(boards()["chip"], sec.CHIP, values(), "silicon", 11.9, 6.0, 3.0)
        names = [p.name for p in s.parts()]
        self.assertNotIn("chip_copper_bottom", names)
        self.assertIn("chip_substrate", names)
        self.assertEqual(s.substrate_bottom, values()["pedestal_depth"])

    def test_a_section_with_a_gap_that_fills_it_is_refused(self):
        s = sec.from_parameters(sec.BOARD, values(), TMM, 9.8, 6.0, 0.5, 0.4)
        self.assertTrue(any("widen it" in p for p in s.problems()))

    def test_a_channel_no_wider_than_the_trace_is_refused(self):
        s = sec.from_parameters(sec.BOARD, values(route_width=0.45), TMM, 9.8, 6.0, 3.0, 0.11)
        self.assertTrue(any("less than 0.1 mm wider" in p for p in s.problems()))

    def test_a_section_narrower_than_the_lid_channel_is_refused(self):
        s = sec.from_parameters(sec.BOARD, values(route_width=1.6), TMM, 9.8, 3.0, 0.8, 0.11)
        self.assertTrue(any("side walls would be cut off" in p for p in s.problems()))

    def test_a_sound_section_has_no_problems(self):
        self.assertEqual(self.board_section().problems(), [])

    def test_from_parameters_says_the_fence_is_not_verified_and_makes_one(self):
        s = sec.from_parameters(sec.BOARD, values(), TMM, 9.8, 6.0, 3.0, 0.11)
        self.assertEqual(s.source, "parameters")
        self.assertTrue(any("not read from a layout" in n for n in s.notes))
        self.assertGreater(len(s.vias), 0)
        self.assertEqual(len({round(abs(z), 6) for _x, z, _d in s.vias}), 1)   # one row each side

    def test_the_two_ports_stand_at_the_ends_and_the_voltage_path_crosses_a_gap(self):
        s = self.board_section()
        self.assertEqual(s.port_sheet(0)[0], 0.0)
        self.assertEqual(s.port_sheet(1)[0], 6.0)
        (x0, y0, z0), (x1, y1, z1) = s.integration_line(0)
        self.assertEqual((x0, x1, y0, y1), (0.0, 0.0, y0, y0))
        self.assertAlmostEqual(z1 - z0, s.gap)


class TestTheImpedanceMaths(unittest.TestCase):
    @staticmethod
    def sparams(zc, z0, theta):
        """S11, S21 of an ideal line of impedance zc and electrical length theta in a z0 system."""
        a = d = cmath.cos(theta)
        b = 1j * zc * cmath.sin(theta)
        c = 1j * cmath.sin(theta) / zc
        denominator = a + b / z0 + c * z0 + d
        return (a + b / z0 - c * z0 - d) / denominator, 2.0 / denominator

    def test_a_line_of_known_impedance_is_recovered_at_every_length(self):
        for zc in (35.0, 50.0, 61.0, 90.0):
            for theta in (0.2, 0.7, 1.3, 2.4):
                s11, s21 = self.sparams(zc, 50.0, theta)
                self.assertAlmostEqual(impedance.zc_from_sparams(s11, s21, 50.0).real, zc, places=6)

    def test_a_matched_line_reads_the_reference(self):
        s11, s21 = self.sparams(50.0, 50.0, 1.0)
        self.assertAlmostEqual(abs(s11), 0.0, places=9)
        self.assertAlmostEqual(impedance.zc_from_sparams(s11, s21).real, 50.0)

    def test_a_half_wavelength_section_is_not_ill_read(self):
        """At theta = pi the inversion divides by nothing: a hair of S11 noise
        read 35 ohm for a 51 ohm line. Those points are refused, not reported."""
        s11, s21 = self.sparams(51.0, 50.0, math.pi - 0.02)
        with self.assertRaisesRegex(ValueError, "ill-conditioned"):
            impedance.zc_from_sparams(s11 + 0.002, s21)
        s11, s21 = self.sparams(51.0, 50.0, 0.4)           # a short section reads fine
        self.assertAlmostEqual(impedance.zc_from_sparams(s11, s21).real, 51.0, places=6)

    def test_nothing_transmitted_is_not_an_impedance(self):
        with self.assertRaises(ValueError):
            impedance.zc_from_sparams(0.98 + 0j, 0.0 + 0j)

    def test_db_and_degrees_round_trip(self):
        value = impedance.s_complex(-6.0206, 90.0)
        self.assertAlmostEqual(abs(value), 0.5, places=3)
        self.assertAlmostEqual(math.degrees(cmath.phase(value)), 90.0)

    def test_rows_get_their_impedance_and_a_bad_row_says_why(self):
        s11, s21 = self.sparams(60.0, 50.0, 0.9)
        good = {"freq_ghz": 5.0, "s11_db": 20 * math.log10(abs(s11)), "s11_deg": math.degrees(cmath.phase(s11)),
                "s21_db": 20 * math.log10(abs(s21)), "s21_deg": math.degrees(cmath.phase(s21))}
        dead = {"freq_ghz": 6.0, "s11_db": -0.1, "s11_deg": 0.0, "s21_db": -300.0, "s21_deg": 0.0}
        rows = impedance.derive_rows([good, dead])
        self.assertAlmostEqual(rows[0]["z0_ohm"], 60.0, places=4)
        self.assertIn("z0_error", rows[1])

    def test_pass_means_every_frequency_is_inside_the_window(self):
        rows = [{"freq_ghz": f, "z0_ohm": z} for f, z in ((1, 50.0), (2, 51.0), (3, 49.0))]
        self.assertTrue(impedance.evaluate(rows, 50.0, 2.5)["passed"])

    def test_one_frequency_outside_fails_and_is_named(self):
        rows = [{"freq_ghz": f, "z0_ohm": z} for f, z in ((1, 50.0), (2, 56.0), (3, 49.0))]
        verdict = impedance.evaluate(rows, 50.0, 2.5)
        self.assertFalse(verdict["passed"])
        self.assertEqual(verdict["worst_freq_ghz"], 2)
        self.assertAlmostEqual(verdict["share_in_tolerance"], 2 / 3)

    def test_no_impedance_at_all_fails_with_a_reason(self):
        verdict = impedance.evaluate([{"freq_ghz": 1.0}], 50.0, 2.5)
        self.assertFalse(verdict["passed"])
        self.assertIn("no impedance", verdict["reason"])

    def test_a_gap_correction_has_the_right_sign(self):
        self.assertGreater(impedance.gap_to_reach(50.0, 40.0, 0.1, 0.4), 0.1)     # reads low: widen
        self.assertLess(impedance.gap_to_reach(50.0, 60.0, 0.1, 0.4), 0.1)        # reads high: narrow


class RecordingHfss(FakeHfss):
    """FakeHfss that also draws: records what was made, where, and the ports."""

    def __init__(self):
        super().__init__([])
        self.made = {}
        self.ports = []
        self.sheets = []

    def _add(self, name, kind, **info):
        obj = FakeObject(name)
        self._objects.append(obj)
        self.made[name] = {"kind": kind, **info}
        return obj

    def __getitem__(self, name):
        return next(o for o in self._objects if o.name == name)

    def create_box(self, origin, sizes, name=None, material=None):
        return self._add(name, "box", origin=origin, sizes=sizes)

    def create_cylinder(self, orientation, origin, radius, height, name=None, **_k):
        return self._add(name, "cylinder", origin=origin, radius=radius, height=height)

    def create_polyline(self, points, close_surface=True, cover_surface=True, name=None):
        obj = self._add(name, "sheet", points=points)
        self.sheets.append(name)
        return obj

    def create_rectangle(self, orientation, origin, sizes, name=None):
        return self._add(name, "rectangle", origin=origin, sizes=sizes, orientation=orientation)

    def wave_port(self, assignment, integration_line, modes, impedance, name, renormalize):
        self.ports.append({"name": name, "assignment": assignment, "line": integration_line,
                           "modes": modes, "impedance": impedance, "renormalize": renormalize})
        return object()


DUROID = "Rogers RT/duroid 5880 (tm)"
MATERIALS = {"mat_box": "aluminum", "mat_pcb": TMM, "mat_chip": "silicon", "mat_vacuum": "vacuum",
             "perfect_e_box": True, "mat_conductor_pcb": "copper", "mat_conductor_chip": "niobium",
             "perfect_e_conductors": True, "mat_wirebond": "gold", "include_wirebonds": True}


class TestDrawing(unittest.TestCase):
    def draw(self, which=sec.BOARD, thin=False):
        board = boards()["package" if which == sec.BOARD else "chip"]
        s = sec.from_copper(board, which, values(), TMM if which == sec.BOARD else "silicon",
                            9.8 if which == sec.BOARD else 11.9, 6.0, 3.0)
        hfss = RecordingHfss()
        names, ports = build.draw_section(hfss, s, MATERIALS, 50.0, 1, log=lambda _m: None)
        return s, hfss, names, ports

    def test_every_part_is_drawn_and_the_vacuum_is_the_whole_section(self):
        s, hfss, names, _ports = self.draw()
        for part in s.parts():
            self.assertIn(part.name, hfss.made)
        lo, hi = s.region()
        self.assertEqual(hfss.made["vacuum"]["origin"], list(lo))
        self.assertAlmostEqual(hfss.made["vacuum"]["sizes"][1], s.top)

    def test_the_names_pick_the_packages_own_materials_and_boundaries(self):
        _s, hfss, names, _ports = self.draw()
        # copper and the lid are perfect E, exactly as in the package
        for name in hfss.made:
            if "copper" in name or "lid_inside" in name or "_via_" in name:
                self.assertIn(name, hfss.perfect_e, name)
        self.assertEqual(names["pcb"], "pcb_substrate")
        self.assertEqual(names["vacuum"], "vacuum")

    def test_the_vacuum_is_carved_by_the_substrate_only(self):
        _s, hfss, _names, _ports = self.draw()
        target, tools, _keep = hfss.subtractions[-1]
        self.assertEqual(target, "vacuum")
        self.assertEqual(tools, ["pcb_substrate"])

    def test_two_wave_ports_renormalised_with_a_voltage_path_each(self):
        s, hfss, _names, ports = self.draw()
        self.assertEqual(ports, ["port_1", "port_2"])
        for port, end in zip(hfss.ports, (0, 1)):
            self.assertEqual(port["impedance"], 50.0)
            self.assertTrue(port["renormalize"])
            self.assertEqual(port["modes"], 1)
            start, stop = s.integration_line(end)
            self.assertEqual(port["line"], [list(start), list(stop)])
        self.assertEqual(hfss.made["port_1_sheet"]["orientation"], "YZ")

    def test_the_chip_line_draws_its_film_as_sheets(self):
        s, hfss, _names, _ports = self.draw(sec.CHIP)
        self.assertTrue(s.copper_thickness < model_builder.SHEET_BELOW_MM)
        self.assertIn("chip_copper_top", hfss.sheets)
        self.assertEqual(hfss.made["chip_copper_top"]["kind"], "sheet")


class FakeSetup:
    def __init__(self):
        self.props = {}

    def update(self):
        pass


class SetupHfss:
    def __init__(self):
        self.setup = FakeSetup()
        self.sweeps = []

    def create_setup(self, name):
        return self.setup

    def create_linear_count_sweep(self, **kw):
        self.sweeps.append(kw)
        return object()


class TestSetupAndSection(unittest.TestCase):
    SETTINGS = {"line_f_start_ghz": 1.0, "line_f_stop_ghz": 26.0, "line_points": 53, "line_delta_s": 0.02,
                "max_passes": 4, "min_passes": 1, "min_converged": 1}

    def test_the_setup_adapts_at_the_top_and_sweeps_the_band(self):
        hfss = SetupHfss()
        build.create_setup(hfss, "", self.SETTINGS, log=lambda _m: None)
        self.assertEqual(hfss.setup.props["Frequency"], "26.0GHz")
        self.assertEqual(hfss.setup.props["MaxDeltaS"], 0.02)
        self.assertEqual(hfss.setup.props["MaximumPasses"], 4)
        sweep = hfss.sweeps[0]
        self.assertEqual((sweep["start_frequency"], sweep["stop_frequency"], sweep["num_of_freq_points"]),
                         (1.0, 26.0, 53))
        self.assertEqual(sweep["sweep_type"], "Interpolating")

    def test_a_backwards_band_is_refused(self):
        with self.assertRaises(ValueError):
            build.create_setup(SetupHfss(), "", {**self.SETTINGS, "line_f_start_ghz": 30.0}, log=lambda _m: None)

    def settings(self, **over):
        return {"which_line": sec.BOARD, "materials": {**MATERIALS, "mat_pcb": DUROID}, "section_length_mm": 6.0,
                "section_half_width_mm": 3.0, "line_z0_ohm": 50.0, **over}

    def test_the_section_comes_from_the_copper_file_when_there_is_one(self):
        folder = tempfile.mkdtemp()
        with open(os.path.join(folder, "copper.txt"), "w") as handle:
            handle.write(COPPER)
        s, analytic = build.make_section(values(), self.settings(), folder, log=lambda _m: None)
        self.assertEqual(s.source, "copper")
        self.assertAlmostEqual(s.gap, 0.11)
        self.assertGreater(analytic, 0.0)            # the analytic gap, to compare with

    def test_without_a_copper_file_it_is_re_derived_and_says_so(self):
        s, analytic = build.make_section(values(cpw_trace_width=0.2), self.settings(), tempfile.mkdtemp(),
                                         log=lambda _m: None)
        self.assertEqual(s.source, "parameters")
        self.assertAlmostEqual(s.gap, analytic)

    def test_a_chip_line_on_a_board_with_no_chip_says_why(self):
        folder = tempfile.mkdtemp()
        with open(os.path.join(folder, "copper.txt"), "w") as handle:
            handle.write(COPPER.split("board=chip")[0])
        with self.assertRaisesRegex(registry.PortPlacementError, "no chip line"):
            build.make_section(values(), self.settings(which_line=sec.CHIP), folder, log=lambda _m: None)

    def test_a_section_that_would_short_is_refused_before_anything_is_drawn(self):
        folder = tempfile.mkdtemp()
        with open(os.path.join(folder, "copper.txt"), "w") as handle:
            handle.write(COPPER)
        with self.assertRaisesRegex(registry.PortPlacementError, "less than 0.1 mm wider"):
            build.make_section(values(route_width=0.4), self.settings(), folder, log=lambda _m: None)

    def test_the_board_gap_is_solved_under_the_lid_and_the_chip_one_on_its_own_trace(self):
        board, _ = build.make_section(values(cpw_trace_width=0.45), self.settings(), tempfile.mkdtemp(),
                                      log=lambda _m: None)
        self.assertAlmostEqual(board.gap, 0.129, places=3)          # the 2D Extractor's 50.17 ohm board
        chip, _ = build.make_section(values(cpw_trace_width=0.4, chip_trace_width=0.2),
                                     self.settings(which_line=sec.CHIP), tempfile.mkdtemp(),
                                     log=lambda _m: None)
        self.assertEqual(chip.trace_width, 0.2)

    def test_a_gap_set_by_hand_wins_over_the_layout_and_says_so(self):
        folder = tempfile.mkdtemp()
        with open(os.path.join(folder, "copper.txt"), "w") as handle:
            handle.write(COPPER)
        s, analytic = build.make_section(values(), self.settings(line_gap_mm=0.111), folder, log=lambda _m: None)
        self.assertEqual((s.source, s.gap), ("parameters", 0.111))
        self.assertTrue(any("by hand" in n for n in s.notes))
        self.assertGreater(analytic, 0.0)


class TestReadingBack(unittest.TestCase):
    def test_the_port_impedance_quantities_are_found_best_first(self):
        found = results.port_impedance_quantities(["S(1,1)", "Z0(1,1)", "Zpv(1)", "Zpi(1)", "VSWR(1)"])
        self.assertEqual(found, ["Zpi(1)", "Zpv(1)", "Z0(1,1)"])

    def test_none_of_them_means_an_empty_list(self):
        self.assertEqual(results.port_impedance_quantities(["S(1,1)", "S(2,1)"]), [])


class TestPlumbing(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)

    def test_it_is_a_registered_solution_type_with_its_own_settings(self):
        solver = registry.get(LINE_IMPEDANCE)
        self.assertFalse(solver.needs_solid)
        self.assertIsNotNone(solver.build_model)
        self.assertEqual(solver.hfss_type, "DrivenModal")
        defaults = registry.all_defaults()
        self.assertEqual((defaults["line_z0_ohm"], defaults["line_tolerance_ohm"]), (50.0, 2.5))
        self.assertEqual(defaults["which_line"], "board")

    def test_the_other_solvers_still_open_a_design_of_their_own_type(self):
        for value in ("Eigenmode", "DrivenModal"):
            self.assertEqual(registry.get(value).hfss_type, value)

    def test_the_form_lists_it_and_reads_its_settings(self):
        page = webapp.app.test_client().get("/").get_data(as_text=True)
        self.assertIn('value="LineImpedance"', page)
        self.assertIn('name="line_tolerance_ohm"', page)
        from werkzeug.datastructures import MultiDict
        got = webapp._solve_settings_from_form(MultiDict({
            "solution_type": "LineImpedance", "run_both": "on", "line_tolerance_ohm": "1.5",
            "which_line": "chip"}))
        self.assertEqual(got["solution_type"], "LineImpedance")
        self.assertEqual((got["line_tolerance_ohm"], got["which_line"]), (1.5, "chip"))
        self.assertFalse(got["run_both"])              # a line check is not half of a pair

    def test_a_line_run_is_one_run_and_needs_no_two_ports(self):
        run = pipeline.create_run(
            [SweepParameter(name="port_count", values=[1.0])],
            {**pipeline.DEFAULT_ANALYSIS_SETTINGS, "solution_type": LINE_IMPEDANCE, "run_both": True}, {})
        self.assertEqual(registry.solution_type_of(run.analysis_settings), LINE_IMPEDANCE)
        self.assertEqual(os.listdir(paths.RUNS_ROOT), [run.run_id])       # no hidden eigenmode part

    def test_the_task_says_driven_to_the_worker_and_line_to_the_client(self):
        run = Run.create([SweepParameter(name="pedestal_depth", values=[0.0])],
                         {**pipeline.DEFAULT_ANALYSIS_SETTINGS, "solution_type": LINE_IMPEDANCE}, {})
        combo = next(iter(run.state["combos"]))
        run.update_combo(combo, task_id="line_1", object_names={})
        task = postprocess.task_from_run(run, combo, run.analysis_settings)
        self.assertEqual(task["solution_type"], "DrivenModal")
        self.assertEqual(task["metadata"]["solver"], LINE_IMPEDANCE)

    def test_a_task_from_the_queue_is_read_back_by_the_line_solver(self):
        """The worker sees a DrivenModal task; the client finds the solver from the metadata."""
        task = {"solution_type": "DrivenModal", "metadata": {"solver": LINE_IMPEDANCE}}
        picked = registry.get((task.get("metadata") or {}).get("solver") or task.get("solution_type"))
        self.assertIs(picked, registry.get(LINE_IMPEDANCE))
        old_task = {"solution_type": "DrivenModal"}                  # queued before this existed
        self.assertIs(registry.get((old_task.get("metadata") or {}).get("solver") or old_task.get("solution_type")),
                      registry.get("DrivenModal"))

    def test_solidworks_is_not_started_for_a_solver_that_draws_its_own_model(self):
        run = Run.create([SweepParameter(name="pedestal_depth", values=[0.0])],
                         {**pipeline.DEFAULT_ANALYSIS_SETTINGS, "solution_type": LINE_IMPEDANCE,
                          "use_altium": False}, {})
        combo = next(iter(run.state["combos"]))
        # A copy of the committed file: a run without SolidWorks takes the lock on it, and
        # the real base_parts folder is never written by the suite.
        folder = tempfile.TemporaryDirectory(prefix="equations_")
        self.addCleanup(folder.cleanup)
        equations = os.path.join(folder.name, "default.txt")
        eq.write_file(equations, eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
        with mock.patch.object(stages, "_SolidWorksStage") as solidworks, \
                mock.patch.object(stages, "_AedtStage") as aedt:
            aedt.return_value.build.return_value = True
            stages.run_local_stages(
                run, equations_path=equations, part_paths={},
                analysis_settings=run.analysis_settings, handoff=lambda *_a: None,
                parameter_defaults={}, log=lambda _m: None)
        solidworks.assert_not_called()
        aedt.return_value.build.assert_called_once_with(combo)           # straight to AEDT, nothing to wait for
        snapshot = os.path.join(run.combo_path(combo), "dimensions.txt")
        self.assertTrue(os.path.isfile(snapshot))                        # what the AEDT stage reads
        self.assertEqual(run.state["combos"][combo]["step_status"], "done")

    def test_a_normal_run_still_uses_solidworks(self):
        self.assertTrue(registry.get("Eigenmode").needs_solid)
        self.assertTrue(registry.get("DrivenModal").needs_solid)


class TestPageVerdict(unittest.TestCase):
    def test_a_line_run_page_shows_each_combinations_verdict(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)
        run = Run.create([SweepParameter(name="pedestal_depth", values=[0.0])],
                         {**pipeline.DEFAULT_ANALYSIS_SETTINGS, "solution_type": LINE_IMPEDANCE}, {})
        combo = next(iter(run.state["combos"]))
        with open(os.path.join(run.combo_dir(combo), "line_check.json"), "w") as handle:
            json.dump({"passed": False, "min_ohm": 41.0, "max_ohm": 49.0, "target_ohm": 50.0,
                       "tolerance_ohm": 2.5, "worst_ohm": 41.0, "worst_freq_ghz": 12.0, "line": "board",
                       "gap_mm": 0.111, "analytic_gap_mm": 0.111, "geometry_from": "copper"}, handle)
        page = webapp.app.test_client().get(f"/runs/{run.run_id}").get_data(as_text=True)
        self.assertIn("<h2>Line impedance</h2>", page)
        self.assertIn("FAIL", page)
        self.assertIn("41.0", page)
        self.assertEqual(len(webapp._line_checks(run)), 1)

    def test_other_runs_have_no_such_section(self):
        run = mock.Mock()
        run.analysis_settings = {"solution_type": "DrivenModal"}
        self.assertEqual(webapp._line_checks(run), [])


if __name__ == "__main__":
    unittest.main()
