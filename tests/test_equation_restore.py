"""The shared equations file is restored, locked and checked around a build.

A run's SolidWorks stage writes each combination's values into the shared
`base_parts/default.txt` and must put the file back afterwards. Before this,
a stage that stopped without restoring (or two overlapping builds) left the
file polluted, the next build took the polluted file as its baseline, and
chip runs were built as bare boards. These tests drive Session with fake
SolidWorks calls on a temporary equations file -- nothing opens SolidWorks.

A run that draws its own model (LaunchStudy, LineImpedance) never opens a
session, so it reads the baseline itself: the tests at the end of this file
check that a committed change to default.txt reaches those runs too.

Run: python -m unittest discover -s tests
"""
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from microwave_package import paths, pipeline
from microwave_package.design import equations as eq
from microwave_package.design import step_generation as sg
from microwave_package.design.sweep import SweepParameter
from microwave_package.hfss.launch import LAUNCH_STUDY
from microwave_package.hfss.line import LINE_IMPEDANCE
from microwave_package.workflow import stages
from microwave_package.workflow.run_manager import Run

COMMITTED = (
    '"no_chip"= 0\n'
    '"screws_on"= 1\n'
    '"vents_on"= 1\n'
    '"pedestal_depth"= 0.2\n'
    '"pedestal_shape"= 0\n'
)
BARE_POLLUTED = COMMITTED.replace('"no_chip"= 0', '"no_chip"= 1')
# The file after a later commit adds a parameter the recorded baseline lacks.
NEWER = COMMITTED + '"launch_taper_length"= 0.5\n'


def setUpModule():
    logging.disable(logging.INFO)


def tearDownModule():
    logging.disable(logging.NOTSET)


class FakeModel:
    def __init__(self):
        self.rebuilds = 0


def fake_sw(build_error=None):
    """Patches that make Session.open/build/close run without SolidWorks."""
    def save(model, step_path, options=1):
        if build_error:
            raise build_error
        return True, None

    return [
        mock.patch.object(sg, "connect_solidworks", return_value=(object(), None)),
        mock.patch.object(sg, "open_and_activate_part", side_effect=lambda app, p: (FakeModel(), None)),
        mock.patch.object(sg, "link_equations", return_value=False),
        mock.patch.object(sg, "rebuild_model", return_value=None),
        mock.patch.object(sg, "save_as_step", side_effect=save),
    ]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, "default.txt")
        eq.write_file(self.path, COMMITTED)
        self.combo_dir = os.path.join(self.dir, "combo")
        patches = fake_sw()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def open(self, timeout_s=0.0):
        return sg.Session.open(self.path, {"box": "box.SLDPRT"}, log=lambda _m: None,
                               lock_timeout_s=timeout_s)

    def dead_pid(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def leave_lock_held_by(self, pid):
        """A lock whose holder is `pid` on this host."""
        import json
        import socket
        import psutil
        record = {"pid": pid, "host": socket.gethostname(), "at": 0.0, "started": 0.0}
        if pid == os.getpid():
            record["started"] = psutil.Process(pid).create_time()
        with open(sg.lock_path(self.path), "w", encoding="utf-8") as f:
            json.dump(record, f)


class TestRestore(Base):
    def test_close_puts_the_committed_file_back_and_records_it(self):
        session = self.open()
        session.build("c1", BARE_POLLUTED, self.combo_dir, log=lambda _m: None)
        self.assertEqual(eq.read_file(self.path), BARE_POLLUTED)
        session.close(log=lambda _m: None)
        self.assertEqual(eq.read_file(self.path), COMMITTED)
        self.assertEqual(eq.read_file(sg.baseline_path(self.path)), COMMITTED)
        self.assertFalse(os.path.exists(sg.lock_path(self.path)))

    def test_a_failed_build_still_restores_when_the_stage_closes(self):
        """The stage closes the session in a finally, so a build that raises
        leaves the file as it was found."""
        session = self.open()
        try:
            session.build("c1", BARE_POLLUTED, self.combo_dir, log=lambda _m: None)
            raise RuntimeError("rebuild crashed")
        except RuntimeError:
            pass
        finally:
            session.close(log=lambda _m: None)
        self.assertEqual(eq.read_file(self.path), COMMITTED)
        self.assertFalse(os.path.exists(sg.lock_path(self.path)))

    def test_a_failed_open_restores_and_releases(self):
        with mock.patch.object(sg, "open_and_activate_part", return_value=(None, "no part")):
            with self.assertRaises(RuntimeError):
                self.open()
        self.assertEqual(eq.read_file(self.path), COMMITTED)
        self.assertFalse(os.path.exists(sg.lock_path(self.path)))


class TestTheReproduction(Base):
    def test_a_crashed_bare_run_is_not_inherited_by_the_next_chip_run(self):
        """The 2026-10-25 sequence: a bare build is writes no_chip = 1 into the
        file and then dies without closing. The next build must refuse to
        start from the polluted file -- before, it took no_chip = 1 as its
        baseline, restored it, and built its chip run as a bare board."""
        crashed = self.open()
        crashed.build("bare", BARE_POLLUTED, self.combo_dir, log=lambda _m: None)
        # the process is gone: its lock is left behind with a dead pid
        self.leave_lock_held_by(self.dead_pid())
        with self.assertRaises(sg.PollutedBaseline) as caught:
            self.open()
        self.assertIn("no_chip", str(caught.exception))
        # refusing touches nothing: the polluted file is left for a person to look at
        self.assertEqual(eq.read_file(self.path), BARE_POLLUTED)

    def test_the_baseline_is_the_recorded_one_not_the_polluted_file(self):
        self.open().close(log=lambda _m: None)           # records COMMITTED
        eq.write_file(self.path, BARE_POLLUTED)            # someone left it polluted
        self.assertEqual(sg.baseline_content(self.path), COMMITTED)

    def test_overlapping_build_waits_instead_of_reading_a_held_file(self):
        first = self.open()
        first.build("bare", BARE_POLLUTED, self.combo_dir, log=lambda _m: None)
        with self.assertRaises(sg.EquationsBusy):
            self.open(timeout_s=0.0)                       # live holder: not stale
        # and the second generator did not write anything
        self.assertEqual(eq.read_file(self.path), BARE_POLLUTED)
        first.close(log=lambda _m: None)
        self.assertEqual(eq.read_file(self.path), COMMITTED)


class TestTheLock(Base):
    def test_a_stale_lock_from_a_dead_process_is_removed(self):
        self.leave_lock_held_by(self.dead_pid())
        session = self.open()
        session.close(log=lambda _m: None)
        self.assertFalse(os.path.exists(sg.lock_path(self.path)))

    def test_a_lock_older_than_the_limit_is_removed_even_if_its_pid_lives(self):
        self.leave_lock_held_by(os.getpid())
        old = os.path.getmtime(sg.lock_path(self.path)) - sg.LOCK_MAX_AGE_S - 60
        os.utime(sg.lock_path(self.path), (old, old))
        session = self.open()
        session.close(log=lambda _m: None)

    def test_an_unreadable_fresh_lock_is_waited_for(self):
        with open(sg.lock_path(self.path), "w", encoding="utf-8") as f:
            f.write("")
        with self.assertRaises(sg.EquationsBusy):
            self.open(timeout_s=0.0)
        self.assertTrue(os.path.exists(sg.lock_path(self.path)))

    def test_release_only_removes_our_own_lock(self):
        self.leave_lock_held_by(self.dead_pid())
        sg.release_lock(self.path)
        self.assertTrue(os.path.exists(sg.lock_path(self.path)))


class TestBaselineFirstUse(Base):
    def test_with_no_record_the_file_as_it_stands_becomes_the_baseline(self):
        messages = []
        with mock.patch.object(sg, "logger"):
            text = sg.check_baseline(self.path, log=messages.append)
        self.assertEqual(text, COMMITTED)
        self.assertEqual(eq.read_file(sg.baseline_path(self.path)), COMMITTED)
        self.assertTrue(any("No baseline recorded" in m for m in messages))

    def test_with_no_record_a_file_that_differs_from_head_is_warned_about(self):
        messages = []
        with mock.patch.object(sg, "committed_content", return_value=COMMITTED):
            sg.check_baseline(self.path, log=messages.append)   # live == HEAD: no warning
            self.assertFalse(any("WARNING" in m for m in messages))
        os.remove(sg.baseline_path(self.path))
        eq.write_file(self.path, BARE_POLLUTED)
        messages = []
        with mock.patch.object(sg, "committed_content", return_value=COMMITTED):
            text = sg.check_baseline(self.path, log=messages.append)
        self.assertTrue(any("WARNING" in m for m in messages))
        self.assertEqual(text, BARE_POLLUTED)                    # still recorded
        self.assertEqual(eq.read_file(sg.baseline_path(self.path)), BARE_POLLUTED)


class TestTheBaselineAgainstTheCommittedFile(Base):
    def record(self, content):
        eq.write_file(sg.baseline_path(self.path), content)

    def test_live_equal_to_the_sidecar_is_used_without_asking_git(self):
        self.record(COMMITTED)
        with mock.patch.object(sg, "committed_content") as git:
            self.assertEqual(sg.check_baseline(self.path, log=lambda _m: None), COMMITTED)
        git.assert_not_called()

    def test_live_equal_to_head_refreshes_the_sidecar_and_continues(self):
        """A committed edit to default.txt, pulled in after the last restore."""
        edited = COMMITTED.replace('"pedestal_depth"= 0.2', '"pedestal_depth"= 0.3')
        self.record(COMMITTED)
        eq.write_file(self.path, edited)
        messages = []
        with mock.patch.object(sg, "committed_content", return_value=edited):
            self.assertEqual(sg.check_baseline(self.path, log=messages.append), edited)
        self.assertEqual(eq.read_file(sg.baseline_path(self.path)), edited)
        self.assertTrue(any("refreshing" in m for m in messages))

    def test_live_different_from_both_is_refused_and_the_sidecar_is_kept(self):
        self.record(COMMITTED)
        eq.write_file(self.path, BARE_POLLUTED)
        with mock.patch.object(sg, "committed_content", return_value=COMMITTED):
            with self.assertRaises(sg.PollutedBaseline):
                sg.check_baseline(self.path, log=lambda _m: None)
        self.assertEqual(eq.read_file(sg.baseline_path(self.path)), COMMITTED)

    def test_with_no_git_a_file_differing_from_the_sidecar_is_refused(self):
        self.record(COMMITTED)
        eq.write_file(self.path, BARE_POLLUTED)
        with mock.patch.object(sg, "committed_content", return_value=None):
            with self.assertRaises(sg.PollutedBaseline):
                sg.check_baseline(self.path, log=lambda _m: None)

    def test_committed_content_is_none_outside_a_repository(self):
        """The real lookup: a temporary folder is not tracked by any git repo."""
        self.assertIsNone(sg.committed_content(self.path))

    def test_committed_content_is_none_when_git_cannot_run(self):
        with mock.patch.object(sg.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertIsNone(sg.committed_content(self.path))


class TestSanityCheckOfTheDimensions(unittest.TestCase):
    DIMS = '"no_chip"= 1\n"pedestal_depth"= 0\n"pedestal_shape"= 0\n'

    def test_a_chip_run_with_a_bare_board_model_is_a_mismatch(self):
        problems = sg.dimension_mismatches(self.DIMS, {"no_chip": 0.0})
        self.assertEqual(len(problems), 1)
        self.assertIn("no_chip", problems[0])

    def test_matching_values_pass(self):
        self.assertEqual(sg.dimension_mismatches(self.DIMS, {"no_chip": 1.0, "pedestal_depth": 0.0}), [])

    def test_a_pinned_value_missing_from_the_file_is_a_mismatch(self):
        self.assertEqual(len(sg.dimension_mismatches("", {"no_chip": 0.0})), 1)


class RunsBase(Base):
    """A temporary equations folder, and a temporary runs folder for Run.create."""

    def setUp(self):
        super().setUp()
        runs = tempfile.TemporaryDirectory(prefix="runs_")
        self.addCleanup(runs.cleanup)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = runs.name
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)

    def make_run(self, solution_type, name, value, **settings):
        analysis = {**pipeline.DEFAULT_ANALYSIS_SETTINGS, "solution_type": solution_type,
                    "run_both": False, **settings}
        return Run.create([SweepParameter(name=name, values=[value])], analysis, {})

    def run_stages(self, run):
        stages.run_local_stages(
            run, equations_path=self.path, part_paths={},
            analysis_settings=run.analysis_settings, handoff=lambda *_a: None,
            parameter_defaults={}, log=lambda _m: None)


class TestTheBaselineOfARunWithoutSolidWorks(Base):
    """A run that draws its own model never opens a SolidWorks session, so it
    reads the baseline itself, under the lock. It takes up a committed change
    to default.txt, never refuses or rewrites a file it does not own, and never
    records a first baseline from a file it has not been told is committed."""

    def record(self, content):
        eq.write_file(sg.baseline_path(self.path), content)

    def test_a_committed_change_refreshes_the_record_and_is_used(self):
        self.record(COMMITTED)                              # the record from before the pull
        eq.write_file(self.path, NEWER)                     # the pull brought the new file in
        messages = []
        with mock.patch.object(sg, "committed_content", return_value=NEWER):
            text = sg.baseline_for_a_run_without_solidworks(self.path, log=messages.append)
        self.assertEqual(text, NEWER)
        self.assertEqual(eq.read_file(sg.baseline_path(self.path)), NEWER)
        self.assertTrue(any("refreshing" in m for m in messages))
        self.assertFalse(os.path.exists(sg.lock_path(self.path)))

    def test_a_live_file_equal_to_the_record_is_used_without_asking_git(self):
        self.record(COMMITTED)
        with mock.patch.object(sg, "committed_content") as git:
            text = sg.baseline_for_a_run_without_solidworks(self.path, log=lambda _m: None)
        self.assertEqual(text, COMMITTED)
        git.assert_not_called()
        self.assertFalse(os.path.exists(sg.lock_path(self.path)))

    def test_a_polluted_live_file_is_logged_and_the_record_is_used(self):
        self.record(COMMITTED)
        eq.write_file(self.path, BARE_POLLUTED)
        messages = []
        with mock.patch.object(sg, "committed_content", return_value=COMMITTED):
            text = sg.baseline_for_a_run_without_solidworks(self.path, log=messages.append)
        self.assertEqual(text, COMMITTED)
        self.assertEqual(eq.read_file(sg.baseline_path(self.path)), COMMITTED)
        self.assertEqual(eq.read_file(self.path), BARE_POLLUTED)     # not written, not restored
        self.assertTrue(any("WARNING" in m and "no_chip" in m for m in messages))
        self.assertFalse(os.path.exists(sg.lock_path(self.path)))

    def test_with_no_record_the_file_as_it_stands_is_used_and_nothing_is_recorded(self):
        eq.write_file(self.path, BARE_POLLUTED)
        with mock.patch.object(sg, "committed_content", return_value=COMMITTED):
            text = sg.baseline_for_a_run_without_solidworks(self.path, log=lambda _m: None)
        self.assertEqual(text, BARE_POLLUTED)                # as baseline_content always gave it
        self.assertFalse(os.path.exists(sg.baseline_path(self.path)))

    def test_with_no_git_the_recorded_baseline_is_used_as_it_always_was(self):
        self.record(COMMITTED)
        eq.write_file(self.path, NEWER)
        with mock.patch.object(sg.subprocess, "run", side_effect=FileNotFoundError("git")):
            text = sg.baseline_for_a_run_without_solidworks(self.path, log=lambda _m: None)
        self.assertEqual(text, COMMITTED)
        self.assertEqual(text, sg.baseline_content(self.path))
        self.assertEqual(eq.read_file(sg.baseline_path(self.path)), COMMITTED)
        self.assertFalse(os.path.exists(sg.lock_path(self.path)))

    def test_a_lock_held_by_a_live_session_is_neither_waited_for_nor_removed(self):
        self.record(COMMITTED)
        eq.write_file(self.path, NEWER)
        self.leave_lock_held_by(os.getpid())                 # a SolidWorks session in this process
        messages = []
        with mock.patch.object(sg, "BASELINE_READ_WAIT_S", 0.0), \
                mock.patch.object(sg, "committed_content") as git:
            text = sg.baseline_for_a_run_without_solidworks(self.path, log=messages.append)
        self.assertEqual(text, COMMITTED)
        git.assert_not_called()                              # the live file was not read at all
        self.assertTrue(os.path.exists(sg.lock_path(self.path)))       # not ours: left in place
        self.assertTrue(any("Could not take the lock" in m for m in messages))

    def test_the_live_file_is_read_once_and_only_while_the_lock_is_held(self):
        self.record(COMMITTED)
        eq.write_file(self.path, NEWER)
        real_read = eq.read_file
        seen = []

        def spy(path):
            if os.path.abspath(path) == os.path.abspath(self.path):
                seen.append(os.path.exists(sg.lock_path(self.path)))
            return real_read(path)

        with mock.patch.object(eq, "read_file", side_effect=spy), \
                mock.patch.object(sg, "committed_content", return_value=NEWER):
            sg.baseline_for_a_run_without_solidworks(self.path, log=lambda _m: None)
        self.assertEqual(seen, [True])

    def test_a_record_that_cannot_be_refreshed_does_not_fail_the_run(self):
        self.record(COMMITTED)
        eq.write_file(self.path, NEWER)
        with mock.patch.object(sg, "committed_content", return_value=NEWER), \
                mock.patch.object(eq, "write_file", side_effect=PermissionError("read-only")):
            text = sg.baseline_for_a_run_without_solidworks(self.path, log=lambda _m: None)
        self.assertEqual(text, NEWER)
        self.assertFalse(os.path.exists(sg.lock_path(self.path)))

    def test_the_lock_is_released_when_the_read_fails(self):
        os.remove(self.path)                                 # no live file and no record
        with self.assertRaises(FileNotFoundError):
            sg.baseline_for_a_run_without_solidworks(self.path, log=lambda _m: None)
        self.assertFalse(os.path.exists(sg.lock_path(self.path)))


@unittest.skipUnless(shutil.which("git"), "git is needed to commit the fixture's default.txt")
class TestACommittedChangeReachesARunWithoutSolidWorks(RunsBase):
    """The 2026-10-29 failure: a LaunchStudy run with a lever on launch_taper_length
    died because the record was the file from before PR #103. Here the fixture is
    a real git repository: the record is the old file, the live file and HEAD
    are the new one, as after a git pull."""

    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.commit(COMMITTED, "default.txt before the change")
        eq.write_file(sg.baseline_path(self.path), COMMITTED)   # the record from the last restore
        self.commit(NEWER, "default.txt: add launch_taper_length")  # live == HEAD == NEWER

    def git(self, *args):
        subprocess.run(["git", "-C", self.dir, *args], check=True)

    def commit(self, content, message):
        eq.write_file(self.path, content)
        self.git("add", "default.txt")
        self.git("-c", "user.name=test", "-c", "user.email=test@example.invalid",
                 "commit", "-q", "-m", message)

    def test_a_lever_on_the_new_parameter_is_built_from_the_committed_file(self):
        run = self.make_run(LAUNCH_STUDY, "launch_taper_length", 0.5, use_altium=False)
        combo = next(iter(run.state["combos"]))
        with mock.patch.object(stages, "_AedtStage") as aedt:
            aedt.return_value.build.return_value = True
            self.run_stages(run)
        snapshot = eq.parse(eq.read_file(os.path.join(run.combo_dir(combo), "dimensions.txt")))
        self.assertEqual(snapshot["launch_taper_length"].numeric_value, 0.5)
        self.assertEqual(eq.read_file(sg.baseline_path(self.path)), NEWER)
        self.assertFalse(os.path.exists(sg.lock_path(self.path)))

    def test_the_altium_stage_is_laid_over_the_same_refreshed_baseline(self):
        """LineImpedance draws no solid but does draw its copper in Altium, and
        that stage reads the same baseline."""
        run = self.make_run(LINE_IMPEDANCE, "pedestal_depth", 0.0, use_altium=True)
        with mock.patch.object(stages, "_AltiumStage") as altium, \
                mock.patch.object(stages, "_AedtStage") as aedt:
            altium.return_value.build.return_value = True
            aedt.return_value.build.return_value = True
            self.run_stages(run)
        self.assertEqual(altium.call_args[0][1], NEWER)


class TestACommittedChangeReachesARunWithSolidWorks(TestACommittedChangeReachesARunWithoutSolidWorks):
    """The 2026-11-22 failure (vias_off): the record lacked an equation the
    pull added, the run pinned it, and layering it over the record raised
    KeyError before the SolidWorks session could refresh the record."""

    def test_a_run_pinning_the_new_equation_is_laid_over_the_committed_file(self):
        run = self.make_run("Eigenmode", "launch_taper_length", 0.5, use_altium=False)
        combo = next(iter(run.state["combos"]))
        with mock.patch.object(stages, "_AedtStage") as aedt:
            aedt.return_value.build.return_value = True
            self.run_stages(run)                           # no KeyError
        snapshot = eq.parse(eq.read_file(os.path.join(run.combo_dir(combo), "dimensions.txt")))
        self.assertEqual(snapshot["launch_taper_length"].numeric_value, 0.5)
        self.assertEqual(eq.read_file(sg.baseline_path(self.path)), NEWER)   # refreshed by the session
        self.assertFalse(os.path.exists(sg.lock_path(self.path)))

    def test_the_read_itself_writes_nothing_and_takes_no_lock(self):
        before = eq.read_file(sg.baseline_path(self.path))
        text = sg.baseline_for_a_run_with_solidworks(self.path, log=lambda _m: None)
        self.assertEqual(text, NEWER)
        self.assertEqual(eq.read_file(sg.baseline_path(self.path)), before)
        self.assertFalse(os.path.exists(sg.lock_path(self.path)))

    def test_a_polluted_live_file_is_passed_over_and_the_session_still_refuses_it(self):
        eq.write_file(self.path, BARE_POLLUTED)            # not committed (HEAD is NEWER)
        self.assertEqual(sg.baseline_for_a_run_with_solidworks(self.path, log=lambda _m: None), COMMITTED)
        with self.assertRaises(sg.PollutedBaseline):
            self.open()


class TestTheSolidWorksPathIsUnchanged(RunsBase):
    """Runs that use SolidWorks are checked against the live file as they always were."""

    def test_a_session_refuses_a_polluted_file_and_touches_nothing(self):
        eq.write_file(sg.baseline_path(self.path), COMMITTED)
        eq.write_file(self.path, BARE_POLLUTED)
        with mock.patch.object(sg, "committed_content", return_value=COMMITTED):
            with self.assertRaises(sg.PollutedBaseline):
                self.open()
        self.assertEqual(eq.read_file(self.path), BARE_POLLUTED)
        self.assertEqual(eq.read_file(sg.baseline_path(self.path)), COMMITTED)
        self.assertFalse(os.path.exists(sg.lock_path(self.path)))

    def test_a_solidworks_run_is_refused_the_polluted_file_by_its_stage(self):
        eq.write_file(sg.baseline_path(self.path), COMMITTED)
        eq.write_file(self.path, BARE_POLLUTED)
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.INFO)       # the lane logs the traceback
        run = self.make_run("Eigenmode", "pedestal_depth", 0.2, use_altium=False)
        with mock.patch.object(sg, "committed_content", return_value=COMMITTED), \
                mock.patch.object(stages, "_AedtStage"):
            with self.assertRaises(sg.PollutedBaseline):
                self.run_stages(run)
        self.assertEqual(eq.read_file(self.path), BARE_POLLUTED)
        self.assertEqual(eq.read_file(sg.baseline_path(self.path)), COMMITTED)
        self.assertFalse(os.path.exists(sg.lock_path(self.path)))


if __name__ == "__main__":
    unittest.main()
