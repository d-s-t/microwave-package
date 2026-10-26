"""The shared equations file is restored, locked and checked around a build.

A run's SolidWorks stage writes each combination's values into the shared
`base_parts/default.txt` and must put the file back afterwards. Before this,
a stage that stopped without restoring (or two overlapping builds) left the
file polluted, the next build took the polluted file as its baseline, and
chip runs were built as bare boards. These tests drive Session with fake
SolidWorks calls on a temporary equations file -- nothing opens SolidWorks.

Run: python -m unittest discover -s tests
"""
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from microwave_package.design import equations as eq
from microwave_package.design import step_generation as sg

COMMITTED = (
    '"no_chip"= 0\n'
    '"screws_on"= 1\n'
    '"vents_on"= 1\n'
    '"pedestal_depth"= 0.2\n'
    '"pedestal_shape"= 0\n'
)
BARE_POLLUTED = COMMITTED.replace('"no_chip"= 0', '"no_chip"= 1')


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


if __name__ == "__main__":
    unittest.main()
