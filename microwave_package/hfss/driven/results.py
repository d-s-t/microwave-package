"""
Reading a driven run's S-matrix back out of a solved project.

One row per frequency point -- magnitude in dB and phase in degrees for
each port pair -- plus a Touchstone file, so the numbers are usable
outside this project as well as inside it.

Two things here are silent when they go wrong, which is why both are
deliberate rather than assumed: which of a driven setup's solutions the
S-parameters are read from, and what unit its frequency axis is in.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("microwave_package")


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
    wanted = _wanted_expressions(available)
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


_S_QUANTITY = re.compile(r"^S\((.+),(.+)\)$")


def _wanted_expressions(available) -> List[tuple]:
    """S_PARAMETERS as the solution actually names them.

    AEDT names each S-parameter after its ports: "S(port_2,port_1)" for
    ports called port_1/port_2, "S(2,1)" only when the ports are called
    1/2. So each numbered expression is matched to the port whose name
    ends in that number. With nothing available to check against, the
    numbered names are asked for as they are.
    """
    if not available:
        return list(S_PARAMETERS)
    port_by_number: Dict[str, str] = {}
    for quantity in available:
        match = _S_QUANTITY.match(quantity)
        if not match:
            continue
        for port in match.groups():
            number = re.search(r"(\d+)$", port.strip())
            if number:
                port_by_number.setdefault(str(int(number.group(1))), port.strip())
    wanted = []
    for expr, key in S_PARAMETERS:
        row, col = _S_QUANTITY.match(expr).groups()
        named = f"S({port_by_number.get(row, row)},{port_by_number.get(col, col)})"
        if named in available:
            wanted.append((named, key))
        elif expr in available:
            wanted.append((expr, key))
    return wanted


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


def read_results(hfss, setup_names, task, out_dir: str, log) -> List[Dict[str, Any]]:
    """Every frequency point of every setup, as one row each, plus a
    Touchstone file per setup.

    No post_processing ops run here: those are per-mode field quantities
    and a driven run has no modes to run them against, which is why this
    solver asks for none. The signature is the solver contract's (see
    hfss/registry.py), so `task` is here whether it is used or not.
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
