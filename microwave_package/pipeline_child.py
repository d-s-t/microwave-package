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
from typing import Optional

from . import pipeline
from .workflow import companion, queue_hold, resilient_io
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


# Serialises applying requests, so the watcher and the final apply below
# cannot both take the same one.
_REQUEST_LOCK = threading.Lock()


def _apply_here(target: Run, queue, action: str) -> None:
    if action == "hold":
        report = queue_hold.hold_run(target, queue)
    else:
        report = queue_hold.release_run(target, queue)
    logger.info("Run %s: %s", target.run_id, queue_hold.describe([report], action))


def apply_queue_request(run: Run, queue) -> Optional[str]:
    """Apply the pause or resume the web app asked for, to this run and its parts.

    Each target is changed by the one process that may write its state:
    - this run, and any part driven in this process (pipeline.live_run),
      are changed here, through the object that is driven here;
    - a part another process drives (a refinement has a process of its own)
      is not touched: its folder gets the request, and that process applies it;
    - any other part is loaded fresh and changed, since nothing is writing it.

    Returns the action applied, or None when there was nothing to do. The
    queue is not touched unless a request is waiting, and when the queue cannot
    be reached the request waits for the next look.
    """
    with _REQUEST_LOCK:
        if not queue_hold.request_pending(run.run_dir):
            return None  # local disk only: the share is not probed without a request
        if not resilient_io.path_available(queue.root):
            return None
        action = queue_hold.take_request(run.run_dir)
        if action is None:
            return None
        _apply_here(pipeline.live_run(run.run_id) or run, queue, action)
        for part in companion.parts_of(run):
            live = pipeline.live_run(part.run_id)
            if live is not None:
                _apply_here(live, queue, action)
            elif part.driven_elsewhere() is not None:
                queue_hold.write_request(part.run_dir, action)
            else:
                _apply_here(part, queue, action)
        return action


def watch_for_queue_requests(run: Run, queue, stop_event: threading.Event) -> None:
    """Apply the pauses and resumes the web app leaves for this run, as they come."""
    while not stop_event.wait(_STOP_POLL_SECONDS):
        try:
            apply_queue_request(run, queue)
        except Exception:
            logger.exception("Could not apply a pause or resume for run %s", run.run_id)


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
        threading.Thread(target=watch_for_queue_requests, args=(run, queue, stop_event),
                         daemon=True).start()
        pipeline.run_pipeline(run, queue, log=logger.info, stop_event=stop_event)
        # A pause or resume asked for while the run was finishing is applied
        # before this process exits, rather than left for the next driver.
        # Its failure is logged only: it must not mark the run as crashed.
        try:
            apply_queue_request(run, queue)
        except Exception:
            logger.exception("Could not apply a pause or resume for run %s", run.run_id)
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
