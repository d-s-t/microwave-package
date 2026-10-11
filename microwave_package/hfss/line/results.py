"""
Reading the line-impedance check back out of the solved project.

One row per frequency: the section's S-parameters, the characteristic
impedance DERIVED from them (`impedance.py`), the port's own Zo when the
solved design reports one, and whether the derived value is inside the
tolerance. The verdict for the whole line is `evaluate`d once and written
beside the project (`line_check.json`), which is what the run page shows.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List

from ..driven import results as driven_results
from . import build, impedance

logger = logging.getLogger("microwave_package")

CHECK_FILE = "line_check.json"

# The names HFSS gives the port impedance in a modal solution's reports vary
# with version and port setup; these are the ones looked for, best first.
_PORT_Z_PATTERNS = (r"^zpi", r"^zpv", r"^zvi", r"^z0", r"^zo", r"port ?z")


def _record(out_dir: str) -> Dict[str, Any]:
    try:
        with open(os.path.join(out_dir, build.SECTION_FILE), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def port_impedance_quantities(available) -> List[str]:
    """The report quantities that look like a port impedance, best first."""
    found: List[str] = []
    for pattern in _PORT_Z_PATTERNS:
        found += [q for q in sorted(available) if re.search(pattern, q, re.I) and q not in found]
    return found


def _port_impedance(hfss, solution: str, log) -> Dict[str, Any]:
    """The port's own Zo against frequency, as {column: values} -- empty when
    the design reports none or the read fails. The S-derived impedance
    does not depend on this."""
    try:
        available = list(hfss.post.available_report_quantities(solution=solution) or [])
    except Exception as exc:
        log(f"Could not list the report quantities ({exc}); no port Zo column.")
        return {}
    candidates = port_impedance_quantities(available)
    log(f"Port impedance quantities in this solution: {candidates or 'none'}.")
    for quantity in candidates:
        try:
            data = hfss.post.get_solution_data(
                expressions=[quantity], setup_sweep_name=solution,
                report_category="Modal Solution Data", domain="Sweep")
            if data is None:
                continue
            _f, real = data.get_expression_data(expression=quantity, formula="re")
            columns: Dict[str, Any] = {"zport_ohm": [float(v) for v in real], "zport_quantity": quantity}
            try:
                _f, imag = data.get_expression_data(expression=quantity, formula="im")
                columns["zport_im_ohm"] = [float(v) for v in imag]
            except Exception:
                pass
            return columns
        except Exception as exc:
            log(f"Could not read {quantity}: {exc}")
    return {}


def read_results(hfss, setup_names, task, out_dir: str, log) -> List[Dict[str, Any]]:
    record = _record(out_dir)
    z_ref = float(record.get("port_ohm", impedance.DEFAULT_TARGET_OHM))
    rows: List[Dict[str, Any]] = []
    port_columns: Dict[str, Any] = {}
    for setup_name in setup_names:
        setup = hfss.get_setup(setup_name)
        rows += driven_results.get_sparameter_results(hfss, setup, log, out_dir=out_dir)
        port_columns = _port_impedance(hfss, driven_results._sweep_solution_name(hfss, setup), log)
    rows = impedance.derive_rows(rows, z_ref=z_ref)
    for column in ("zport_ohm", "zport_im_ohm"):
        values = port_columns.get(column)
        if values and len(values) == len(rows):
            for row, value in zip(rows, values):
                row[column] = value
    target = float(record.get("target_ohm", impedance.DEFAULT_TARGET_OHM))
    tolerance = float(record.get("tolerance_ohm", impedance.DEFAULT_TOLERANCE_OHM))
    verdict = impedance.evaluate(rows, target, tolerance)
    verdict.update({k: record.get(k) for k in (
        "line", "geometry_from", "trace_width_mm", "gap_mm", "analytic_gap_mm", "substrate", "eps_r",
        "relief_width_mm", "relief_depth_mm")})
    if port_columns.get("zport_quantity"):
        verdict["port_quantity"] = port_columns["zport_quantity"]
        port_rows = [r for r in rows if r.get("zport_ohm") is not None]
        verdict["port_zo"] = impedance.evaluate(port_rows, target, tolerance, column="zport_ohm")
    with open(os.path.join(out_dir, CHECK_FILE), "w", encoding="utf-8") as handle:
        json.dump(verdict, handle, indent=2, default=str)
    for row in rows:
        row["line"] = record.get("line")
        row["target_ohm"] = target
        z = row.get("z0_ohm")
        row["z0_in_tolerance"] = (abs(z - target) <= tolerance) if z is not None else None
    log(f"Line check ({record.get('line')}): "
        + ("PASS" if verdict.get("passed") else "FAIL")
        + f" -- Zo {verdict.get('min_ohm', float('nan')):.1f}..{verdict.get('max_ohm', float('nan')):.1f} ohm "
        f"against {target:g} +- {tolerance:g}.")
    return rows
