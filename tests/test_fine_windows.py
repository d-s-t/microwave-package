"""Tests for the driven sweep's fine windows around the eigenmodes.

A box mode is f/Q wide -- tens of MHz -- and the coarse sweep steps ~100
MHz, so the dip is only as deep as the nearest sample. The windows are
arithmetic on (f, Q) pairs, so all of it is testable without AEDT.

Run: python -m unittest discover -s tests
"""
import unittest

from microwave_package.hfss import registry
from microwave_package.hfss.driven import results as driven_results
from microwave_package.hfss.driven import setup as driven_setup
from microwave_package.hfss.driven import windows
from microwave_package.hfss.driven.windows import Window

SETTINGS = {
    "f_start_ghz": 1.0, "f_stop_ghz": 20.0, "sweep_points": 401,
    "max_delta_s": 0.02, "max_passes": 10, "min_passes": 2, "min_converged": 1,
}


class TestWindowsFromModes(unittest.TestCase):
    def test_half_width_and_step_follow_f_over_q(self):
        [w] = windows.windows_from_modes([(10.0, 200.0)], 1.0, 20.0, margin_percent=0.0)
        self.assertAlmostEqual(w.hi - w.lo, 2 * 0.5 * 10.0 / 200.0)   # +-f/(2Q)
        self.assertAlmostEqual(w.step, 10.0 / 200.0 / 20.0)            # f/(20Q)
        self.assertAlmostEqual((w.lo + w.hi) / 2, 10.0)

    def test_the_margin_widens_it_by_a_percent_of_f_each_side(self):
        [bare] = windows.windows_from_modes([(10.0, 200.0)], 1.0, 20.0, margin_percent=0.0)
        [wide] = windows.windows_from_modes([(10.0, 200.0)], 1.0, 20.0, margin_percent=2.0)
        self.assertAlmostEqual((wide.hi - wide.lo) - (bare.hi - bare.lo), 2 * 0.2)

    def test_modes_at_or_above_the_top_of_the_band_get_no_window(self):
        found = windows.windows_from_modes([(10.0, 100.0), (20.0, 100.0), (25.0, 100.0)], 1.0, 20.0)
        self.assertEqual(len(found), 1)

    def test_a_window_is_clipped_to_the_band(self):
        [w] = windows.windows_from_modes([(1.005, 50.0)], 1.0, 20.0)
        self.assertEqual(w.lo, 1.0)
        [w] = windows.windows_from_modes([(19.99, 50.0)], 1.0, 20.0)
        self.assertEqual(w.hi, 20.0)

    def test_a_missing_or_infinite_q_uses_the_fallback(self):
        [expected] = windows.windows_from_modes([(10.0, 100.0)], 1.0, 20.0)
        for q in (None, float("inf"), float("nan"), 0, -5):
            [got] = windows.windows_from_modes([(10.0, q)], 1.0, 20.0, fallback_q=100.0)
            self.assertEqual(got, expected, q)

    def test_the_factors_are_settings_not_constants(self):
        [narrow] = windows.windows_from_modes(
            [(10.0, 100.0)], 1.0, 20.0, halfwidth_linewidths=0.25, margin_percent=0.0)
        [wide] = windows.windows_from_modes(
            [(10.0, 100.0)], 1.0, 20.0, halfwidth_linewidths=1.0, margin_percent=0.0)
        self.assertAlmostEqual((wide.hi - wide.lo) / (narrow.hi - narrow.lo), 4.0)
        [coarse] = windows.windows_from_modes([(10.0, 100.0)], 1.0, 20.0, points_per_linewidth=5)
        [fine] = windows.windows_from_modes([(10.0, 100.0)], 1.0, 20.0, points_per_linewidth=40)
        self.assertAlmostEqual(coarse.step / fine.step, 8.0)

    def test_nonsense_factors_are_refused(self):
        with self.assertRaises(ValueError):
            windows.windows_from_modes([(10.0, 100.0)], 1.0, 20.0, points_per_linewidth=0)


class TestManualWindows(unittest.TestCase):
    def test_lo_hi_step_separated_by_semicolons_or_lines(self):
        self.assertEqual(
            windows.parse_manual("12.1:12.4:0.002; 15:15.2:0.001\n17:17.5:0.01"),
            [Window(12.1, 12.4, 0.002), Window(15.0, 15.2, 0.001), Window(17.0, 17.5, 0.01)])

    def test_blank_is_none(self):
        self.assertEqual(windows.parse_manual(""), [])
        self.assertEqual(windows.parse_manual(None), [])

    def test_a_malformed_window_says_what_it_should_look_like(self):
        for bad in ("12.1:12.4", "a:b:c", "12.4:12.1:0.01", "1:2:0"):
            with self.assertRaisesRegex(ValueError, "lo:hi:step|lo < hi"):
                windows.parse_manual(bad)


class TestMergeAndCap(unittest.TestCase):
    def test_overlapping_windows_become_one_at_the_finer_step(self):
        merged = windows.merge([Window(5.0, 5.2, 0.01), Window(5.1, 5.3, 0.002), Window(8.0, 8.1, 0.01)])
        self.assertEqual(merged, [Window(5.0, 5.3, 0.002), Window(8.0, 8.1, 0.01)])

    def test_points_include_both_ends(self):
        self.assertEqual(Window(1.0, 1.1, 0.01).points, 11)

    def test_over_the_cap_every_step_is_coarsened_and_no_window_dropped(self):
        wins = [Window(5.0, 5.3, 0.001), Window(9.0, 9.3, 0.001)]   # 301 points each
        capped = windows.cap_points(wins, 200)
        self.assertEqual(len(capped), 2)
        self.assertLessEqual(windows.total_points(capped), 200)
        self.assertGreater(windows.total_points(capped), 150)       # thinned, not gutted
        self.assertEqual([(w.lo, w.hi) for w in capped], [(w.lo, w.hi) for w in wins])
        self.assertAlmostEqual(capped[0].step, capped[1].step)

    def test_under_the_cap_nothing_changes(self):
        wins = [Window(5.0, 5.3, 0.01)]
        self.assertEqual(windows.cap_points(wins, 1000), wins)

    def test_a_cap_too_small_for_every_window_keeps_each_at_the_minimum(self):
        wins = [Window(float(i), i + 0.1, 0.001) for i in range(1, 11)]
        capped = windows.cap_points(wins, 5)
        self.assertEqual(len(capped), 10)
        self.assertTrue(all(w.points == windows.MIN_WINDOW_POINTS for w in capped))


class TestPlan(unittest.TestCase):
    MODES = [(5.0, 150.0), (5.02, 150.0), (12.0, 300.0), (30.0, 100.0)]

    def test_auto_windows_come_from_the_modes_below_the_top(self):
        planned = windows.plan({**SETTINGS, "fine_windows": "auto"}, self.MODES)
        self.assertEqual(len(planned), 2)          # the two near 5 GHz merge; 30 GHz is out
        self.assertLess(planned[0].lo, 5.0)
        self.assertGreater(planned[0].hi, 5.02)

    def test_off_leaves_only_the_manual_windows(self):
        settings = {**SETTINGS, "fine_windows": "off", "fine_windows_manual": "8:8.2:0.01"}
        self.assertEqual(windows.plan(settings, self.MODES), [Window(8.0, 8.2, 0.01)])

    def test_manual_windows_work_with_no_eigenmodes_at_all(self):
        settings = {**SETTINGS, "fine_windows": "auto", "fine_windows_manual": "8:8.2:0.01"}
        self.assertEqual(windows.plan(settings, []), [Window(8.0, 8.2, 0.01)])

    def test_auto_with_no_modes_and_no_manual_windows_is_empty(self):
        self.assertEqual(windows.plan(SETTINGS, []), [])

    def test_the_point_cap_is_a_setting(self):
        many = [(2.0 + i, 100.0) for i in range(15)]
        planned = windows.plan({**SETTINGS, "fine_max_points": 300}, many)
        self.assertLessEqual(windows.total_points(planned), 300)
        self.assertEqual(len(planned), 15)

    def test_manual_windows_outside_the_band_are_clipped_or_dropped(self):
        settings = {**SETTINGS, "fine_windows": "off",
                    "fine_windows_manual": "19.9:21:0.01; 25:26:0.01; 0.5:1.2:0.01"}
        self.assertEqual(windows.plan(settings, []),
                         [Window(1.0, 1.2, 0.01), Window(19.9, 20.0, 0.01)])


class TestMeshFrequency(unittest.TestCase):
    def test_the_top_of_the_band_by_default(self):
        self.assertEqual(windows.mesh_frequency_ghz(SETTINGS, [(5.0, 100.0)]), 20.0)

    def test_highest_mode_adapts_just_above_the_highest_mode_in_band(self):
        settings = {**SETTINGS, "adapt_at": "highest_mode", "fine_margin_percent": 1.0}
        self.assertAlmostEqual(
            windows.mesh_frequency_ghz(settings, [(5.0, 1.0), (12.0, 1.0), (30.0, 1.0)]), 12.12)

    def test_highest_mode_without_modes_falls_back_to_the_top(self):
        self.assertEqual(
            windows.mesh_frequency_ghz({**SETTINGS, "adapt_at": "highest_mode"}, []), 20.0)


class FakeSetup:
    def __init__(self):
        self.props = {}

    def update(self):
        pass


class FakeDrivenHfss:
    def __init__(self, refuse=()):
        self.sweeps = []
        self.setup = FakeSetup()
        self.refuse = refuse

    def create_setup(self, name):
        return self.setup

    def create_linear_count_sweep(self, **kwargs):
        if kwargs["name"] in self.refuse:
            return False
        self.sweeps.append(kwargs)
        return object()


class TestDrivenSetup(unittest.TestCase):
    def test_with_no_modes_the_setup_is_what_it_was(self):
        hfss = FakeDrivenHfss()
        driven_setup.create_setup(hfss, "", dict(SETTINGS), log=lambda _m: None)
        self.assertEqual([s["name"] for s in hfss.sweeps], ["Sweep_1"])
        self.assertEqual(hfss.setup.props["Frequency"], "20.0GHz")

    def test_each_window_is_its_own_interpolating_sweep_beside_the_coarse_one(self):
        hfss = FakeDrivenHfss()
        settings = {**SETTINGS, "eigenmodes": [(5.0, 150.0), (12.0, 300.0)]}
        driven_setup.create_setup(hfss, "", settings, log=lambda _m: None)
        self.assertEqual([s["name"] for s in hfss.sweeps], ["Sweep_1", "Fine_1", "Fine_2"])
        fine = hfss.sweeps[1]
        self.assertEqual(fine["sweep_type"], "Interpolating")
        self.assertEqual(fine["unit"], "GHz")
        self.assertLess(fine["start_frequency"], 5.0)
        self.assertGreater(fine["stop_frequency"], 5.0)
        self.assertGreater(fine["num_of_freq_points"], 10)

    def test_a_window_hfss_refuses_does_not_lose_the_model(self):
        hfss = FakeDrivenHfss(refuse=("Fine_1",))
        settings = {**SETTINGS, "eigenmodes": [(5.0, 150.0), (12.0, 300.0)]}
        driven_setup.create_setup(hfss, "", settings, log=lambda _m: None)
        self.assertEqual([s["name"] for s in hfss.sweeps], ["Sweep_1", "Fine_2"])

    def test_the_mesh_can_adapt_at_the_highest_mode(self):
        hfss = FakeDrivenHfss()
        settings = {**SETTINGS, "eigenmodes": [(5.0, 150.0), (12.0, 300.0)],
                    "adapt_at": "highest_mode"}
        driven_setup.create_setup(hfss, "", settings, log=lambda _m: None)
        self.assertEqual(hfss.setup.props["Frequency"], f"{12.0 * 1.01}GHz")

    def test_a_bad_manual_window_fails_the_build_rather_than_being_ignored(self):
        with self.assertRaises(ValueError):
            driven_setup.create_setup(
                FakeDrivenHfss(), "", {**SETTINGS, "fine_windows_manual": "oops"}, log=lambda _m: None)


class FakeSetupNamed:
    name = "Setup_1"


class TestReadingTheWindowsBack(unittest.TestCase):
    SWEEPS = ["Setup_1 : LastAdaptive", "Setup_1 : Fine_2", "Setup_1 : Fine_1",
              "Setup_1 : Fine_10", "Setup_1 : Sweep_1"]

    class Hfss:
        def __init__(self, sweeps):
            self.existing_analysis_sweeps = sweeps

    def test_the_coarse_sweep_is_never_a_fine_one(self):
        hfss = self.Hfss(self.SWEEPS)
        self.assertEqual(driven_results._sweep_solution_name(hfss, FakeSetupNamed()),
                         "Setup_1 : Sweep_1")

    def test_fine_solutions_come_back_in_window_order(self):
        self.assertEqual(
            driven_results._fine_solution_names(self.Hfss(self.SWEEPS), FakeSetupNamed()),
            ["Setup_1 : Fine_1", "Setup_1 : Fine_2", "Setup_1 : Fine_10"])

    def test_merging_gives_one_curve_in_frequency_order_with_the_fine_row_winning(self):
        coarse = [{"freq_ghz": f, "s21_db": -1.0} for f in (5.0, 5.1, 5.2)]
        fine = [("Fine_1", [{"freq_ghz": 5.1, "s21_db": -40.0}, {"freq_ghz": 5.11, "s21_db": -41.0}])]
        merged = driven_results.merge_sweeps(coarse, fine)
        self.assertEqual([r["freq_ghz"] for r in merged], [5.0, 5.1, 5.11, 5.2])
        self.assertEqual(merged[1]["s21_db"], -40.0)
        self.assertEqual([r["sweep"] for r in merged], ["coarse", "Fine_1", "Fine_1", "coarse"])


class TestSettingsPlumbing(unittest.TestCase):
    def test_every_fine_window_setting_is_a_driven_field_with_a_default(self):
        names = {f["name"] for f in registry.get("DrivenModal").fields}
        wanted = {"fine_windows", "fine_halfwidth_linewidths", "fine_points_per_linewidth",
                  "fine_margin_percent", "fine_q_fallback", "fine_max_points",
                  "fine_windows_manual", "adapt_at"}
        self.assertLessEqual(wanted, names)
        defaults = registry.all_defaults()
        self.assertEqual(defaults["fine_halfwidth_linewidths"], 0.5)
        self.assertEqual(defaults["fine_points_per_linewidth"], 20.0)
        self.assertEqual(defaults["fine_windows"], "auto")
        self.assertEqual(defaults["adapt_at"], "f_stop")

    def test_text_fields_are_stripped_and_selects_still_validated(self):
        by_name = {f["name"]: f for f in registry.get("DrivenModal").fields}
        self.assertEqual(registry.coerce(by_name["fine_windows_manual"], "  8:9:0.1 "), "8:9:0.1")
        with self.assertRaises(ValueError):
            registry.coerce(by_name["fine_windows"], "sometimes")

    def test_the_defaults_reproduce_the_documented_formula(self):
        d = registry.all_defaults()
        [w] = windows.windows_from_modes(
            [(10.0, 100.0)], 1.0, 20.0, d["fine_halfwidth_linewidths"],
            d["fine_points_per_linewidth"], 0.0)
        self.assertAlmostEqual(w.hi - w.lo, 10.0 / 100.0)      # 2 * f/(2Q)
        self.assertAlmostEqual(w.step, 10.0 / 100.0 / 20.0)    # f/(20Q)


if __name__ == "__main__":
    unittest.main()
