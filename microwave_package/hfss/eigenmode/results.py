"""
Reading an Eigenmode run's answer back out of a solved project.

One row per mode: its resonant frequency and its Q, with whatever
per-mode field statistics and plots the task asked for run against it.

Read-only export from solution data already on disk, which the student
licence is perfectly able to do -- see ansys-analyze-worker's
ARCHITECTURE.md section 12 for why this is client-side rather than part
of the worker.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List

from .fields import run_post_processing

logger = logging.getLogger("microwave_package")


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


def read_results(hfss, setup_names, task, out_dir: str, log) -> List[Dict[str, Any]]:
    """Every mode of every setup, as one row each.

    The signature is the solver contract's (see hfss/registry.py), which
    is why `task` and `out_dir` are here even though only the field ops
    use them.
    """
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
