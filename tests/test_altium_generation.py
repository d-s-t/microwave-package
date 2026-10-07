"""Tests for the Altium bridge's pure parts.

Launching Altium is not exercised here -- that needs the real
application and belongs in the spikes. What is tested is everything the
sweep depends on being right *before* a launch, and the sentinel parsing
it depends on afterwards.

Run: python -m unittest discover -s tests
"""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from microwave_package import paths
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


class TestScriptsRunFromAnyCheckout(unittest.TestCase):
    """The scripts committed one machine's folder once, and every other
    checkout's first write became a modal "Cannot create file" dialog."""

    SCRIPTS = (
        paths.ALTIUM_GENERATOR, paths.ALTIUM_COPPER_EXPORTER,
        paths.ALTIUM_FOOTPRINT_MEASURER, paths.ALTIUM_PING,
        paths.ALTIUM_DIR / "VerifyGenerated.pas",
    )

    def setUp(self):
        patcher = mock.patch.object(paths, "ALTIUM_RESOLVED_DIR",
                                    paths.Path(tempfile.mkdtemp()) / "resolved")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_script_names_a_folder_of_its_own(self):
        for script in self.SCRIPTS:
            with self.subTest(script=script.name):
                self.assertNotIn(b":\\", script.read_bytes())

    def test_what_altium_runs_names_this_checkout(self):
        for script in self.SCRIPTS:
            with self.subTest(script=script.name):
                with open(ag.resolve_script(str(script)), "rb") as f:
                    text = f.read()
                self.assertNotIn(b"<ALTIUM_DIR>", text)
                if script != paths.ALTIUM_PING:
                    self.assertIn(str(paths.ALTIUM_DIR).encode("mbcs"), text)

    def test_the_source_itself_is_left_alone(self):
        before = paths.ALTIUM_GENERATOR.read_bytes()
        ag.resolve_script(str(paths.ALTIUM_GENERATOR))
        self.assertEqual(paths.ALTIUM_GENERATOR.read_bytes(), before)

    def test_an_unchanged_copy_is_not_rewritten(self):
        first = ag.resolve_script(str(paths.ALTIUM_GENERATOR))
        os.utime(first, (0, 0))
        ag.resolve_script(str(paths.ALTIUM_GENERATOR))
        self.assertEqual(os.stat(first).st_mtime, 0)


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

    @unittest.skipUnless(os.name == "nt", "the copy is a Windows call")
    def test_the_report_itself_is_never_opened_only_a_copy(self):
        """An open handle on the report is what fails the script's write,
        and a thread can be held up with one open for as long as an AEDT
        call lasts -- PyAEDT keeps Python's lock through each one. So the
        report is copied by a single Windows call and only the copy is
        opened here."""
        self._write("stage=end", "ok=1")
        real_open = open
        report = os.path.normcase(os.path.abspath(self.path))

        def guarded_open(file, *args, **kwargs):
            if os.path.normcase(os.path.abspath(os.fspath(file))) == report:
                raise AssertionError("the report was opened in place")
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", guarded_open):
            self.assertEqual(ag.read_sentinel(self.path), {"stage": "end", "ok": "1"})

    @unittest.skipUnless(os.name == "nt", "the copy is a Windows call")
    def test_trouble_with_the_copy_reads_as_missing_not_an_error(self):
        """No temp space, or a scanner holding the fresh copy: the run
        polls again rather than losing its Altium stage to an exception."""
        self._write("stage=end", "ok=1")
        with mock.patch("tempfile.mkstemp", side_effect=OSError("no space")):
            self.assertEqual(ag.read_sentinel(self.path), {})
        real_open = open

        def refusing_open(file, *args, **kwargs):
            if "altium_report_" in os.path.basename(os.fspath(file)):
                raise PermissionError("held by a scanner")
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", refusing_open):
            self.assertEqual(ag.read_sentinel(self.path), {})
        self.assertEqual(ag.read_sentinel(self.path), {"stage": "end", "ok": "1"})

    @unittest.skipUnless(os.name == "nt", "share modes are Windows'")
    def test_a_report_the_script_is_writing_reads_as_missing(self):
        """What Altium holds while it writes a progress line: the file,
        open with no sharing at all. Reading then must come back empty
        rather than fail -- and must not leave anything open behind it."""
        import ctypes
        from ctypes import wintypes

        self._write("stage=start")
        create = ctypes.windll.kernel32.CreateFileW
        create.restype = wintypes.HANDLE
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        GENERIC_WRITE, OPEN_EXISTING = 0x40000000, 3
        held = create(self.path, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
        self.assertNotEqual(held, wintypes.HANDLE(-1).value)
        try:
            self.assertEqual(ag.read_sentinel(self.path), {})
        finally:
            ctypes.windll.kernel32.CloseHandle(held)
        self.assertEqual(ag.read_sentinel(self.path), {"stage": "start"})
        os.remove(self.path)  # nothing of ours still has it open

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


class TestAScriptGivenUpOnDoesNotPoisonTheNext(unittest.TestCase):
    """Altium carries on with a script this side has given up on, and runs
    the next one launched nested inside it as soon as it lets a message
    through -- both drawing into the same files, while the second call
    reads the first one's report as its own.

    Launchers are stood in for by Python processes: one that exits at
    once is Altium taking the request, one that sleeps is Altium busy.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sentinel = os.path.join(self.dir, "report.txt")
        self.script = os.path.join(self.dir, "Real.pas")
        with open(self.script, "w") as f:
            f.write("{ stands in for a real script }")
        self.launched = []
        self.messages = []
        # run_script waits on every report of ours, not just its own; the
        # real ones belong to whatever Altium last did on this machine.
        for name in ("ALTIUM_GENERATE_REPORT", "ALTIUM_COPPER_REPORT", "ALTIUM_MEASURE_REPORT"):
            patcher = mock.patch.object(paths, name, os.path.join(self.dir, name.lower() + ".txt"))
            patcher.start()
            self.addCleanup(patcher.stop)
        # An Altium that has been up since long before any report here.
        patcher = mock.patch.object(ag, "_altium_processes", return_value={1: 0.0})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _report(self, *lines, age):
        """A report last written `age` seconds ago."""
        with open(self.sentinel, "w") as f:
            f.write("".join(f"{line}\n" for line in lines))
        then = time.time() - age
        os.utime(self.sentinel, (then, then))

    def tearDown(self):
        for proc in self.launched:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def _spawn(self, busy):
        code = "import time; time.sleep(60)" if busy else "pass"
        proc = subprocess.Popen([sys.executable, "-c", code])
        self.launched.append(proc)
        return proc

    def _fake_launch(self, busy_for):
        """A `_launch` whose launcher hangs while `busy_for(procedure, n)`
        says so -- n counting that procedure's launches from 1."""
        calls = []

        def launch(exe, script_path, procedure):
            calls.append(procedure)
            return self._spawn(busy_for(procedure, calls.count(procedure)))

        return launch, calls

    def test_a_free_altium_answers_the_first_probe(self):
        launch, calls = self._fake_launch(lambda proc, n: False)
        with mock.patch.object(ag, "_launch", launch):
            self.assertIsNone(ag._wait_until_idle("X2.EXE", [], 5.0, self.messages.append))
        self.assertEqual(calls, ["Ping"])

    def test_a_busy_altium_is_given_up_on_and_every_probe_withdrawn(self):
        """A dialog nobody closes: say so, and leave nothing queued behind
        it -- a probe left waiting would have been one more request to
        land later, on top of whatever the next job is doing."""
        launch, calls = self._fake_launch(lambda proc, n: True)
        with mock.patch.object(ag, "_launch", launch), \
                mock.patch.object(ag, "_is_launcher_stub", return_value=True), \
                mock.patch.object(ag, "PROBE_RETRY", 0.3):
            reason = ag._wait_until_idle("X2.EXE", [], 1.0, self.messages.append)
        self.assertIn("dialog", reason)
        self.assertIn("Nothing was launched", reason)
        self.assertGreater(len(calls), 1, "the question should have been asked again")
        self.assertTrue(all(p.poll() is not None for p in self.launched),
                        "a probe was left waiting in Altium's queue")

    def test_a_stuck_probe_is_replaced_rather_than_waited_out(self):
        """Seen on AD26: a launcher already waiting when a dialog was
        closed stayed stuck a minute and a half after it had gone, while
        a fresh one got through at once."""
        launch, calls = self._fake_launch(lambda proc, n: n == 1)
        with mock.patch.object(ag, "_launch", launch), \
                mock.patch.object(ag, "_is_launcher_stub", return_value=True), \
                mock.patch.object(ag, "PROBE_RETRY", 0.3):
            self.assertIsNone(ag._wait_until_idle("X2.EXE", [], 30.0, self.messages.append))
        self.assertEqual(calls, ["Ping", "Ping"])
        self.assertIsNotNone(self.launched[0].poll(), "the stuck probe was not withdrawn")

    def test_an_unfinished_report_holds_the_launch_back(self):
        """The ping alone cannot see this: watched on AD26, a script
        launched behind a running generator was taken the moment the
        generator closed its package board, and ran nested inside it."""
        self._report("stage=start", "package_body=ok", age=5)
        launch, calls = self._fake_launch(lambda proc, n: False)
        with mock.patch.object(ag, "_launch", launch), \
                mock.patch.object(ag, "POLL_SECONDS", 0.1):
            reason = ag._wait_until_idle("X2.EXE", [self.sentinel], 0.5, self.messages.append)
        self.assertIn("report.txt", reason, "the reason should say which script")
        self.assertIn("Nothing was launched", reason)
        self.assertEqual(calls, [], "not even the ping: Altium would have taken it, nested")

    def test_the_launch_goes_ahead_once_the_report_ends(self):
        self._report("stage=start", age=5)
        finish = threading.Timer(0.4, lambda: self._report("stage=start", "stage=end", age=5))
        finish.start()
        self.addCleanup(finish.cancel)
        launch, calls = self._fake_launch(lambda proc, n: False)
        with mock.patch.object(ag, "_launch", launch), \
                mock.patch.object(ag, "POLL_SECONDS", 0.1):
            self.assertIsNone(ag._wait_until_idle("X2.EXE", [self.sentinel], 10.0,
                                                  self.messages.append))
        self.assertEqual(calls, ["Ping"])
        self.assertTrue(any("still running" in m for m in self.messages),
                        "a wait this long should say what it is waiting for")

    def test_a_report_being_written_is_not_opened(self):
        """Delphi's SaveToFile takes the file exclusively, and a write that
        fails inside Altium is a modal dialog -- see run_script."""
        self._report("stage=start", age=0)
        with mock.patch.object(ag, "read_sentinel", side_effect=AssertionError("opened")):
            self.assertTrue(ag._still_running(self.sentinel))

    def test_a_finished_report_is_not_waited_for(self):
        self._report("stage=start", "ok=1", "stage=end", age=5)
        self.assertFalse(ag._still_running(self.sentinel))

    def test_a_missing_report_is_not_waited_for(self):
        self.assertFalse(ag._still_running(self.sentinel))

    def test_a_report_from_an_altium_since_closed_is_not_waited_for(self):
        self._report("stage=start", age=30)
        restarted = {1: time.time() - 10, 2: time.time() - 5}
        with mock.patch.object(ag, "_altium_processes", return_value=restarted):
            self.assertFalse(ag._still_running(self.sentinel))
        with mock.patch.object(ag, "_altium_processes", return_value={}):
            self.assertFalse(ag._still_running(self.sentinel), "no Altium, nothing running")

    def test_an_altium_whose_start_cannot_be_read_is_assumed_busy(self):
        """psutil reports a start time it cannot read as None. Guessing
        "not running" there is the guess that launches into a live script."""
        self._report("stage=start", age=30)
        with mock.patch.object(ag, "_altium_processes", return_value={1: None}):
            self.assertTrue(ag._still_running(self.sentinel))

    def test_a_report_nobody_has_written_for_the_whole_budget_is_not_waited_for(self):
        """A script stopped by hand at a dialog never writes its end. Without
        this, every launch after it would wait and fail for good."""
        self._report("stage=start", age=ag.BUSY_TIMEOUT + 5)
        self.assertFalse(ag._still_running(self.sentinel))
        self._report("stage=start", age=10)
        self.assertTrue(ag._still_running(self.sentinel))

    def test_nothing_is_launched_while_an_earlier_script_is_still_going(self):
        """The failure this exists for: a new script run nested inside one
        still going, and the old one's report read as the new one's. The
        report is left alone too -- clearing it is what let the old
        script's next line pass for the new script's first."""
        # Still writing its report, the way a live script does.
        stop = threading.Event()

        def live():
            while not stop.is_set():
                self._report("stage=start", "package_body=ok", age=0)
                stop.wait(0.05)

        writer = threading.Thread(target=live)
        writer.start()
        self.addCleanup(writer.join)
        self.addCleanup(stop.set)
        launch, calls = self._fake_launch(lambda proc, n: False)
        with mock.patch.object(ag, "_launch", launch), \
                mock.patch.object(ag, "find_altium_exe", return_value="X2.EXE"), \
                mock.patch.object(ag, "altium_is_running", return_value=True), \
                mock.patch.object(ag, "BUSY_TIMEOUT", 0.5), \
                mock.patch.object(ag, "POLL_SECONDS", 0.1):
            ok, report = ag.run_script(self.script, "Real", self.sentinel, log=self.messages.append)
        self.assertFalse(ok)
        self.assertIn("still running", report["error"])
        self.assertEqual(calls, [])
        with open(self.sentinel) as f:
            self.assertIn("package_body=ok", f.read())

    def test_nothing_is_launched_while_altium_is_on_a_dialog(self):
        launch, calls = self._fake_launch(lambda proc, n: True)
        with mock.patch.object(ag, "_launch", launch), \
                mock.patch.object(ag, "find_altium_exe", return_value="X2.EXE"), \
                mock.patch.object(ag, "altium_is_running", return_value=True), \
                mock.patch.object(ag, "_is_launcher_stub", return_value=True), \
                mock.patch.object(ag, "BUSY_TIMEOUT", 0.5), \
                mock.patch.object(ag, "PROBE_RETRY", 0.2):
            ok, report = ag.run_script(self.script, "Real", self.sentinel, log=self.messages.append)
        self.assertFalse(ok)
        self.assertIn("dialog", report["error"])
        self.assertNotIn("Real", calls)

    def test_a_script_altium_never_started_is_withdrawn_at_the_timeout(self):
        """Something else took Altium between the probe and the launch.
        Left queued, it would run later against the next job's files."""
        launch, calls = self._fake_launch(lambda proc, n: proc == "Real")
        with mock.patch.object(ag, "_launch", launch), \
                mock.patch.object(ag, "find_altium_exe", return_value="X2.EXE"), \
                mock.patch.object(ag, "altium_is_running", return_value=True), \
                mock.patch.object(ag, "_is_launcher_stub", return_value=True), \
                mock.patch.object(ag, "POLL_SECONDS", 0.05):
            ok, report = ag.run_script(self.script, "Real", self.sentinel, timeout=0.5,
                                       log=self.messages.append)
        self.assertFalse(ok)
        self.assertIn("never started", report["error"])
        self.assertIsNotNone(self.launched[-1].poll(), "the queued request was left in place")

    def test_something_that_is_not_x2_is_never_a_stub(self):
        other = self._spawn(busy=True)
        self.assertFalse(ag._is_launcher_stub(other))
        self.assertFalse(ag._withdraw(other))
        self.assertIsNone(other.poll())

    def test_a_stub_is_an_x2_with_an_older_x2_to_forward_to(self):
        """On a cold start the process launched IS Altium. Killing it at
        a timeout would take the operator's open documents with it."""
        launcher = mock.Mock(pid=20)
        for processes, stub in (
            ({10: 100.0, 20: 200.0}, True),     # Altium, then this stub
            ({20: 200.0}, False),               # this one is Altium
            ({20: 200.0, 30: 300.0}, False),    # only a younger stub about
            ({10: None, 20: 200.0}, False),     # an age that proves nothing
            ({10: 100.0, 20: None}, False),     # nor does its own
            ({10: 100.0}, False),               # not an X2.EXE at all
        ):
            with self.subTest(processes=processes), \
                    mock.patch.object(ag, "_altium_processes", return_value=processes):
                self.assertIs(ag._is_launcher_stub(launcher), stub)

    def test_a_timed_out_cold_start_leaves_altium_running(self):
        """Even if it looks like a stub: an X2.EXE left over from a session
        since closed is older than the Altium this launch just started."""
        launch, calls = self._fake_launch(lambda proc, n: True)
        with mock.patch.object(ag, "_launch", launch), \
                mock.patch.object(ag, "find_altium_exe", return_value="X2.EXE"), \
                mock.patch.object(ag, "altium_is_running", return_value=False), \
                mock.patch.object(ag, "_is_launcher_stub", return_value=True), \
                mock.patch.object(ag, "POLL_SECONDS", 0.05):
            ok, report = ag.run_script(self.script, "Real", self.sentinel, timeout=0.3,
                                       log=self.messages.append)
        self.assertFalse(ok)
        self.assertEqual(calls, ["Real"], "a cold start has nothing to wait for")
        self.assertIsNone(self.launched[0].poll())

    def test_a_missing_ping_is_refused_before_anything_is_launched(self):
        """Asking Altium to run a file that is not there is itself a dialog."""
        launch, calls = self._fake_launch(lambda proc, n: False)
        with mock.patch.object(ag, "_launch", launch), \
                mock.patch.object(ag, "find_altium_exe", return_value="X2.EXE"), \
                mock.patch.object(ag, "altium_is_running", return_value=True), \
                mock.patch.object(paths, "ALTIUM_PING", os.path.join(self.dir, "nope.pas")):
            with self.assertRaises(FileNotFoundError):
                ag.run_script(self.script, "Real", self.sentinel, log=self.messages.append)
        self.assertEqual(calls, [])

    def test_a_dialog_after_a_report_wait_is_still_named(self):
        """The two halves each say what they are waiting for, so a dialog
        found after a long wait on a report does not hide behind it."""
        self._report("stage=start", age=5)
        threading.Timer(0.3, lambda: self._report("stage=start", "stage=end", age=5)).start()
        launch, calls = self._fake_launch(lambda proc, n: True)
        with mock.patch.object(ag, "_launch", launch), \
                mock.patch.object(ag, "_is_launcher_stub", return_value=True), \
                mock.patch.object(ag, "POLL_SECONDS", 0.1), \
                mock.patch.object(ag, "PROBE_RETRY", 0.3), \
                mock.patch.object(ag, "PING_PATIENCE", 0.1):
            reason = ag._wait_until_idle("X2.EXE", [self.sentinel], 1.0, self.messages.append)
        self.assertIn("dialog", reason)
        self.assertTrue(any("still running" in m for m in self.messages))
        self.assertTrue(any("not taking scripts" in m for m in self.messages))


class TestOneAltiumJobAtATime(unittest.TestCase):
    """Every run's pipeline is a thread in the same web app. Two drawing
    layouts at once share params.txt: the second's parameters land while
    the first's script has yet to read them."""

    def test_a_second_job_waits_for_the_first_and_says_so(self):
        holding, release = threading.Event(), threading.Event()
        order, said = [], []

        def first():
            with ag._one_at_a_time(lambda m: None):
                order.append("first in")
                holding.set()
                release.wait(5)
                order.append("first out")

        def second():
            holding.wait(5)
            with ag._one_at_a_time(said.append):
                order.append("second in")

        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        for t in threads:
            t.start()
        # The second says it is waiting just before it blocks.
        deadline = time.monotonic() + 5
        while not said and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(order, ["first in"], "the second job did not wait")
        release.set()
        for t in threads:
            t.join(5)
        self.assertEqual(order, ["first in", "first out", "second in"])
        self.assertTrue(said and "waiting" in said[0])

    def test_a_job_can_run_its_own_scripts(self):
        """generate_for_combo holds it across run_script, which takes it
        too -- that must not deadlock."""
        with ag._one_at_a_time(lambda m: None):
            with ag._one_at_a_time(lambda m: self.fail("waited on itself")):
                pass


if __name__ == "__main__":
    unittest.main()
