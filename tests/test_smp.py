"""Tests for the SMP land pattern and where the launch sits.

The numbers are Wuerth's, read out of the KiCad footprint shipped with
WR-SMP 60124102122403. These tests exist to catch them being edited by
hand, and to pin the placement rule the board depends on.
"""
import unittest

from eigen_mode_analyze import smp


class TestTheVendorNumbers(unittest.TestCase):
    def test_signal_pad_matches_the_vendor_footprint(self):
        u, v, su, sv = smp.SIGNAL_PAD
        self.assertEqual((u, v, su, sv), (0.00, 2.90, 0.48, 1.40))

    def test_three_ground_pads_form_the_u(self):
        """Two arms along the signal axis and a strap across its closed
        end -- the strap is what joins the two sides of the ground."""
        self.assertEqual(len(smp.PADS), 4)
        arms = [p for p in smp.PADS if abs(p[0]) > 1]
        self.assertEqual(len(arms), 2)
        self.assertAlmostEqual(smp.GROUND_STRAP[0], 0.0)

    def test_extents_follow_from_the_pads(self):
        self.assertAlmostEqual(smp.OUTER_V, -2.60)
        self.assertAlmostEqual(smp.INNER_V, 3.60)
        self.assertAlmostEqual(smp.LENGTH, 6.20)

    def test_the_signal_pad_is_the_innermost_copper(self):
        """The centre conductor meets the launch there, so nothing may
        reach past it."""
        self.assertAlmostEqual(smp.INNER_V, smp.SIGNAL_PAD[1] + smp.SIGNAL_PAD[3] / 2)


class TestPlacement(unittest.TestCase):
    HALF_L = 13.0
    PORT_SPACE = 1.5

    def test_outermost_copper_sits_port_space_from_the_edge(self):
        origin = smp.launch_origin(self.HALF_L, self.PORT_SPACE)
        outer = origin - smp.OUTER_V
        self.assertAlmostEqual(outer, self.HALF_L - self.PORT_SPACE)

    def test_launch_centre_is_where_the_trace_ends(self):
        self.assertAlmostEqual(smp.launch_centre(self.HALF_L, self.PORT_SPACE), 6.0)

    def test_inner_edge_is_inboard_of_the_centre(self):
        self.assertLess(
            smp.launch_inner_edge(self.HALF_L, self.PORT_SPACE),
            smp.launch_centre(self.HALF_L, self.PORT_SPACE),
        )

    def test_the_whole_launch_stays_on_the_board(self):
        origin = smp.launch_origin(self.HALF_L, self.PORT_SPACE)
        for _u, v, _su, sv in smp.PADS:
            for edge in (v - sv / 2, v + sv / 2):
                self.assertLess(abs(origin - edge), self.HALF_L)

    def test_moving_the_connector_out_moves_everything_together(self):
        a = smp.launch_centre(self.HALF_L, 1.5)
        b = smp.launch_centre(self.HALF_L, 2.5)
        self.assertAlmostEqual(a - b, 1.0)


class TestParameterHandoff(unittest.TestCase):
    def test_every_pad_reaches_the_parameter_file(self):
        """The DelphiScript reads these rather than carrying its own
        copy, so the vendor's numbers live in one place."""
        params = smp.as_parameters()
        for prefix in ("smp_sig", "smp_arml", "smp_armr", "smp_strap"):
            for suffix in ("u", "v", "su", "sv"):
                self.assertIn(f"{prefix}_{suffix}", params)
        self.assertIn("smp_outer_v", params)
        self.assertIn("smp_inner_v", params)

    def test_the_values_are_the_pads(self):
        params = smp.as_parameters()
        self.assertAlmostEqual(params["smp_sig_v"], smp.SIGNAL_PAD[1])
        self.assertAlmostEqual(params["smp_strap_su"], smp.GROUND_STRAP[2])


if __name__ == "__main__":
    unittest.main()
