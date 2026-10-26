"""The web app must keep answering while a run's pipeline is building a model.

Issue #97: during an AEDT model build the app accepted connections but did not
answer for minutes, so other sessions thought it was down and restarted it.

The pipeline runs in a process of its own (pipeline_child). A stand-in for it
holds its interpreter lock inside one native call (Windows Sleep through
ctypes.PyDLL), the way a long native call in the build can. While it runs,
/runs.json and a run's status page are requested from a real server on another
port and must answer promptly.

Windows only (uses kernel32). Run: python -m unittest discover -s tests
"""
import json
import logging
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from werkzeug.serving import make_server

from microwave_package import paths, pipeline, pipeline_child, webapp
from microwave_package.design.sweep import SweepParameter
from microwave_package.workflow.run_manager import Run

HOLD_MS = 6000
REPLY_WITHIN_S = 2.0
PROBE_DELAY_S = 1.5

# Asks each route from a fresh process, once the stage has been holding the GIL
# for a moment, and reports how long each took (None when it failed or timed out).
# It runs out of process because anything in this test process, the test thread
# included, is stuck behind the held GIL too.
PROBE = """
import json, sys, time, urllib.request
base, run_id, delay = sys.argv[1], sys.argv[2], float(sys.argv[3])
time.sleep(delay)
results = {}
for path in ("/runs.json", "/runs/" + run_id):
    started = time.monotonic()
    try:
        with urllib.request.urlopen(base + path, timeout=10) as reply:
            reply.read()
        results[path] = time.monotonic() - started
    except Exception:
        results[path] = None
print(json.dumps(results))
"""


# Stands in for the pipeline process: holds its interpreter lock in one native call.
HOLDING_PIPELINE = [sys.executable, "-c",
                    "import ctypes; ctypes.PyDLL('kernel32').Sleep(%d)" % HOLD_MS]


@unittest.skipUnless(sys.platform == "win32", "needs kernel32 Sleep")
class TestWebAppAnswersDuringBuild(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)
        self.run = Run.create([SweepParameter(name="pedestal_depth", values=[0.0])],
                              dict(pipeline.DEFAULT_ANALYSIS_SETTINGS), {})

        server = make_server("127.0.0.1", 0, webapp.app, threaded=True)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def test_runs_json_and_status_page_answer_while_a_stage_holds_the_gil(self):
        probe = subprocess.Popen(
            [sys.executable, "-c", PROBE, self.base, self.run.run_id, str(PROBE_DELAY_S)],
            stdout=subprocess.PIPE, universal_newlines=True)
        with mock.patch.object(webapp, "PIPELINE_COMMAND", HOLDING_PIPELINE):
            webapp._start_pipeline_thread(self.run)
            out, _ = probe.communicate(timeout=60)

        for path, took in json.loads(out).items():
            with self.subTest(path=path):
                self.assertIsNotNone(took, f"{path} did not answer")
                self.assertLess(took, REPLY_WITHIN_S)


class TestStopFileReachesThePipelineProcess(unittest.TestCase):
    def test_a_stop_file_in_the_run_folder_sets_the_stop_event(self):
        run_dir = tempfile.mkdtemp(prefix="run_")
        run = types.SimpleNamespace(run_dir=run_dir)
        stop_event = threading.Event()
        watcher = threading.Thread(target=pipeline_child._watch_for_stop,
                                   args=(run, stop_event), daemon=True)
        with mock.patch.object(pipeline_child, "_STOP_POLL_SECONDS", 0.01):
            watcher.start()
            self.assertFalse(stop_event.wait(0.05))
            open(pipeline_child.stop_file_path(run), "w").close()
            self.assertTrue(stop_event.wait(5))
        watcher.join(5)


if __name__ == "__main__":
    unittest.main()
