"""The 50 ohm gap is solved for the line that is built: the lid rests on the
board's copper and its relief channel (route_width wide, route_clearence deep)
is all that is over the trace, so the solve takes the channel's depth as the
ceiling and the layout check refuses a channel that cannot carry the line.

The box study found by trial that on RT/duroid 5880 the default lid (a 0.5 mm
channel 40 um deep) mismatches the line badly -- S11 -4.8 dB on average at 2-10
GHz, against -14.4 dB with a 1.0 mm channel 200 um deep -- because the gap was
solved for an open top."""
import unittest

from microwave_package import paths
from microwave_package.design import altium_generation, constraints, cpw, equations as eq

TMM, DUROID = "Rogers TMM 10i (tm)", "Rogers RT/duroid 5880 (tm)"


def defaults():
    return eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))


def values(**override):
    return {**defaults(), "no_chip": 1.0, "pedestal_depth": 0.0, **override}


class TestLidCover(unittest.TestCase):
    def test_the_lid_is_the_relief_channels_width_and_depth(self):
        self.assertEqual(
            constraints.lid_cover({"route_width": 1.2, "route_clearence": 0.3}),
            {"channel_width": 1.2, "cover_height": 0.3})

    def test_values_with_no_lid_describe_an_open_top_line(self):
        self.assertEqual(constraints.lid_cover({"cpw_trace_width": 0.2}), {})
        self.assertEqual(constraints.lid_cover({"route_width": 1.2}), {})
        self.assertEqual(constraints.lid_cover({"route_width": 0.0, "route_clearence": 0.3}), {})


class TestBuildParams(unittest.TestCase):
    def test_the_board_gap_is_solved_under_the_lid(self):
        v = defaults()
        params = altium_generation.build_params(v)
        expected = cpw.solve_gap_for_z0(
            v["cpw_trace_width"], v["pcb_width"], 9.8, conductor_thickness=v["copper_thickness"],
            cover_height=v["route_clearence"], channel_width=v["route_width"])
        self.assertEqual(params["pcb_cpw_gap"], expected)
        # a ceiling loads the line, so the gap that makes it 50 ohm again is wider
        open_gap = cpw.solve_gap_for_z0(
            v["cpw_trace_width"], v["pcb_width"], 9.8, conductor_thickness=v["copper_thickness"])
        self.assertGreater(params["pcb_cpw_gap"], open_gap)

    def test_a_deeper_relief_asks_for_a_narrower_gap(self):
        shallow = altium_generation.build_params(values(route_clearence=0.3))["pcb_cpw_gap"]
        deep = altium_generation.build_params(values(route_clearence=0.6, route_width=1.6))["pcb_cpw_gap"]
        self.assertLess(deep, shallow)

    def test_the_candidate_board_gets_its_gap(self):
        """5880, 0.4 mm trace, 1.0 mm channel 200 um deep: the box study's candidate."""
        params = altium_generation.build_params(
            values(cpw_trace_width=0.4, route_width=1.0, route_clearence=0.2), mat_pcb=DUROID)
        self.assertAlmostEqual(params["pcb_cpw_gap"], 0.0943, delta=0.0005)

    def test_the_chip_line_is_not_under_the_relief_channel(self):
        """It sits in the lid's pocket over the die, whose ceiling is not route_clearence."""
        a = altium_generation.build_params(defaults())["chip_cpw_gap"]
        b = altium_generation.build_params(dict(defaults(), route_clearence=0.5, route_width=1.6))["chip_cpw_gap"]
        c = altium_generation.build_params(
            {k: v for k, v in defaults().items() if not k.startswith("route_")})["chip_cpw_gap"]
        self.assertEqual(a, b)
        self.assertEqual(a, c)


class TestLayoutCheckOfTheLid(unittest.TestCase):
    def test_the_shipped_defaults_pass(self):
        self.assertIsNone(constraints.check_layout(defaults()))

    def test_the_old_default_lid_is_refused_as_too_shallow(self):
        """0.5 mm wide and 40 um deep is a ceiling so close that a 0.2 mm trace on TMM 10i is a
        28 ohm line however far the slots are opened (a field solve of the real cross-section
        agrees: 27.6 ohm at the old gap)."""
        reason = constraints.check_layout(defaults() | {"route_width": 0.5, "route_clearence": 0.04}, TMM)
        self.assertIn("route_clearence", reason)
        self.assertIn("only 40 um deep", reason)
        self.assertIn("deepen route_clearence to at least", reason)

    def test_the_depth_it_asks_for_is_one_that_works(self):
        reason = constraints.check_layout(defaults() | {"route_clearence": 0.04}, TMM)
        asked = int(reason.split("to at least ")[1].split(" um")[0])
        deeper = defaults() | {"route_clearence": asked * 1.15 / 1000.0}
        self.assertNotIn("route_clearence", constraints.check_layout(deeper, TMM) or "")

    def test_a_channel_that_does_not_clear_the_cpw_is_refused_with_the_width_that_does(self):
        reason = constraints.check_layout(values(route_width=0.8), TMM)
        self.assertIn("route_width = 0.8", reason)
        self.assertIn("raise route_width to at least", reason)
        needed = float(reason.split("to at least ")[1].split(" mm")[0])
        self.assertIsNone(constraints.check_layout(values(route_width=needed + 0.01), TMM))
        self.assertIsNotNone(constraints.check_layout(values(route_width=needed - 0.05), TMM))

    def test_the_default_channel_clears_the_smp_window(self):
        # 1.2 mm: the window's corner arcs start 1.8 mm out, clear of the channel's edge.
        self.assertIsNone(constraints.check_layout(values(route_width=1.2), TMM))

    def test_a_wider_channel_still_ends_on_the_flat_window_wall(self):
        # 2.0 mm (z +-1.0) still ends on the flat inner wall, which runs to z +-1.8.
        self.assertIsNone(constraints.check_layout(values(route_width=2.0), TMM))

    def test_a_relief_with_no_depth_is_refused(self):
        reason = constraints.check_layout(values(route_clearence=0.0), TMM)
        self.assertIn("route_clearence", reason)
        self.assertIn("rest on the trace", reason)

    def test_values_that_carry_no_lid_are_checked_as_an_open_top_line(self):
        v = {k: x for k, x in values().items() if not k.startswith("route_")}
        self.assertIsNone(constraints.check_layout(v, TMM))

    def test_an_open_top_line_that_cannot_reach_50_ohm_is_still_a_no_geometry_refusal(self):
        v = {k: x for k, x in values(cpw_trace_width=0.6).items() if not k.startswith("route_")}
        self.assertIn("no 50 ohm geometry", constraints.check_layout(v, TMM))

    def test_the_material_decides_what_the_lid_has_to_clear(self):
        """The same 1.0 mm / 300 um lid: 0.5 mm trace is a 5880 board's 106 um gap and a 0.7 mm CPW,
        and a TMM 10i board's trace the lid cannot reach 50 ohm over."""
        v = values(cpw_trace_width=0.5, route_width=1.2)
        self.assertIsNone(constraints.check_layout(v, DUROID))
        self.assertIn("route_clearence", constraints.check_layout(v, TMM))


if __name__ == "__main__":
    unittest.main()
