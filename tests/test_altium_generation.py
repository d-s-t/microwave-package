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

from microwave_package.design import altium_generation as ag


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


class TestNotHoldingTheReportOpen(unittest.TestCase):
    """The script rewrites its report on every progress line, and Delphi
    takes the file exclusively to do it -- so a handle held on this side
    does not slow the script down, it fails its write, and a failed write
    inside Altium is a modal dialog waiting for a human. These pin the
    two halves of avoiding that: a file that is still being written is
    not opened at all, and one that cannot be read is not an error.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "done.txt")

    def _write(self, *lines):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("".join(f"{line}\n" for line in lines))

    def test_a_file_that_cannot_be_read_is_empty_not_an_error(self):
        """Same answer as a missing one: the caller polls, so the next
        pass gets it -- there is nothing here worth raising over."""
        os.mkdir(os.path.join(self.dir, "a_directory.txt"))
        self.assertEqual(ag.read_sentinel(os.path.join(self.dir, "a_directory.txt")), {})

    def test_a_missing_file_has_no_fingerprint(self):
        self.assertIsNone(ag._fingerprint(self.path))

    def test_a_file_that_has_not_changed_fingerprints_the_same(self):
        self._write("stage=start")
        self.assertEqual(ag._fingerprint(self.path), ag._fingerprint(self.path))

    def test_a_file_that_has_grown_fingerprints_differently(self):
        """How "the script is still writing" is known without opening
        the file the script is writing."""
        self._write("stage=start")
        before = ag._fingerprint(self.path)
        self._write("stage=start", "open=ok")
        self.assertNotEqual(ag._fingerprint(self.path), before)

    def test_clearing_removes_the_previous_report(self):
        self._write("stage=end")
        self.assertTrue(ag._clear_sentinel(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_clearing_a_report_that_is_not_there_is_fine(self):
        self.assertTrue(ag._clear_sentinel(self.path))

    def test_clearing_gives_up_rather_than_waiting_forever(self):
        """A report that will not go away means the last script still has
        it open -- almost always because Altium is sitting on a dialog.
        Waiting for that forever helps nobody."""
        undeletable = os.path.join(self.dir, "a_directory_here_too")
        os.mkdir(undeletable)
        with open(os.path.join(undeletable, "keeps_it_alive.txt"), "w") as f:
            f.write("x")
        self.assertFalse(ag._clear_sentinel(undeletable, timeout=0.5))


class TestAnIncompleteBoardIsAFailure(unittest.TestCase):
    """The generator reaches its end whatever happens -- every feature is
    guarded separately so one failure does not take the rest of the board
    with it. Reading only "did the script finish" therefore accepts a
    board with pieces missing.

    From a real run: a parameter file written by a stale process named no
    connector library, the generator dutifully said so and built a board
    with no launch on it, and the sweep carried that board forward. It
    surfaced two stages later as a parse error about pad records, which
    is about as far from "there is no connector" as a symptom gets.
    """

    GOOD = {
        "package_body": "ok", "chip_body": "ok", "package_cavity": "ok",
        "package_ports": "2", "package_bond_pads": "54",
        "chip_bond_pads": "54", "cavity_kind_readback": "board_cutout",
    }

    def test_a_complete_board_has_nothing_to_report(self):
        self.assertEqual(ag._missing_features(self.GOOD), "")

    def test_a_board_with_no_connector_is_caught(self):
        report = dict(self.GOOD, package_ports="NO_LAUNCH_PLACED",
                      smp_library="NOT_CONFIGURED")
        problem = ag._missing_features(report)
        self.assertIn("no connector", problem)
        self.assertIn("NOT_CONFIGURED", problem,
                      "the reason has to travel with the complaint")

    def test_a_missing_footprint_names_itself(self):
        report = dict(self.GOOD, package_ports="NO_LAUNCH_PLACED",
                      smp_footprint="NOT_IN_LIBRARY")
        self.assertIn("NOT_IN_LIBRARY", ag._missing_features(report))

    def test_a_failed_feature_is_caught(self):
        for key in ("package_body", "chip_body", "package_cavity"):
            with self.subTest(key=key):
                self.assertNotEqual(
                    ag._missing_features(dict(self.GOOD, **{key: "FAIL"})), ""
                )

    def test_a_feature_that_built_nothing_is_caught(self):
        """Zero bond pads means bonds.txt never reached the script. The
        board looks fine and the die is connected to nothing."""
        self.assertIn("bond", ag._missing_features(dict(self.GOOD, package_bond_pads="0")))

    def test_a_feature_never_reported_on_is_caught(self):
        """An older generator, or one that died before reaching a
        feature -- either way nobody can say the board has it."""
        report = dict(self.GOOD)
        del report["package_cavity"]
        self.assertIn("never reported on", ag._missing_features(report))

    def test_a_cavity_that_is_not_really_a_cutout_is_caught(self):
        """The Kind assignment not taking leaves a plain copper region
        sitting on the keep-out layer, which looks like a cutout in the
        tree and is not one."""
        self.assertIn(
            "opening", ag._missing_features(dict(self.GOOD, cavity_kind_readback="WRONG"))
        )

    def test_every_complaint_says_what_is_missing(self):
        """A reason the reader cannot act on is barely better than none."""
        for override in ({"package_ports": "NO_LAUNCH_PLACED"},
                         {"package_body": "FAIL"},
                         {"chip_bond_pads": "0"},
                         {"cavity_kind_readback": "WRONG"}):
            problem = ag._missing_features(dict(self.GOOD, **override))
            self.assertTrue(problem and len(problem.split()) > 2, problem)


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
