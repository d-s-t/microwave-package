"""
Pause and resume a run's queued worker tasks: hold them out of the worker queue, and give them back.

The worker takes tasks from `<queue>/pending` in order, one at a time, so a
short run waits behind every task of a long one ahead of it. Pausing a run
moves its tasks that have not started yet into `<queue>/_held/<run_id>/`,
where the worker does not look. Resuming moves them back under the same
names, so their place in the queue is kept. A task the worker has already
picked up (`in_progress`) cannot be paused, and a finished one has nothing
to pause; both are reported and left alone.

Who writes what. The process driving a run is the only writer of that run's
`state.json` while it runs: its in-memory copy would overwrite anything
written behind its back. So while a run is being driven, pausing or resuming
it is a request file in the run's folder (`REQUEST_FILE`), which that process
applies (see pipeline_child.apply_queue_request). A run nobody is driving is
paused straight from the web app. Either way the moves go through `hold_run`
and `release_run`, under the run's `queue_lock`, which is what stops a task
being queued at the same moment from landing on the wrong side.

Where a held task is found. The worker's own library (queue_common) only
looks in done, failed, in_progress and pending, so a held task reads as
'unknown' there. `task_status` answers 'held' for it instead, so the monitor
keeps the status rather than overwriting it.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import types
from typing import Dict, List, Optional

from ansys_analyze_common import queue_common

from . import resilient_io

logger = logging.getLogger("microwave_package")

HELD_FOLDER = "_held"
REQUEST_FILE = "queue_hold.request.json"
ACTIONS = ("hold", "release")


def held_dir(queue, run_id: str) -> str:
    """The folder a run's held tasks wait in. Named for the run, so a
    run's held tasks can be found, released or deleted without reading
    anything else."""
    return os.path.join(queue.root, HELD_FOLDER, run_id)


def held_task_path(queue, run_id: str, task_id: str) -> str:
    return os.path.join(held_dir(queue, run_id), f"{task_id}.task.json")


def _pending_path(queue, task_id: str) -> str:
    return os.path.join(queue.pending, f"{task_id}.task.json")


def task_status(queue, run_id: str, task_id: str) -> str:
    """queue_common.get_task_status, which also knows a held task.

    Anything the library can see is answered exactly as it answers it. A
    task it cannot see, but which is in the run's held folder, is 'held'.
    """
    status = queue_common.get_task_status(queue, task_id)
    if status == "unknown" and os.path.exists(held_task_path(queue, run_id, task_id)):
        return "held"
    return status


def intake_queue(queue, run_id: str):
    """Where a task queued while its run is paused is written: the run's
    held folder, standing in for `queue.pending`.

    queue_common.write_task_atomically writes only into `queue.pending`, so
    this is enough to have it land in the held folder, with the same atomic
    write and the same name. Everything else (the project folder included)
    stays where it is, because the held task points at it.
    """
    folder = held_dir(queue, run_id)
    os.makedirs(folder, exist_ok=True)
    return types.SimpleNamespace(root=queue.root, pending=folder)


def _report(action: str) -> Dict[str, object]:
    return {
        "action": action,
        "moved": [],      # task ids moved (held, or back to the worker's pending folder)
        "already": [],    # task ids that were held already
        "started": [],    # task ids the worker has in progress: cannot be paused
        "finished": [],   # task ids already done or failed: nothing to pause
        "missing": [],    # task ids with no task file anywhere in the queue
        "gone": [],       # resume: tasks recorded as held whose file is not in the held folder
        "failed": [],     # a move that raised an OSError (the task is left where it was)
        "error": None,    # why nothing was done at all, e.g. the queue is unreachable
    }


def _unreachable(queue, report) -> bool:
    if resilient_io.path_available(queue.root):
        return False
    report["error"] = f"The queue at {queue.root} is not reachable, so nothing was changed."
    return True


def hold_run(run, queue) -> Dict[str, object]:
    """Pause: hold this run's tasks that are still waiting.

    Each combination's task file in `pending` moves to the held folder under
    its own name, and its status becomes 'held'. The run is marked paused
    first, so a task queued while this is going on is held too (see
    `intake_queue`). Returns a report of what happened.
    """
    report = _report("hold")
    if _unreachable(queue, report):
        return report
    with run.queue_lock:
        folder = held_dir(queue, run.run_id)
        changes: Dict[str, dict] = {}
        for combo_id, combo in run.state["combos"].items():
            task_id = combo.get("task_id")
            if not task_id:
                continue
            held = held_task_path(queue, run.run_id, task_id)
            if os.path.exists(held):
                report["already"].append(task_id)
                changes[combo_id] = {"queue_status": "held"}
                continue
            try:
                os.makedirs(folder, exist_ok=True)
                os.replace(_pending_path(queue, task_id), held)
            except FileNotFoundError:
                # Not waiting in pending: the worker has it, or has finished it.
                status = queue_common.get_task_status(queue, task_id)
                if status == "in_progress":
                    report["started"].append(task_id)
                elif status in ("done", "failed"):
                    report["finished"].append(task_id)
                else:
                    report["missing"].append(task_id)
                continue
            except OSError as exc:
                logger.warning("Could not hold task %s: %s", task_id, exc)
                report["failed"].append(task_id)
                continue
            report["moved"].append(task_id)
            changes[combo_id] = {"queue_status": "held"}
        run.update_queue_state(paused=True, combos=changes)
    return report


def release_run(run, queue) -> Dict[str, object]:
    """Resume: give this run's held tasks back to the worker.

    Each held task moves back to `pending` under its own name, so the worker
    takes it in the same place in the queue. Its status returns to 'pending'
    and the monitor reads the real one from the queue on its next pass. A
    task recorded as held whose file is not in the held folder is reported
    as `gone`; nothing is invented for it.
    """
    report = _report("release")
    if _unreachable(queue, report):
        return report
    with run.queue_lock:
        folder = held_dir(queue, run.run_id)
        changes: Dict[str, dict] = {}
        # Only task files: a temporary file left by an interrupted write is not one.
        listed = ({name for name in os.listdir(folder) if name.endswith(".task.json")}
                  if os.path.isdir(folder) else set())
        for combo_id, combo in run.state["combos"].items():
            task_id = combo.get("task_id")
            if not task_id:
                continue
            name = f"{task_id}.task.json"
            if name in listed:
                listed.discard(name)
                _give_back(queue, folder, task_id, report, changes, combo_id)
            elif combo.get("queue_status") == "held":
                if queue_common.get_task_status(queue, task_id) == "unknown":
                    report["gone"].append(task_id)
                changes[combo_id] = {"queue_status": "pending"}
        # Anything else of this run's still in the held folder goes back too:
        # a hold that was cut short leaves files the record does not show yet.
        for name in sorted(listed):
            _give_back(queue, folder, name[: -len(".task.json")], report, changes, None)
        if os.path.isdir(folder):
            try:
                os.rmdir(folder)
            except OSError:
                pass  # something is still in it: left for the next resume
        run.update_queue_state(paused=False, combos=changes)
    return report


def _give_back(queue, folder, task_id, report, changes, combo_id) -> None:
    pending = _pending_path(queue, task_id)
    if os.path.exists(pending):
        # Never overwrite a task the worker can see. Both copies are left for a person to look at.
        report["failed"].append(task_id)
        return
    try:
        os.replace(os.path.join(folder, f"{task_id}.task.json"), pending)
    except OSError as exc:
        logger.warning("Could not release task %s: %s", task_id, exc)
        report["failed"].append(task_id)
        return
    report["moved"].append(task_id)
    if combo_id is not None:
        changes[combo_id] = {"queue_status": "pending"}


def remove_held(queue, run_id: str) -> None:
    """Delete a run's held folder and the task files in it (used when the run is deleted).

    Raises OSError if the folder cannot be removed, so the caller can say so.
    """
    folder = held_dir(queue, run_id)
    if os.path.isdir(folder):
        shutil.rmtree(folder)


def describe(reports: List[Dict[str, object]], action: str) -> str:
    """One sentence for the run page, saying what a pause or resume did."""
    def ids(key):
        return [task_id for report in reports for task_id in report[key]]

    moved, started, gone = ids("moved"), ids("started"), ids("gone")
    if action == "hold":
        text = (f"Paused: {len(moved)} task(s) held out of the worker queue."
                if moved else "Paused: no task of this run was waiting in the queue.")
        already = ids("already")
        if already:
            text += f" {len(already)} were held already."
        if started:
            text += (f" {len(started)} the worker has already started cannot be paused: "
                     "they finish first.")
        finished, missing = ids("finished"), ids("missing")
        if finished:
            text += f" {len(finished)} already finished."
        if missing:
            text += f" {len(missing)} had no task file in the queue."
    else:
        text = f"Resumed: {len(moved)} task(s) back in the worker queue, in their original order."
        if gone:
            text += (f" {len(gone)} held task file(s) were not found in the held folder: "
                     + ", ".join(gone[:5]) + (" ..." if len(gone) > 5 else ""))
    failed = ids("failed")
    if failed:
        text += f" {len(failed)} could not be moved and were left as they were."
    return text


# -- requests from the web app to the process driving a run ------------------

def request_path(run_dir: str) -> str:
    return os.path.join(run_dir, REQUEST_FILE)


def write_request(run_dir: str, action: str) -> None:
    if action not in ACTIONS:
        raise ValueError(f"unknown queue action {action!r}")
    with open(request_path(run_dir), "w") as f:
        json.dump({"action": action}, f)


def request_pending(run_dir: str) -> bool:
    return os.path.exists(request_path(run_dir))


def take_request(run_dir: str) -> Optional[str]:
    """The pause or resume asked for and not yet applied, or None.

    The request is removed as it is taken, so each one is applied once. A
    file that is still being written is left for the next look.
    """
    path = request_path(run_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            action = json.load(f).get("action")
    except (OSError, ValueError, AttributeError):
        return None
    try:
        os.remove(path)
    except OSError:
        return None  # not taken: the next look reads and removes it
    return action if action in ACTIONS else None
