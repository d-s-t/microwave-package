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

import cmath
import logging
import math
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


def get_sparameter_results(hfss, setup, log, out_dir: Optional[str] = None) -> List[Dict[str, Any]]:
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
    groups = _port_groups(available)
    if any(len(members) > 1 for members in groups.values()):
        return _combined_sparameter_rows(hfss, solution, groups, setup, out_dir, log)
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



# --- ports made of several ports ---------------------------------------
#
# A CPW on a die with no backing ground is excited across its two gaps,
# one port each (see ports._gap_ports), so the project has four ports and
# the run is about two. The pair at each end is one port: both are driven
# alike, with the same voltage across them, so their currents add. Built
# to twice the run's impedance, two of them in parallel are one port of
# the run's impedance, and the power waves of the pair are
#
#     a_end = (a_1 + a_2) / sqrt(2),     b_end = (b_1 + b_2) / sqrt(2)
#
# which makes the combined S-parameter the sum of the member entries
# divided by sqrt(members_i * members_j) -- half their sum for two and two.

_PORT_NAME = re.compile(r"^(.*?)(\d+)(?:_[A-Za-z]+)?$")


def _port_groups(available) -> Dict[str, List[str]]:
    """The solved project's ports, grouped by which end they belong to.

    `port_1`, `port_2` are a group each of one; `port_1_a`, `port_1_b`
    are one group of two, numbered 1. Names that do not end in a number
    (optionally followed by a letter suffix) are left out.
    """
    groups: Dict[str, List[str]] = {}
    for quantity in available:
        match = _S_QUANTITY.match(quantity)
        if not match:
            continue
        for port in match.groups():
            port = port.strip()
            named = _PORT_NAME.match(port)
            if named:
                members = groups.setdefault(str(int(named.group(2))), [])
                if port not in members:
                    members.append(port)
    return {number: sorted(members) for number, members in sorted(groups.items())}


def combined_s_parameters(entries: Dict[tuple, Any], groups: Dict[str, List[str]]) -> Dict[tuple, Any]:
    """Combine member S-parameters into one per pair of ends.

    `entries` maps (receiving port, driving port) to a complex value or
    an array of them; the result maps (receiving end, driving end) --
    the numbers `groups` is keyed by -- to the combined value.
    """
    combined = {}
    for row, row_members in groups.items():
        for column, column_members in groups.items():
            total = sum(entries[(p, q)] for p in row_members for q in column_members)
            combined[(row, column)] = total / math.sqrt(len(row_members) * len(column_members))
    return combined


def _db_and_degrees(value: complex) -> tuple:
    return 20.0 * math.log10(max(abs(value), 1e-300)), math.degrees(cmath.phase(value))


def _combined_sparameter_rows(hfss, solution: str, groups: Dict[str, List[str]],
                              setup, out_dir: Optional[str], log) -> List[Dict[str, Any]]:
    """The same rows `get_sparameter_results` makes, from a multi-port
    project: each end's ports combined into one."""
    members = [port for ports in groups.values() for port in ports]
    expressions = [f"S({p},{q})" for p in members for q in members]
    data = hfss.post.get_solution_data(
        expressions=expressions, setup_sweep_name=solution,
        report_category="Modal Solution Data", domain="Sweep",
    )
    if data is None:
        raise RuntimeError(f"No solution data returned for {solution}.")
    frequencies = _frequencies_ghz(data)

    raw: Dict[tuple, List[complex]] = {}
    for p in members:
        for q in members:
            expression = f"S({p},{q})"
            _, real = data.get_expression_data(expression=expression, formula="re")
            _, imag = data.get_expression_data(expression=expression, formula="im")
            values = [complex(a, b) for a, b in zip(real, imag)]
            if len(values) != len(frequencies):
                raise RuntimeError(
                    f"{expression} came back with {len(values)} value(s) for "
                    f"{len(frequencies)} frequencies."
                )
            raw[(p, q)] = values
    # Combined point by point: the sum is over ports, the frequency stays.
    combined = {}
    for index in range(len(frequencies)):
        point = combined_s_parameters({k: v[index] for k, v in raw.items()}, groups)
        for key, value in point.items():
            combined.setdefault(key, []).append(value)

    rows: List[Dict[str, Any]] = []
    for index, freq_ghz in enumerate(frequencies):
        row = {"freq_ghz": freq_ghz}
        for (receiving, driving), key in (
            (("1", "1"), "s11"), (("2", "1"), "s21"), (("2", "2"), "s22"), (("1", "2"), "s12"),
        ):
            if (receiving, driving) in combined:
                db, degrees = _db_and_degrees(combined[(receiving, driving)][index])
                row[f"{key}_db"], row[f"{key}_deg"] = db, degrees
        rows.append(row)

    if out_dir and all(key in combined for key in (("1", "1"), ("2", "1"), ("1", "2"), ("2", "2"))):
        write_touchstone_2port(
            os.path.join(out_dir, f"{setup.name}_{solution.split(' : ')[1]}.s2p"),
            frequencies, combined,
        )
    log(f"{setup.name}: {len(rows)} frequency point(s) from {solution}, with each end's "
        f"{', '.join(f'{len(ports)} ports' for ports in groups.values())} combined into one.")
    return rows


def write_touchstone_2port(path: str, frequencies_ghz, combined: Dict[tuple, List[complex]],
                           impedance: float = 50.0) -> None:
    """A 2-port Touchstone file (real/imaginary) of the combined matrix."""
    lines = [f"# GHz S RI R {impedance:g}", "! S-parameters of the ends' ports combined into one each"]
    for index, freq in enumerate(frequencies_ghz):
        fields = [f"{freq:.9g}"]
        for key in (("1", "1"), ("2", "1"), ("1", "2"), ("2", "2")):
            value = combined[key][index]
            fields += [f"{value.real:.9g}", f"{value.imag:.9g}"]
        lines.append(" ".join(fields))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


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
    try:
        port_count = len(hfss.excitation_names)
    except Exception:
        port_count = 2
    # A project whose ends are each several ports writes the whole matrix
    # here; the 2-port file of the combined ends is `get_sparameter_results`'s.
    stem = f"{setup.name}_{sweep}" if port_count == 2 else f"{setup.name}_{sweep}_all_ports"
    path = os.path.join(out_dir, f"{stem}.s{port_count}p")
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
        for row in get_sparameter_results(hfss, setup, log, out_dir=out_dir):
            row["setup"] = setup_name
            # The 2-port file is the one the rows describe, when a
            # multi-port project wrote one beside its full matrix.
            sweep = _sweep_solution_name(hfss, setup).split(" : ")[1]
            combined = os.path.join(out_dir, f"{setup.name}_{sweep}.s2p")
            if os.path.isfile(combined):
                row["touchstone"] = os.path.basename(combined)
            elif touchstone:
                row["touchstone"] = os.path.basename(touchstone)
            rows.append(row)
    return rows
