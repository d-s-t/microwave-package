"""
Client-side post-processing: once the worker reports a task as done (it
only ever ran the project's existing setups -- see
ansys-analyze-worker's handlers/run_setups.py), this brings the analysed
files over, reopens the now-solved project itself, and hands it to the
run's solver to read. All of that used to happen inside the worker,
right after analyze() -- it's here now because none of it needs the full
license, only the worker's analyze() call does (see
ansys-analyze-worker's ARCHITECTURE.md section 12).

What gets read out is the solver's business, not this module's: it
orchestrates (fetch, open, write client_result.json, record the outcome)
and delegates the reading.

Runs strictly one task at a time, never in parallel -- driving more than
one AEDT session from the same pipeline at once isn't something the
student license setup here supports, and this step is quick (read-only
solution-data export) next to the analysis it follows, so there's no
real benefit to overlapping it anyway.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from typing import Any, Callable, Dict, List, Optional

from ansys_analyze_common import queue_common

from ..hfss import registry
from ..hfss.model_builder import aedt_version_for, close_project_safely, one_aedt_at_a_time
from . import resilient_io

logger = logging.getLogger("microwave_package")


@one_aedt_at_a_time
def postprocess_task(
    task: Dict[str, Any],
    aedt_version,
    student_version: bool,
    graphical: bool,
    log: Callable[[str], None],
) -> Dict[str, Any]:
    """Open the analyzed project (student version) and pull its results out.

    What "results" means is the solver's business -- an eigenmode
    project's modes, a driven project's S-matrix -- and it comes back as
    `rows`, one dict per result whatever a result is, so everything
    downstream builds one table the same way.

    `task`'s `project_file`/`output_dir` must already be resolved to real
    paths (see queue_common.resolve_path)."""
    from ansys.aedt.core import Hfss

    kwargs = dict(
        project=task["project_file"],
        version=aedt_version_for(aedt_version),
        new_desktop=False,
        student_version=student_version,
        non_graphical=not graphical,
    )
    if task.get("design_name"):
        kwargs["design"] = task["design_name"]
    if task.get("solution_type"):
        kwargs["solution_type"] = task["solution_type"]

    hfss = Hfss(**kwargs)
    try:
        setup_names = list(getattr(hfss, "setup_names", []) or [])
        if not setup_names:
            raise RuntimeError("Analyzed project has no setup to post-process.")

        solver = registry.get(task.get("solution_type"))
        rows = solver.read_results(hfss, setup_names, task, task["output_dir"], log)

        # `rows`, not `modes`: a driven run's rows are frequency points,
        # not modes. Results written before driven runs existed still say
        # `modes`, and results.py reads either.
        return {
            "rows": rows,
            "solution_type": solver.value,
            "row_count": len(rows),
        }
    finally:
        close_project_safely(hfss, log=log)


def copy_tree_verified(source: str, destination: str) -> List[str]:
    """Copy a folder and check every file arrived intact.

    Returns the relative paths that did not make it (empty when the copy
    is good). The caller only deletes the original once this comes back
    empty -- the queue copy is the only copy until then.
    """
    shutil.copytree(source, destination, dirs_exist_ok=True)

    missing = []
    for folder, _, files in os.walk(source):
        for name in files:
            original = os.path.join(folder, name)
            relative = os.path.relpath(original, source)
            copied = os.path.join(destination, relative)
            try:
                same = os.path.getsize(copied) == os.path.getsize(original)
            except OSError:
                same = False
            if not same:
                missing.append(relative)
    return missing


def bring_task_local(run, queue: "queue_common.QueuePaths", combo_id: str, task_id: str,
                     output_dir: str, log: Callable[[str], None]) -> Optional[str]:
    """Move a finished task's files off the share and into the run folder.

    In order, each step only happening because the one before it worked:

      1. copy the remote folder into a staging folder beside the combo,
         and check every file arrived
      2. clear the remote copy, now that a good local one exists
      3. move the staged files into the combo folder, replacing what is
         there

    The staging step is the point of the exercise: a share that drops
    halfway through a copy leaves a half-written staging folder, which is
    thrown away, rather than a half-overwritten combo folder. Nothing
    remote is deleted until the local copy has been verified.

    Returns the combo folder once its files are local, or None if the
    transfer did not complete -- in which case the remote copy is still
    the good one and is left alone.

    Raises the OSError instead when the share itself has gone, rather
    than these files: the laptop's standby drops the network, and a copy
    it cut off is not a failure of that combination. The caller's
    call_with_retry waits for the share and tries again.
    """
    combo_dir = run.combo_dir(combo_id)
    staging = os.path.join(combo_dir, ".incoming")

    shutil.rmtree(staging, ignore_errors=True)
    try:
        missing = copy_tree_verified(output_dir, staging)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if not resilient_io.path_available(queue.root):
            log(f"{combo_id}: the queue went away mid-copy ({exc}); waiting for it to come back.")
            raise
        log(f"{combo_id}: could not copy the analysed files off the queue ({exc}); leaving them there.")
        return None

    if missing:
        log(f"{combo_id}: {len(missing)} file(s) did not copy off the queue "
            f"(e.g. {missing[0]}); leaving the queue copy in place.")
        shutil.rmtree(staging, ignore_errors=True)
        return None

    try:
        queue_common.remove_task(queue, task_id)
        done_task_file = os.path.join(queue.done, f"{task_id}.task.json")
        if os.path.exists(done_task_file):
            os.remove(done_task_file)
    except OSError as exc:
        log(f"{combo_id}: copied locally but could not clear the queue ({exc}).")

    for entry in os.listdir(staging):
        source = os.path.join(staging, entry)
        target = os.path.join(combo_dir, entry)
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        elif os.path.exists(target):
            os.remove(target)
        shutil.move(source, target)
    shutil.rmtree(staging, ignore_errors=True)

    log(f"{combo_id}: analysed files are now in the run folder.")
    return combo_dir


def task_from_run(run, combo_id: str, analysis_settings: dict) -> Dict[str, Any]:
    """The task a combination was queued with, rebuilt from the run's record.

    What `model_builder.queue_model` wrote into it that post-processing
    reads, with the local project in place of the queue's.
    """
    from ..hfss import model_builder

    combo = run.state["combos"][combo_id]
    solver = registry.for_settings(analysis_settings)
    return {
        "task_id": combo["task_id"],
        "project_file": model_builder.project_file_for(run, combo_id),
        "output_dir": run.combo_dir(combo_id),
        "solution_type": solver.value,
        "objects": combo.get("object_names") or {},
        "post_processing": solver.post_processing_ops(analysis_settings),
    }


def find_project_file(folder: str, remote_project_file: str) -> Optional[str]:
    """The local .aedt to open, now that the files have been brought over."""
    expected = os.path.join(folder, os.path.basename(remote_project_file))
    if os.path.exists(expected):
        return expected
    for name in sorted(os.listdir(folder)):
        if name.lower().endswith(".aedt"):
            return os.path.join(folder, name)
    return None


def postprocess_run(
    run,
    queue: "queue_common.QueuePaths",
    analysis_settings: dict,
    log: Callable[[str], None] = logger.info,
    stop_event=None,
    announce_idle: bool = True,
    retry_results: bool = True,
) -> int:
    """For every combo whose worker task finished successfully, reopen its
    analyzed project and extract results -- one at a time, never in
    parallel (see module docstring). Resumable: skips combos already
    post-processed. Returns how many combos it handled, so a caller can
    poll this while analyses are still coming in.

    `announce_idle` is for that polling caller: it would otherwise log
    "nothing to do" on every pass. So is `retry_results=False`, which
    leaves a combo whose files are here but whose results failed for a
    pass that is not polling: retrying it reopens its project in AEDT,
    and one that fails every time would otherwise do so every poll."""
    aedt_version = analysis_settings.get("aedt_version")
    student_version = analysis_settings.get("student_version", True)
    graphical = analysis_settings.get("graphical", False)

    todo = [
        combo_id for combo_id, combo in run.state["combos"].items()
        if combo.get("queue_status") == "done" and combo.get("post_status") != "done"
        and (retry_results or not (combo.get("post_status") == "failed"
                                   and combo.get("fetch_status") == "done"))
    ]
    if not todo:
        if announce_idle:
            log("Nothing to post-process -- every completed combo already has its results.")
        return 0

    log(f"Post-processing {len(todo)} analyzed project(s)...")
    handled = 0
    for combo_id in todo:
        if stop_event is not None and stop_event.is_set():
            return handled

        combo = run.state["combos"][combo_id]
        task_id = combo["task_id"]
        if combo.get("fetch_status") == "done":
            # Already brought over -- and bring_task_local cleared the task
            # off the queue once it had, so the queue has nothing left to
            # read, and a post-processing that failed after the fetch used
            # to be marked "task file not found" and never tried again. It
            # is retried from the local files and the run's own record.
            task = task_from_run(run, combo_id, analysis_settings)
            local_dir = combo.get("analysis_dir") or run.combo_dir(combo_id)
            if not os.path.isdir(local_dir):
                # Checked here, not left to the listing below: that would
                # raise an OSError, which call_with_retry takes for the
                # share being down and waits out forever.
                log(f"{combo_id}: its analysed files are no longer at {local_dir}.")
                run.update_combo(combo_id, post_status="failed",
                                 post_error=f"the analysed files are no longer at {local_dir}")
                handled += 1
                continue
        else:
            task = queue_common.read_task(queue, task_id)
            if task is None:
                log(f"{combo_id}: task {task_id} is no longer in the queue -- skipping.")
                run.update_combo(combo_id, post_status="failed", post_error="task file not found")
                handled += 1
                continue

            remote_output_dir = queue_common.resolve_path(queue, task["output_dir"])

            # Everything is brought over the network first and AEDT is then
            # pointed at the local copy: opening a solved project across a
            # share is slow, and it was what put O:\ paths into the results.
            # Recorded as its own phase, because a solved project coming back
            # over a share is minutes of a combination's life, not a moment.
            log(f"{combo_id}: fetching analysed files for task {task_id}...")
            run.update_combo(combo_id, fetch_status="running")
            local_dir = bring_task_local(run, queue, combo_id, task_id, remote_output_dir, log)
            if local_dir is None:
                run.update_combo(combo_id, fetch_status="failed", post_status="failed",
                                 post_error="could not copy the analysed files off the queue")
                handled += 1
                continue

            run.update_combo(combo_id, fetch_status="done")

        project_file = find_project_file(local_dir, task["project_file"])
        if project_file is None:
            # The copy itself was fine and verified -- there is just
            # nothing in it to read, which is the next phase's failure.
            run.update_combo(combo_id, post_status="failed",
                             post_error=f"no .aedt project in {local_dir}")
            log(f"{combo_id}: no .aedt project among the files that came over.")
            handled += 1
            continue

        task["project_file"] = project_file
        task["output_dir"] = local_dir
        # Named for the same reason the worker's task names it (see
        # model_builder.queue_model): unnamed, PyAEDT misses a driven
        # design, inserts an empty one, and finds no setup to read. A task
        # rebuilt by task_from_run, or queued before the name was sent,
        # has none, so it is read off the project itself.
        if not task.get("design_name"):
            from ..hfss import model_builder
            task["design_name"] = model_builder.design_name_in_project(project_file)

        log(f"{combo_id}: post-processing {os.path.basename(project_file)}...")
        run.update_combo(combo_id, post_status="running")
        try:
            result = postprocess_task(task, aedt_version, student_version, graphical, log)
            client_result = {"status": "success", "task_id": task_id, "combo_id": combo_id, "result": result}
            with open(os.path.join(local_dir, "client_result.json"), "w") as f:
                json.dump(client_result, f, indent=2, default=str)
            run.update_combo(combo_id, post_status="done", post_error=None, analysis_dir=local_dir)
            log(f"{combo_id}: post-processing done ({result['row_count']} result row(s)).")
        except Exception as exc:
            # The files are already local by this point, so a failure here
            # is retryable without going back to the queue.
            logger.exception("Post-processing failed for %s (task %s)", combo_id, task_id)
            run.update_combo(combo_id, post_status="failed", post_error=str(exc),
                             analysis_dir=local_dir)
            log(f"{combo_id}: post-processing failed -- {exc} (its files are in {local_dir}).")
        handled += 1
    return handled
