"""Drives one run's pipeline in a process of its own: `python -m microwave_package.pipeline_child RUN_ID`.

The web app starts a run here rather than on one of its own threads. A model
build holds the interpreter lock inside native calls for long stretches, and
while it does, the whole web app -- every page and /runs.json -- stops answering
(issue #97). A separate process has its own lock, so the web app stays up.

The web app asks the run to stop by creating STOP_FILE in the run's folder; this
process turns that into the same stop_event the in-process thread used.
"""
import logging
import os
import threading
import traceback

from . import pipeline
from .workflow import resilient_io
from .workflow.run_manager import Run

logger = logging.getLogger("microwave_package")

STOP_FILE = "stop.requested"
_STOP_POLL_SECONDS = 1.0


def stop_file_path(run: Run) -> str:
    return os.path.join(run.run_dir, STOP_FILE)


def clear_stop_request(run: Run) -> None:
    """Forget a stop asked for an earlier attempt, so a fresh start is not stopped at once."""
    try:
        os.remove(stop_file_path(run))
    except OSError:
        pass


def watch_for_stop(run: Run, stop_event: threading.Event) -> None:
    while not stop_event.wait(_STOP_POLL_SECONDS):
        if os.path.exists(stop_file_path(run)):
            stop_event.set()
            return


def main(run_id: str) -> int:
    import pythoncom

    # COM requires each thread that uses it to initialise it first; this
    # process's main thread is the one driving SolidWorks and AEDT.
    pythoncom.CoInitialize()
    try:
        run = Run.load(run_id)
        queue = resilient_io.queue_from_env("ANSYS_ANALYZE_QUEUE_PATH")
        stop_event = threading.Event()
        threading.Thread(target=watch_for_stop, args=(run, stop_event), daemon=True).start()
        pipeline.run_pipeline(run, queue, log=logger.info, stop_event=stop_event)
        return 0
    except Exception:
        # Saved onto the run so the web UI shows what happened, as the
        # in-process driver did; the console gets it too.
        logger.exception("Pipeline for run %s crashed", run_id)
        run = Run.load(run_id)
        run.state["stage"] = "error"
        run.state["error"] = traceback.format_exc()
        run.save()
        return 1
    finally:
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    sys.exit(main(sys.argv[1]))
