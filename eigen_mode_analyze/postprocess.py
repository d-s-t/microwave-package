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

from . import solve_types
from .model_builder import close_project_safely
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


def _sweep_solution_name(hfss, setup) -> str:
    """The "<setup> : <sweep>" string for a driven setup's frequency sweep.

    A driven setup has at least two solution entries, not one: its
    adaptive solution ("LastAdaptive"), which holds a single frequency,
    and its frequency sweep, which holds the band. `_solution_name`
    returns whichever comes first and would happily hand back the
    adaptive one -- an S-parameter read off that is a single point, not a
    curve, and nothing about it looks wrong until you plot it.
    """
    candidates = [
        name for name in hfss.existing_analysis_sweeps
        if name.split(" : ")[0] == setup.name and not name.endswith("LastAdaptive")
    ]
    if not candidates:
        raise RuntimeError(
            f"Setup {setup.name} has no frequency sweep solution "
            f"(existing_analysis_sweeps={hfss.existing_analysis_sweeps}). The "
            "adaptive pass may have solved while the sweep did not -- the worker "
            "reports a task as successful either way, because it checks the "
            "adaptive solution. Check the HFSS message manager on the worker "
            "machine."
        )
    return candidates[0]


# What comes back for each port pair, and what the results table calls it.
# Only the first row of a 2-port matrix: S11 is the reflection the run is
# usually about, S21 the transmission, and S22/S12 are the same two
# measured from the other end -- worth having, and cheap, since they are
# already in the solution.
S_PARAMETERS = (
    ("S(1,1)", "s11"),
    ("S(2,1)", "s21"),
    ("S(2,2)", "s22"),
    ("S(1,2)", "s12"),
)


def get_sparameter_results(hfss, setup, log) -> List[Dict[str, Any]]:
    """The S-matrix of a solved driven setup, one row per frequency point.

    Magnitude in dB and phase in degrees for each parameter -- dB because
    that is how a return loss is read, and the phase alongside it because
    without it the rows cannot be turned back into a network.
    """
    if not setup.is_solved:
        raise RuntimeError(
            f"Setup {setup.name} is not solved -- the worker should have solved it "
            "before handing the task back as done. Check the worker's log."
        )

    solution = _sweep_solution_name(hfss, setup)
    available = set(hfss.post.available_report_quantities(solution=solution) or [])
    wanted = [(expr, key) for expr, key in S_PARAMETERS if not available or expr in available]
    if not wanted:
        raise RuntimeError(
            f"{solution} reports no S-parameters at all (available: {sorted(available)}). "
            "The project's ports did not survive into the solved model."
        )

    data = hfss.post.get_solution_data(
        expressions=[expr for expr, _ in wanted],
        setup_sweep_name=solution,
        report_category="Modal Solution Data",
        domain="Sweep",
    )
    if data is None:
        raise RuntimeError(f"No solution data returned for {solution}.")

    frequencies = _frequencies_ghz(data)
    columns: Dict[str, Any] = {}
    for expression, key in wanted:
        for formula, suffix in (("db20", "db"), ("phase", "deg")):
            _, values = data.get_expression_data(expression=expression, formula=formula)
            values = list(values)
            if len(values) != len(frequencies):
                log(
                    f"WARNING: {expression} came back with {len(values)} value(s) for "
                    f"{len(frequencies)} frequencies; dropping it."
                )
                break
            columns[f"{key}_{suffix}"] = values

    rows: List[Dict[str, Any]] = []
    for index, freq_ghz in enumerate(frequencies):
        row = {"freq_ghz": freq_ghz}
        row.update({name: float(values[index]) for name, values in columns.items()})
        rows.append(row)

    log(f"{setup.name}: {len(rows)} frequency point(s) from {solution}, "
        f"{', '.join(sorted(columns))}.")
    return rows


# Sweep units AEDT can report a frequency axis in, as multiples of a GHz.
_FREQUENCY_SCALE_TO_GHZ = {"hz": 1e-9, "khz": 1e-6, "mhz": 1e-3, "ghz": 1.0, "thz": 1e3}


def _frequencies_ghz(data) -> List[float]:
    """The sweep's frequency axis in GHz, whatever unit AEDT reported it in.

    The unit is not fixed: it follows how the sweep was set up, so a
    project made by hand in the UI can hand back Hz where this pipeline's
    own sweeps hand back GHz. Reading the number without the unit is the
    kind of mistake that produces a plot that looks fine and is off by
    nine orders of magnitude.
    """
    values = [float(v) for v in data.primary_sweep_values]
    unit = (data.units_sweeps or {}).get(data.primary_sweep)
    scale = _FREQUENCY_SCALE_TO_GHZ.get(str(unit or "GHz").strip().lower())
    if scale is None:
        raise RuntimeError(
            f"The sweep's frequency axis is in {unit!r}, which this code cannot "
            f"convert to GHz. Known units: {sorted(_FREQUENCY_SCALE_TO_GHZ)}."
        )
    return [value * scale for value in values]


def export_touchstone(hfss, setup, out_dir: str, log) -> Optional[str]:
    """Write the setup's sweep out as a Touchstone file, and say where.

    The results table carries the same numbers, but a .s2p is the format
    every other RF tool reads, so the run leaves one behind rather than
    making its S-parameters only usable from inside this project. Not
    fatal if it fails -- the table is the primary record.
    """
    sweep = _sweep_solution_name(hfss, setup).split(" : ")[1]
    path = os.path.join(out_dir, f"{setup.name}_{sweep}.s2p")
    try:
        # No renormalisation here: the ports were already renormalised to
        # the run's port impedance when they were created, so asking for
        # it again would reference the file to a different impedance than
        # the table beside it.
        written = hfss.export_touchstone(setup=setup.name, sweep=sweep, output_file=path)
    except Exception as exc:
        log(f"WARNING: could not export a Touchstone file for {setup.name}: {exc}")
        return None
    if not written:
        log(f"WARNING: HFSS declined to export a Touchstone file for {setup.name}.")
        return None
    log(f"Touchstone written to {os.path.basename(path)}.")
    return path


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
    """Open the analyzed project (student version) and pull its results out.

    What "results" means depends on what was solved: an eigenmode
    project's modes, each with the task's post_processing ops run against
    it, or a driven project's S-matrix over its frequency sweep. Both come
    back as `rows` -- one dict per result, whatever a result is for that
    solution type -- so everything downstream builds one table the same
    way.

    `task`'s `project_file`/`output_dir` must already be resolved to real
    paths (see queue_common.resolve_path)."""
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
        if solve_types.is_driven(task.get("solution_type")):
            rows = _driven_rows(hfss, setup_names, out_dir, log)
        else:
            rows = _eigenmode_rows(hfss, task, setup_names, out_dir, log)

        # `rows`, not `modes`: a driven run's rows are frequency points,
        # not modes. Results written before driven runs existed still say
        # `modes`, and results.py reads either.
        return {
            "rows": rows,
            "solution_type": task.get("solution_type") or solve_types.EIGENMODE,
            "row_count": len(rows),
        }
    finally:
        close_project_safely(hfss, log=log)


def _eigenmode_rows(hfss, task, setup_names, out_dir: str, log) -> List[Dict[str, Any]]:
    """One row per mode, with the task's post_processing ops run against each."""
    rows: List[Dict[str, Any]] = []
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
            rows.append(row)
    return rows


def _driven_rows(hfss, setup_names, out_dir: str, log) -> List[Dict[str, Any]]:
    """One row per frequency point, plus a Touchstone file per setup.

    No post_processing ops: those are per-mode field quantities, and a
    driven run has no modes to run them against -- `model_builder`
    queues a driven task with an empty ops list for the same reason.
    """
    rows: List[Dict[str, Any]] = []
    for setup_name in setup_names:
        setup = hfss.get_setup(setup_name)
        touchstone = export_touchstone(hfss, setup, out_dir, log)
        for row in get_sparameter_results(hfss, setup, log):
            row["setup"] = setup_name
            if touchstone:
                row["touchstone"] = os.path.basename(touchstone)
            rows.append(row)
    return rows


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
    """
    combo_dir = run.combo_dir(combo_id)
    staging = os.path.join(combo_dir, ".incoming")

    shutil.rmtree(staging, ignore_errors=True)
    try:
        missing = copy_tree_verified(output_dir, staging)
    except OSError as exc:
        log(f"{combo_id}: could not copy the analysed files off the queue ({exc}); leaving them there.")
        shutil.rmtree(staging, ignore_errors=True)
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

        remote_project_file = task["project_file"]
        remote_output_dir = queue_common.resolve_path(queue, task["output_dir"])

        # Everything is brought over the network first and AEDT is then
        # pointed at the local copy: opening a solved project across a
        # share is slow, and it was what put O:\ paths into the results.
        log(f"{combo_id}: fetching analysed files for task {task_id}...")
        local_dir = bring_task_local(run, queue, combo_id, task_id, remote_output_dir, log)
        if local_dir is None:
            run.update_combo(combo_id, post_status="failed",
                             post_error="could not copy the analysed files off the queue")
            handled += 1
            continue

        project_file = find_project_file(local_dir, remote_project_file)
        if project_file is None:
            run.update_combo(combo_id, post_status="failed",
                             post_error=f"no .aedt project in {local_dir}")
            log(f"{combo_id}: no .aedt project among the files that came over.")
            handled += 1
            continue

        task["project_file"] = project_file
        task["output_dir"] = local_dir

        log(f"{combo_id}: post-processing {os.path.basename(project_file)}...")
        try:
            result = postprocess_task(task, aedt_version, student_version, graphical, log)
            client_result = {"status": "success", "task_id": task_id, "combo_id": combo_id, "result": result}
            with open(os.path.join(local_dir, "client_result.json"), "w") as f:
                json.dump(client_result, f, indent=2, default=str)
            run.update_combo(combo_id, post_status="done", analysis_dir=local_dir)
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
