"""Tests for the Altium bridge's pure parts.

Launching Altium is not exercised here -- that needs the real
application and belongs in the spikes. What is tested is everything the
sweep depends on being right *before* a launch, and the sentinel parsing
it depends on afterwards.

Run: python -m unittest discover -s tests
"""
import os
import tempfile
import unittest

from eigen_mode_analyze import altium_generation as ag


class TestWriteParams(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "params.txt")

    def test_writes_every_value_as_key_equals_value(self):
        ag.write_params(self.path, {"pcb_length": 26.0, "cpw_trace_width": 0.2})
        got = ag.read_sentinel(self.path)
        self.assertEqual(float(got["pcb_length"]), 26.0)
        self.assertEqual(float(got["cpw_trace_width"]), 0.2)

    def test_round_trips_through_the_reader(self):
        """The writer and reader are two halves of one wire format, so
        they are tested against each other rather than against a fixture."""
        values = {"a": 1.0, "b": 0.0002, "c": 1234.5678}
        ag.write_params(self.path, values)
        got = ag.read_sentinel(self.path)
        for name, value in values.items():
            self.assertAlmostEqual(float(got[name]), value, places=9)

    def test_small_values_survive_without_scientific_notation_loss(self):
        """200 nm is 0.0002 mm. repr keeps full precision; a %f format
        would have silently rounded it to zero."""
        ag.write_params(self.path, {"chip_metal_thickness": 0.0002})
        got = ag.read_sentinel(self.path)
        self.assertEqual(float(got["chip_metal_thickness"]), 0.0002)

    def test_extra_string_entries_are_written(self):
        ag.write_params(self.path, {"x": 1.0}, extra={"out_dir": r"C:\tmp\out"})
        self.assertEqual(ag.read_sentinel(self.path)["out_dir"], r"C:\tmp\out")

    def test_creates_the_directory_if_missing(self):
        nested = os.path.join(self.dir, "a", "b", "params.txt")
        ag.write_params(nested, {"x": 1.0})
        self.assertTrue(os.path.isfile(nested))


class TestReadSentinel(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "done.txt")

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_missing_file_is_empty_not_an_error(self):
        """A script that died before writing anything is a normal
        outcome here, not an exception."""
        self.assertEqual(ag.read_sentinel(os.path.join(self.dir, "nope.txt")), {})

    def test_comments_and_blank_lines_are_ignored(self):
        self._write("# a comment\n\nok=1\nstage=end\n")
        self.assertEqual(ag.read_sentinel(self.path), {"ok": "1", "stage": "end"})

    def test_values_containing_equals_are_kept_whole(self):
        """Windows paths and error messages both contain '='; splitting
        on every one of them would truncate the diagnostic."""
        self._write("error=bad value: a=b\n")
        self.assertEqual(ag.read_sentinel(self.path)["error"], "bad value: a=b")

    def test_partial_report_is_still_readable(self):
        """The whole point of flushing progress before each risky call:
        a truncated file still says how far the script got."""
        self._write("stage=start\nopen=ok\ntrying=export\n")
        got = ag.read_sentinel(self.path)
        self.assertEqual(got["trying"], "export")
        self.assertNotIn("stage_end", got)
        self.assertNotEqual(got.get("stage"), "end")


class TestAltiumDiscovery(unittest.TestCase):
    def test_find_altium_exe_returns_an_existing_file_or_none(self):
        exe = ag.find_altium_exe()
        if exe is not None:
            self.assertTrue(os.path.isfile(exe))
            self.assertTrue(exe.upper().endswith("X2.EXE"))

    def test_run_script_rejects_a_missing_script_before_launching(self):
        """Cheap guard: a typo in a script path must not cost a 7 minute
        timeout waiting for a sentinel that can never appear."""
        if ag.find_altium_exe() is None:
            self.skipTest("Altium not installed on this machine")
        with self.assertRaises(FileNotFoundError):
            ag.run_script(
                os.path.join(self.dir if hasattr(self, "dir") else tempfile.mkdtemp(), "nope.pas"),
                "Whatever",
                os.path.join(tempfile.mkdtemp(), "sentinel.txt"),
            )


if __name__ == "__main__":
    unittest.main()
