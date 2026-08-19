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

logger = logging.getLogger("eigen_mode_analyze")

TERMINAL_STATUSES = {"done", "failed"}


def monitor_run(
    run,
    queue: "queue_common.QueuePaths",
    poll_seconds: float = 10.0,
    log: Callable[[str], None] = logger.info,
    stop_event=None,
) -> None:
    combos = run.state["combos"]
    pending_ids = [
        cid for cid, c in combos.items()
        if c.get("task_id") and c.get("queue_status") not in TERMINAL_STATUSES
    ]
    if not pending_ids:
        log("Nothing to monitor -- every combo is already done/failed, or has no queued task.")
        return

    log(f"Monitoring {len(pending_ids)} task(s)...")
    while pending_ids:
        if stop_event is not None and stop_event.is_set():
            log("Monitoring stopped.")
            return

        resilient_io.wait_until_available(
            queue.root,
            poll_seconds=poll_seconds,
            on_wait=lambda p: log(f"Queue path {p} is unreachable -- waiting for it to come back..."),
        )

        still_pending = []
        for combo_id in pending_ids:
            task_id = combos[combo_id]["task_id"]
            try:
                status = queue_common.get_task_status(queue, task_id)
            except OSError as exc:
                log(f"Could not check status of {combo_id} ({exc}) -- will retry.")
                still_pending.append(combo_id)
                continue

            if status != combos[combo_id].get("queue_status"):
                run.update_combo(combo_id, queue_status=status)
                if status in TERMINAL_STATUSES:
                    log(f"{combo_id} (task {task_id}) -> {status}")

            if status not in TERMINAL_STATUSES:
                still_pending.append(combo_id)

        pending_ids = still_pending
        if pending_ids:
            time.sleep(poll_seconds)

    log("All queued tasks finished.")
