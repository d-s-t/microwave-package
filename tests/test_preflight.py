"""The preflight: the automatic pre-manufacturing checks and the list of the
ones only a simulation or a person can do."""
import os
import tempfile
import unittest

from microwave_package import preflight
from microwave_package.preflight import FAIL, MANUAL, PASS, WARN

DUROID = "Rogers RT/duroid 5880 (tm)"


def by_id(checks):
    return {c.id: c for c in checks}


class TestAutomaticChecks(unittest.TestCase):
    def test_the_shipped_defaults_pass_every_check_that_can_fail(self):
        checks = preflight.run(preflight._values())
        self.assertEqual([c.id for c in checks if c.status == FAIL], [])

    def test_the_candidate_passes_with_the_one_warning_the_user_should_read(self):
        values = preflight._values(preflight.CANDIDATE_VALUES)
        checks = by_id(preflight.run(values, DUROID))
        self.assertEqual([i for i, c in checks.items() if c.status == FAIL], [])
        self.assertEqual(checks["cryo.cte"].status, WARN)          # 5880's 237 ppm/degC z expansion
        self.assertIn("237", checks["cryo.cte"].detail)
        self.assertEqual(checks["cpw.gap"].status, PASS)
        self.assertIn("129 um", checks["cpw.gap"].detail)

    def test_the_default_trace_on_5880_fails_the_layout_check_with_the_reason(self):
        values = preflight._values({"route_width": 1.0, "route_clearence": 0.2})
        checks = by_id(preflight.run(values, DUROID))
        self.assertEqual(checks["layout.build"].status, FAIL)
        self.assertIn("100 um minimum", checks["layout.build"].detail)

    def test_a_lid_channel_too_narrow_for_the_cpw_fails_in_the_gap_and_the_layout(self):
        values = preflight._values({**preflight.CANDIDATE_VALUES, "route_width": 0.7})
        checks = by_id(preflight.run(values, DUROID))
        self.assertEqual(checks["cpw.gap"].status, FAIL)
        self.assertEqual(checks["layout.build"].status, FAIL)
        self.assertIn("route_width", checks["layout.build"].detail)

    def test_a_lid_too_shallow_to_reach_50_ohm_fails_and_says_how_deep(self):
        values = preflight._values({**preflight.CANDIDATE_VALUES, "route_clearence": 0.04})
        checks = by_id(preflight.run(values, DUROID))
        self.assertEqual(checks["layout.build"].status, FAIL)
        self.assertIn("route_clearence", checks["layout.build"].detail)

    def test_the_dies_own_trace_is_what_the_chip_line_is_solved_for(self):
        wide = preflight._values({**preflight.CANDIDATE_VALUES, "chip_trace_width": 0.4})
        self.assertEqual(by_id(preflight.run(wide, DUROID))["cpw.chip_gap"].status, FAIL)   # no 50 ohm CPW on 0.381 mm silicon
        narrow = preflight._values({**preflight.CANDIDATE_VALUES, "chip_trace_width": 0.2})
        self.assertEqual(by_id(preflight.run(narrow, DUROID))["cpw.chip_gap"].status, PASS)

    def test_fab_limits_are_the_fabs_to_change(self):
        values = preflight._values()
        strict = by_id(preflight.run(values, fab={"min_annular_ring": 0.1}))
        self.assertEqual(strict["fab.ring"].status, WARN)               # 75 um left by the vias
        lenient = by_id(preflight.run(values))
        self.assertEqual(lenient["fab.ring"].status, PASS)

    def test_a_trace_under_the_fabs_minimum_fails(self):
        checks = by_id(preflight.run(preflight._values({"cpw_trace_width": 0.08}), fab={"min_trace": 0.1}))
        self.assertEqual(checks["fab.trace"].status, FAIL)

    def test_the_screw_rules_are_checked(self):
        thin_lid = by_id(preflight.run(preflight._values({"lid_width": 1.0})))
        self.assertEqual(thin_lid["mech.countersink"].status, FAIL)
        shallow = by_id(preflight.run(preflight._values({"screw_engagement": 2.0})))
        self.assertEqual(shallow["mech.thread"].status, WARN)

    def test_no_screws_means_no_screw_checks(self):
        checks = by_id(preflight.run(preflight._values({"screws_on": 0.0})))
        self.assertNotIn("mech.countersink", checks)

    def test_a_copper_matched_laminate_has_no_cte_warning(self):
        checks = by_id(preflight.run(preflight._values(), "Rogers RO3003 (tm)"))
        self.assertEqual(checks["cryo.cte"].status, PASS)

    def test_a_chip_line_is_only_looked_at_when_there_is_a_chip(self):
        bare = by_id(preflight.run(preflight._values({"no_chip": 1.0})))
        self.assertNotIn("cpw.chip_gap", bare)
        self.assertIn("cpw.chip_gap", by_id(preflight.run(preflight._values())))

    def test_a_check_that_blows_up_is_reported_not_fatal(self):
        checks = preflight._safely("x", "G", "title", lambda: 1 / 0)
        self.assertEqual((checks[0].status, checks[0].id), (WARN, "x"))
        self.assertIn("could not be evaluated", checks[0].detail)


class TestWhatTheToolCannotDo(unittest.TestCase):
    def test_every_manual_item_says_how_to_do_it_and_the_list_is_complete(self):
        manual = [c for c in preflight.run(preflight._values()) if c.status == MANUAL]
        ids = {c.id for c in manual}
        for needed in ("sim.line", "sim.driven", "sim.eigen", "sim.finite", "sim.fine", "sim.tolerance",
                       "dfm.smp", "dfm.finish", "dfm.vias", "dfm.fits", "cryo.cycle", "meas.rt", "meas.cold"):
            self.assertIn(needed, ids)
        for c in manual:
            self.assertGreater(len(c.detail), 40, c.id)
            self.assertFalse(c.automatic)


class TestTheReport(unittest.TestCase):
    def test_it_names_the_material_the_changed_values_and_the_fab_limits(self):
        values = preflight._values(preflight.CANDIDATE_VALUES)
        text = preflight.render_markdown(preflight.run(values, DUROID), values, DUROID)
        self.assertIn(DUROID, text)
        self.assertIn("cpw_trace_width = 0.45", text)
        self.assertIn("## Fabricator limits used", text)
        self.assertIn("## Simulation", text)

    def test_the_command_line_writes_the_report_and_its_exit_code_follows_the_failures(self):
        out = os.path.join(tempfile.mkdtemp(), "report.md")
        self.assertEqual(preflight.main(["--candidate", "--out", out]), 0)
        self.assertIn("the candidate board", open(out, encoding="utf-8").read())
        self.assertEqual(preflight.main(["--set", "cpw_trace_width=0.08", "--mat-pcb", DUROID]), 1)


class TestSettingOverrides(unittest.TestCase):
    """`--setting NAME=VALUE` on the pipeline command line (how the line check is aimed)."""

    def test_values_take_the_type_of_the_default_they_replace(self):
        from microwave_package import pipeline
        defaults = pipeline.registry.all_defaults()
        out = pipeline.apply_setting_overrides(
            dict(defaults), ["which_line=chip", "line_tolerance_ohm=1.5", "line_points=21"])
        self.assertEqual((out["which_line"], out["line_tolerance_ohm"], out["line_points"]),
                         ("chip", 1.5, 21))

    def test_an_unknown_name_or_a_bad_number_stops_the_command(self):
        from microwave_package import pipeline
        with self.assertRaises(SystemExit):
            pipeline.apply_setting_overrides({}, ["no_such_setting=1"])
        with self.assertRaises(SystemExit):
            pipeline.apply_setting_overrides({}, ["line_tolerance_ohm=abc"])
        with self.assertRaises(SystemExit):
            pipeline.apply_setting_overrides({}, ["line_tolerance_ohm"])


if __name__ == "__main__":
    unittest.main()
