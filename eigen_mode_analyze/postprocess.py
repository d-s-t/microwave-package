"""
Client-side post-processing: once the worker reports a task as done (it
only ever ran the project's one existing setup -- see
ansys-analyze-worker's handlers/eigenmode_analyze.py), this reopens the
now-solved project itself and pulls every bit of data the run needs
straight out of it: mode frequencies, Q, and the field plots configured
in the task's `post_processing` list. All of that used to happen inside
the worker, right after analyze() -- it's here now because none of it
needs the full license, only the worker's analyze() call does (see
ansys-analyze-worker's ARCHITECTURE.md section 12).

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
import re
import shutil
from typing import Any, Callable, Dict, List, Optional

from ansys_analyze_common import queue_common

from .post_processing import run_post_processing

logger = logging.getLogger("eigen_mode_analyze")


def _solution_name(hfss, setup) -> str:
    """The "<setup> : LastAdaptive" string that identifies this setup's
    solution in every post-processing call -- see the note in
    ansys-analyze-worker's ARCHITECTURE.md section 11 on why this can't
    just be assumed/constructed from the setup name alone."""
    for name in hfss.existing_analysis_sweeps:
        if name.split(" : ")[0] == setup.name:
            return name
    raise RuntimeError(
        f"Setup {setup.name} has no solution entry in the design "
        f"(existing_analysis_sweeps={hfss.existing_analysis_sweeps})."
    )


def _mode_index(expression: str) -> int:
    """1 out of 'Mode(1)' / 'Q(1)'."""
    match = re.search(r"\((\d+)\)", expression)
    if not match:
        raise RuntimeError(f"Cannot read a mode number out of report quantity {expression!r}.")
    return int(match.group(1))


def get_eigenmode_results(hfss, setup, log) -> List[Dict[str, Any]]:
    """Resonant frequency and Q for every mode of the solved Eigenmode setup."""
    if not setup.is_solved:
        raise RuntimeError(
            f"Setup {setup.name} is not solved -- the worker should have solved it "
            "before handing the task back as done. Check the worker's log."
        )

    solution = _solution_name(hfss, setup)

    mode_exprs = hfss.post.available_report_quantities(solution=solution)
    q_exprs = hfss.post.available_report_quantities(solution=solution, quantities_category="Eigen Q")
    if not mode_exprs:
        return []

    data = hfss.post.get_solution_data(
        expressions=mode_exprs + q_exprs,
        setup_sweep_name=solution,
        report_category="Eigenmode Parameters",
    )
    if data is None:
        raise RuntimeError(f"No solution data returned for {solution}.")

    def first_real(expression):
        if expression not in data.expressions:
            return None
        _, y = data.get_expression_data(expression=expression, formula="real")
        values = list(y)
        return float(values[0]) if values else None

    qs = {_mode_index(e): first_real(e) for e in q_exprs}

    results: List[Dict[str, Any]] = []
    for mode_expr in mode_exprs:
        mode = _mode_index(mode_expr)
        freq_hz = first_real(mode_expr)
        if freq_hz is None:
            log(f"{setup.name}: no data for {mode_expr!r}; skipping.")
            continue
        results.append({
            "mode": mode,
            "freq_hz": freq_hz,
            "freq_ghz": freq_hz / 1e9,
            "q": qs.get(mode),
        })

    results.sort(key=lambda r: r["freq_hz"])
    return results


def get_convergence_data(setup):
    try:
        return setup.props.get("MaxDeltaFreq", "N/A")
    except Exception:
        return "N/A"


def postprocess_task(
    task: Dict[str, Any],
    aedt_version,
    student_version: bool,
    graphical: bool,
    log: Callable[[str], None],
) -> Dict[str, Any]:
    """Open the analyzed project (student version), extract eigenmode
    results, and run the task's post_processing ops per mode. `task`'s
    `project_file`/`output_dir` must already be resolved to real paths
    (see queue_common.resolve_path)."""
    from ansys.aedt.core import Hfss

    kwargs = dict(
        project=task["project_file"],
        version=aedt_version,
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

        out_dir = task["output_dir"]
        modes_data: List[Dict[str, Any]] = []
        for setup_name in setup_names:
            setup = hfss.get_setup(setup_name)
            eigen_results = get_eigenmode_results(hfss, setup, log)
            conv_error = str(get_convergence_data(setup))

            for mode_result in eigen_results:
                row = {
                    "setup": setup_name,
                    "mode": mode_result["mode"],
                    "freq_ghz": mode_result["freq_ghz"],
                    "q": mode_result["q"],
                    "convergence_error": conv_error,
                }
                mode_out_dir = os.path.join(out_dir, f"{setup_name}_Mode_{mode_result['mode']}")
                os.makedirs(mode_out_dir, exist_ok=True)
                pp_result = run_post_processing(
                    hfss, task, {"setup_name": setup_name, "mode": mode_result["mode"]}, mode_out_dir,
                )
                row.update(pp_result)
                modes_data.append(row)

        return {"modes": modes_data, "num_modes_found": len(modes_data)}
    finally:
        try:
            if hfss is not None and getattr(hfss, "project_name", None):
                hfss.close_project(name=hfss.project_name)
        except Exception:
            log("Failed to close project cleanly after post-processing.")


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


def archive_task(run, queue: "queue_common.QueuePaths", combo_id: str, task_id: str,
                 output_dir: str, log: Callable[[str], None]) -> Optional[str]:
    """Move a finished task's files out of the queue and into the run.

    The queue is working space: once everything a task produced is safely
    in the run folder, its files there are removed. Nothing is deleted
    unless the copy verified, so a failed transfer leaves the queue copy
    as the surviving one.
    """
    archive_dir = os.path.join(run.combo_dir(combo_id), "analysis")
    try:
        missing = copy_tree_verified(output_dir, archive_dir)
    except OSError as exc:
        log(f"{combo_id}: could not copy analysis files out of the queue ({exc}); leaving them there.")
        return None

    if missing:
        log(f"{combo_id}: {len(missing)} file(s) did not copy out of the queue "
            f"(e.g. {missing[0]}); leaving the queue copy in place.")
        return None

    try:
        queue_common.remove_task(queue, task_id)
        done_task_file = os.path.join(queue.done, f"{task_id}.task.json")
        if os.path.exists(done_task_file):
            os.remove(done_task_file)
    except OSError as exc:
        log(f"{combo_id}: copied to the run folder but could not clear the queue ({exc}).")

    log(f"{combo_id}: analysis files moved into the run folder.")
    return archive_dir


def postprocess_run(
    run,
    queue: "queue_common.QueuePaths",
    analysis_settings: dict,
    log: Callable[[str], None] = logger.info,
    stop_event=None,
    announce_idle: bool = True,
) -> int:
    """For every combo whose worker task finished successfully, reopen its
    analyzed project and extract results -- one at a time, never in
    parallel (see module docstring). Resumable: skips combos already
    post-processed. Returns how many combos it handled, so a caller can
    poll this while analyses are still coming in.

    `announce_idle` is for that polling caller: it would otherwise log
    "nothing to do" on every pass."""
    aedt_version = analysis_settings.get("aedt_version")
    student_version = analysis_settings.get("student_version", True)
    graphical = analysis_settings.get("graphical", False)

    todo = [
        combo_id for combo_id, combo in run.state["combos"].items()
        if combo.get("queue_status") == "done" and combo.get("post_status") != "done"
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

        task_id = run.state["combos"][combo_id]["task_id"]
        task = queue_common.read_task(queue, task_id)
        if task is None:
            log(f"{combo_id}: task {task_id} is no longer in the queue -- skipping.")
            run.update_combo(combo_id, post_status="failed", post_error="task file not found")
            handled += 1
            continue

        task["project_file"] = queue_common.resolve_path(queue, task["project_file"])
        task["output_dir"] = queue_common.resolve_path(queue, task["output_dir"])

        log(f"{combo_id}: post-processing task {task_id}...")
        try:
            result = postprocess_task(task, aedt_version, student_version, graphical, log)
            client_result = {"status": "success", "task_id": task_id, "combo_id": combo_id, "result": result}
            with open(os.path.join(task["output_dir"], "client_result.json"), "w") as f:
                json.dump(client_result, f, indent=2, default=str)
            run.update_combo(combo_id, post_status="done")
            log(f"{combo_id}: post-processing done ({result['num_modes_found']} mode(s)).")
            archive_dir = archive_task(run, queue, combo_id, task_id, task["output_dir"], log)
            if archive_dir:
                run.update_combo(combo_id, analysis_dir=archive_dir)
        except Exception as exc:
            # The queue copy is deliberately left alone here: a combo that
            # failed is the one you want to be able to open and look at.
            logger.exception("Post-processing failed for %s (task %s)", combo_id, task_id)
            run.update_combo(combo_id, post_status="failed", post_error=str(exc))
            log(f"{combo_id}: post-processing failed -- {exc} (its files are kept in the queue).")
        handled += 1
    return handled
