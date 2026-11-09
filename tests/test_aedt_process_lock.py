"""The AEDT lock across processes, not just threads.

Each run's pipeline is its own process, so the thread lock alone does not
keep two AEDT users apart. These tests start real child processes that
take the same file lock; nothing here starts AEDT. Every test points the
lock file at a temp file (MICROWAVE_AEDT_LOCK_PATH), never at the PC's.
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from microwave_package.hfss import model_builder

ROOT = Path(__file__).resolve().parent.parent

# Run as a child process: it takes the lock, reports its own pid, and holds
# the lock for the number of seconds it is given. Module-level so the tests
# share it. Its pid is the one that matters: on Windows the venv's python.exe
# is a launcher, so the pid Popen returns is not the interpreter that locks.
HOLDER = """
import os, sys, time
from microwave_package.hfss import model_builder

@model_builder.one_aedt_at_a_time
def hold(seconds):
    print("held", os.getpid(), flush=True)
    time.sleep(seconds)

hold(float(sys.argv[1]))
"""


@model_builder.one_aedt_at_a_time
def _work(log=None):
    return "done"


@model_builder.one_aedt_at_a_time
def _inner():
    return "inner"


@model_builder.one_aedt_at_a_time
def _outer():
    return _inner()


@model_builder.one_aedt_at_a_time
def _fails():
    raise RuntimeError("AEDT said no")


class TestAedtFileLock(unittest.TestCase):
    def setUp(self):
        folder = tempfile.mkdtemp(prefix="aedt_lock_test_")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        self.lock_file = os.path.join(folder, "aedt_session.lock")
        patcher = mock.patch.dict(os.environ, {model_builder.AEDT_LOCK_ENV: self.lock_file})
        patcher.start()
        self.addCleanup(patcher.stop)

    def child(self, seconds):
        """Start a child that takes the lock; returns (process, its pid)."""
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(seconds)],
                                cwd=str(ROOT), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def stop():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            proc.stdout.close()
            proc.stderr.close()
        self.addCleanup(stop)

        line = proc.stdout.readline().split()
        if line[:1] != ["held"]:  # read stderr only now: reading it waits for the child to end
            self.fail(f"the child did not take the lock: {proc.stderr.read()}")
        return proc, int(line[1])

    def test_a_process_that_holds_it_makes_the_other_wait(self):
        proc, pid = self.child(2.0)

        messages = []
        started = time.monotonic()
        self.assertEqual(_work(log=messages.append), "done")
        waited = time.monotonic() - started

        self.assertGreater(waited, 0.8)
        self.assertTrue(messages and f"held by pid {pid} (hold)" in messages[0], messages)
        proc.wait(timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr.read())

    def test_a_killed_holder_frees_it_at_once(self):
        proc, pid = self.child(60)
        os.kill(pid, signal.SIGTERM)  # on Windows this ends the process at once
        proc.wait(timeout=10)

        started = time.monotonic()
        done = []
        worker = threading.Thread(target=lambda: done.append(_work()))
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "the lock was not freed when its holder died")
        self.assertEqual(done, ["done"])
        self.assertLess(time.monotonic() - started, 5)

    def test_nested_calls_do_not_wait_on_themselves(self):
        result = []
        worker = threading.Thread(target=lambda: result.append(_outer()))
        worker.start()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive(), "a nested call deadlocked")
        self.assertEqual(result, ["inner"])
        self.assertEqual(model_builder._aedt_depth, 0)
        self.assertEqual(_work(), "done")  # and the file lock was given back

    def test_an_exception_gives_the_file_lock_back(self):
        with self.assertRaises(RuntimeError):
            _fails()
        self.assertEqual(model_builder._aedt_depth, 0)
        self.assertEqual(_work(), "done")

    def test_the_lock_file_is_where_the_override_says_and_it_is_created(self):
        self.assertEqual(model_builder.aedt_lock_path(), self.lock_file)
        _work()
        self.assertTrue(os.path.exists(self.lock_file))

    def test_without_the_override_it_is_the_fixed_home_path(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(model_builder.AEDT_LOCK_ENV, None)
            expected = Path.home() / ".microwave_package" / "aedt_session.lock"
            self.assertEqual(Path(model_builder.aedt_lock_path()), expected)


if __name__ == "__main__":
    unittest.main()
