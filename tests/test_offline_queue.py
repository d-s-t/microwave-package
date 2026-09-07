"""Tests for preparing a run while the queue share is unreachable.

The geometry is built on this machine; only the handoff to the worker
needs the network. So an offline share must not stop a run being created
or its STEP files being generated -- it should only defer the handoff.

The failure this guards against is concrete: `QueuePaths.from_env`
creates the queue's subfolders as it goes, and it was called while a run
was starting, so a disconnected share meant a 500 and no run at all.

Run: python -m unittest discover -s tests
"""
import os
import tempfile
import time
import unittest

from eigen_mode_analyze import pipeline, resilient_io

# A path that cannot exist, rather than a real disconnected network
# drive. Probing a DISCONNECTED MAPPED drive costs ~21 s on the first
# call in a process, because Windows tries to reconnect it -- which is
# worth knowing about (the pipeline announces it) but is not something
# to pay on every test run. The logic under test is identical: isdir
# answers False without raising.
OFFLINE = r"C:\__no_such_queue_root__\queue"


class TestQueueFromEnv(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("ANSYS_ANALYZE_QUEUE_PATH")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("ANSYS_ANALYZE_QUEUE_PATH", None)
        else:
            os.environ["ANSYS_ANALYZE_QUEUE_PATH"] = self._saved

    def test_builds_against_an_unreachable_share(self):
        """The whole point: no filesystem access, so no exception."""
        os.environ["ANSYS_ANALYZE_QUEUE_PATH"] = OFFLINE
        queue = resilient_io.queue_from_env()
        self.assertEqual(queue.root, OFFLINE)

    def test_derives_the_subfolders_without_creating_them(self):
        os.environ["ANSYS_ANALYZE_QUEUE_PATH"] = OFFLINE
        queue = resilient_io.queue_from_env()
        self.assertTrue(queue.pending.startswith(OFFLINE))
        self.assertFalse(os.path.isdir(queue.pending))

    def test_an_unset_variable_still_explains_itself(self):
        """This is a genuine misconfiguration, unlike an offline share,
        so it should still fail loudly and say what to set."""
        os.environ.pop("ANSYS_ANALYZE_QUEUE_PATH", None)
        with self.assertRaises(EnvironmentError) as ctx:
            resilient_io.queue_from_env()
        self.assertIn("ANSYS_ANALYZE_QUEUE_PATH", str(ctx.exception))

    def test_a_reachable_share_is_reported_as_such(self):
        os.environ["ANSYS_ANALYZE_QUEUE_PATH"] = tempfile.mkdtemp()
        queue = resilient_io.queue_from_env()
        self.assertTrue(resilient_io.path_available(queue.root))

    def test_an_unreachable_share_is_reported_as_such(self):
        self.assertFalse(resilient_io.path_available(OFFLINE))


class TestEnsureQueueReady(unittest.TestCase):
    def test_creates_the_subfolders_once_the_share_is_there(self):
        """The half of from_env that genuinely needs the share, deferred
        to the point where the share is genuinely needed."""
        root = tempfile.mkdtemp()
        os.environ["ANSYS_ANALYZE_QUEUE_PATH"] = root
        try:
            queue = resilient_io.queue_from_env()
            self.assertFalse(os.path.isdir(queue.pending))
            resilient_io.ensure_queue_ready(queue)
            for path in (queue.pending, queue.in_progress, queue.done,
                         queue.failed, queue.projects, queue.logs):
                self.assertTrue(os.path.isdir(path), f"{path} should have been created")
        finally:
            os.environ.pop("ANSYS_ANALYZE_QUEUE_PATH", None)

    def test_is_safe_to_call_again(self):
        root = tempfile.mkdtemp()
        os.environ["ANSYS_ANALYZE_QUEUE_PATH"] = root
        try:
            queue = resilient_io.queue_from_env()
            resilient_io.ensure_queue_ready(queue)
            resilient_io.ensure_queue_ready(queue)
        finally:
            os.environ.pop("ANSYS_ANALYZE_QUEUE_PATH", None)


class TestQueueProbe(unittest.TestCase):
    """The reachability check runs on its own thread so the first STEP
    files and AEDT models start building immediately, instead of after
    the ~20s a disconnected mapped drive costs to answer. These pin the
    two outcomes the pipeline branches on: `wait()` gives the right
    answer, and a reachable queue comes back with its subfolders made.
    """

    def setUp(self):
        self._saved = os.environ.get("ANSYS_ANALYZE_QUEUE_PATH")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("ANSYS_ANALYZE_QUEUE_PATH", None)
        else:
            os.environ["ANSYS_ANALYZE_QUEUE_PATH"] = self._saved

    def test_an_unreachable_share_settles_as_not_reachable(self):
        os.environ["ANSYS_ANALYZE_QUEUE_PATH"] = OFFLINE
        queue = resilient_io.queue_from_env()
        probe = pipeline._QueueProbe(queue, log=lambda _m: None).start()
        self.assertFalse(probe.wait(timeout=30))

    def test_the_caller_is_not_blocked_while_the_probe_runs(self):
        """The whole point of the thread: building starts now, and the
        handoff only ever waits on the probe once a model is ready."""
        os.environ["ANSYS_ANALYZE_QUEUE_PATH"] = OFFLINE
        queue = resilient_io.queue_from_env()
        started = time.monotonic()
        pipeline._QueueProbe(queue, log=lambda _m: None).start()
        self.assertLess(time.monotonic() - started, 2)

    def test_a_reachable_share_comes_back_ready_to_write(self):
        os.environ["ANSYS_ANALYZE_QUEUE_PATH"] = tempfile.mkdtemp()
        queue = resilient_io.queue_from_env()
        probe = pipeline._QueueProbe(queue, log=lambda _m: None).start()
        self.assertTrue(probe.wait(timeout=30))
        for path in (queue.pending, queue.in_progress, queue.done,
                     queue.failed, queue.projects, queue.logs):
            self.assertTrue(os.path.isdir(path), f"{path} should have been created")

    def test_wait_after_it_settles_returns_at_once(self):
        os.environ["ANSYS_ANALYZE_QUEUE_PATH"] = OFFLINE
        queue = resilient_io.queue_from_env()
        probe = pipeline._QueueProbe(queue, log=lambda _m: None).start()
        self.assertFalse(probe.wait(timeout=30))
        started = time.monotonic()
        self.assertFalse(probe.wait(timeout=30))
        self.assertLess(time.monotonic() - started, 0.5)


class TestPathAvailable(unittest.TestCase):
    def test_a_file_is_not_a_usable_queue_root(self):
        handle, path = tempfile.mkstemp()
        os.close(handle)
        self.assertFalse(resilient_io.path_available(path))

    def test_malformed_paths_do_not_raise(self):
        """path_available is called in the hot loop; it must answer
        rather than throw, whatever it is handed."""
        for bad in ("", "\0bad", r"\\?\nonsense\path"):
            self.assertFalse(resilient_io.path_available(bad))


if __name__ == "__main__":
    unittest.main()
