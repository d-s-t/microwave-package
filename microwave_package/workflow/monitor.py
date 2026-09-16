"""
Polls the worker's task queue for the status of every combo's queued
task, prints a line to the screen the moment each one finishes (success
or failure) rather than only reporting at the very end, and keeps the
run's state file up to date so the web UI (or a resumed run) always
reflects the latest known status.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from ansys_analyze_common import queue_common

from . import resilient_io

logger = logging.getLogger("microwave_package")

TERMINAL_STATUSES = {"done", "failed"}


def monitor_run(
    run,
    queue: "queue_common.QueuePaths",
    poll_seconds: float = 10.0,
    log: Callable[[str], None] = logger.info,
    stop_event=None,
    more_expected: Optional[Callable[[], bool]] = None,
) -> None:
    """Track every queued task until it reaches a terminal status.

    The pending set is re-read from the run on each pass instead of being
    fixed up front, because this now runs alongside the stage that queues
    the tasks: models appear one by one while earlier ones are already
    being analysed. `more_expected` tells it whether that stage is still
    going -- while it returns True the loop keeps waiting even with
    nothing pending yet, and once it returns False the loop finishes as
    soon as the outstanding tasks land.
    """
    announced = False
    while True:
        if stop_event is not None and stop_event.is_set():
            log("Monitoring stopped.")
            return

        combos = run.state["combos"]
        pending_ids = [
            cid for cid, c in combos.items()
            if c.get("task_id") and c.get("queue_status") not in TERMINAL_STATUSES
        ]
        still_queueing = bool(more_expected and more_expected())

        if not pending_ids and not still_queueing:
            if announced:
                log("All queued tasks finished.")
            else:
                log("Nothing to monitor -- every combo is already done/failed, or has no queued task.")
            return

        if pending_ids and not announced:
            log(f"Monitoring {len(pending_ids)} task(s)...")
            announced = True

        # Anything unexpected in here is logged and retried on the next
        # pass rather than allowed out: this runs on its own thread, and
        # an exception escaping it would end status reporting for the
        # rest of the run without anything obviously breaking.
        try:
            if pending_ids:
                resilient_io.wait_until_available(
                    queue.root,
                    poll_seconds=poll_seconds,
                    on_wait=lambda p: log(f"Queue path {p} is unreachable -- waiting for it to come back..."),
                    stop_event=stop_event,
                )

            for combo_id in pending_ids:
                task_id = combos[combo_id]["task_id"]
                status = queue_common.get_task_status(queue, task_id)
                if status != combos[combo_id].get("queue_status"):
                    run.update_combo(combo_id, queue_status=status)
                    if status in TERMINAL_STATUSES:
                        log(f"{combo_id} (task {task_id}) -> {status}")
        except resilient_io.Stopped:
            log("Monitoring stopped.")
            return
        except Exception as exc:
            logger.exception("Monitoring pass failed; will retry in %.0fs", poll_seconds)
            log(f"Could not check task status ({exc}) -- will retry in {poll_seconds:.0f}s.")

        time.sleep(poll_seconds)
