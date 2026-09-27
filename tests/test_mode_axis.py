"""The mode number is offered as an axis and a filter, like a swept
parameter, whenever an eigenmode run solved more than one mode."""
import unittest

import pandas as pd

from microwave_package.workflow import plotting


def _frame(modes):
    return pd.DataFrame([
        {"freq_ghz": 5.0 + m + d, "q": 900, "mode": m, "combo_id": f"c{d}", "pedestal_depth": d}
        for d in (0.0, 0.5) for m in modes
    ])


class ModeAxisTests(unittest.TestCase):
    def test_several_modes_are_an_axis_option(self):
        options = {o["value"]: o["label"] for o in plotting.axis_options(_frame([1, 2, 3]), ["pedestal_depth"])}
        self.assertEqual(options.get("mode"), "Mode")

    def test_several_modes_are_a_filter(self):
        groups = {g["column"]: g for g in plotting.filter_options(_frame([1, 2, 3]), ["pedestal_depth"])}
        self.assertIn("mode", groups)
        self.assertEqual([v["key"] for v in groups["mode"]["values"]], ["1", "2", "3"])

    def test_a_single_mode_is_neither(self):
        df = _frame([1])
        self.assertNotIn("mode", [o["value"] for o in plotting.axis_options(df, ["pedestal_depth"])])
        self.assertNotIn("mode", [g["column"] for g in plotting.filter_options(df, ["pedestal_depth"])])

    def test_filtering_by_mode_keeps_only_those_rows(self):
        df = plotting.apply_filters(_frame([1, 2, 3]), {"mode": ["2"]})
        self.assertEqual(sorted(df["mode"].unique().tolist()), [2])

    def test_mode_can_be_plotted_and_is_labelled(self):
        html = plotting.figure_html(_frame([1, 2]), ["pedestal_depth"], x="mode", y="freq_ghz", color="mode")
        self.assertIn("Mode", html)


if __name__ == "__main__":
    unittest.main()
