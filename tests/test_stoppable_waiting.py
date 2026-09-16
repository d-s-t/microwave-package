"""Tests for getting out of a retry when the run is stopped.

"Retry forever" is right for a share that will come back and wrong for
one that will not, and the difference is a person deciding to stop. A
Stop that cannot interrupt a retry is not really a Stop -- which is not
hypothetical: a queue folder that had stopped accepting writes held a
run in a ten-second retry loop that the Stop button could not reach.

Run: python -m unittest discover -s tests
"""
import logging
import threading
import unittest

from microwave_package.workflow import resilient_io

OFFLINE = r"C:\__no_such_queue_root__\queue"


class TestWaitingForAShare(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_a_share_that_is_there_is_not_waited_for(self):
        import tempfile

        self.assertFalse(resilient_io.wait_until_available(tempfile.mkdtemp()))

    def test_a_stop_gets_out_of_the_wait(self):
        stop_event = threading.Event()
        stop_event.set()
        with self.assertRaises(resilient_io.Stopped):
            resilient_io.wait_until_available(OFFLINE, poll_seconds=0.01, stop_event=stop_event)

    def test_a_stop_part_way_through_gets_out_too(self):
        """The event is set while the wait is already going round, which
        is what actually happens: somebody presses Stop on a run that has
        been retrying for a while."""
        stop_event = threading.Event()
        threading.Timer(0.05, stop_event.set).start()
        with self.assertRaises(resilient_io.Stopped):
            resilient_io.wait_until_available(OFFLINE, poll_seconds=0.02, stop_event=stop_event)


class TestRetryingACall(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_a_call_that_works_is_not_retried(self):
        calls = []
        result = resilient_io.call_with_retry(lambda: calls.append(1) or "done")
        self.assertEqual(result, "done")
        self.assertEqual(len(calls), 1)

    def test_a_failing_call_is_retried_until_it_works(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError("the share blinked")
            return "done"

        self.assertEqual(
            resilient_io.call_with_retry(flaky, poll_seconds=0.01), "done"
        )
        self.assertEqual(len(attempts), 3)

    def test_a_stop_ends_the_retrying(self):
        """The failure that made this necessary: an unwritable queue
        folder, retried every ten seconds, with no way out."""
        stop_event = threading.Event()
        attempts = []

        def never_works():
            attempts.append(1)
            if len(attempts) == 2:
                stop_event.set()
            raise OSError("access is denied")

        with self.assertRaises(resilient_io.Stopped):
            resilient_io.call_with_retry(never_works, poll_seconds=0.01, stop_event=stop_event)
        self.assertEqual(len(attempts), 2, "it kept trying after the stop")

    def test_a_stop_before_the_first_attempt_does_not_call_at_all(self):
        stop_event = threading.Event()
        stop_event.set()
        called = []
        with self.assertRaises(resilient_io.Stopped):
            resilient_io.call_with_retry(lambda: called.append(1), stop_event=stop_event)
        self.assertEqual(called, [])

    def test_the_stop_reaches_the_work_as_well_as_the_loop(self):
        """Two jobs, one event: the retry loop gets out, and the work
        itself stops at its own next checkpoint."""
        stop_event = threading.Event()
        seen = {}

        def work(value, stop_event=None):
            seen["stop_event"] = stop_event
            return value

        self.assertEqual(
            resilient_io.call_with_retry(work, 42, stop_event=stop_event), 42
        )
        self.assertIs(seen["stop_event"], stop_event)

    def test_work_that_does_not_take_one_is_not_handed_one(self):
        """Most of what is retried is plain filesystem work with no
        checkpoints of its own -- passing it an argument it does not
        accept would fail the call rather than stop it."""
        def work(value):
            return value

        self.assertEqual(
            resilient_io.call_with_retry(work, 42, stop_event=threading.Event()), 42
        )


if __name__ == "__main__":
    unittest.main()
