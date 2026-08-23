"""
Sweep configuration: which equation-file parameters to vary, over what
values, and the cartesian-product combinations that produces -- one
combination becomes one STEP file / one AEDT model / one queued task.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

from . import parameter_meta


@dataclass
class SweepParameter:
    name: str
    values: List[float]


def expand_values(spec: str) -> List[float]:
    """
    Parse a range specification into an explicit list of floats:

      "4"             -> [4.0]                      a single value
      "0,2,4,6,8,10"  -> explicit comma-separated list
      "0:10:2"        -> start:stop:step, INCLUSIVE of stop when it
                          lands exactly on a step (like the worker
                          repo's --runs slice syntax, but for float
                          ranges rather than list indices)
    """
    spec = spec.strip()
    if not spec:
        return []
    if ":" in spec:
        parts = spec.split(":")
        if len(parts) not in (2, 3):
            raise ValueError(f"Invalid range {spec!r} -- expected start:stop[:step]")
        start = float(parts[0])
        stop = float(parts[1])
        step = float(parts[2]) if len(parts) == 3 else 1.0
        if step == 0:
            raise ValueError(f"Invalid range {spec!r} -- step cannot be 0")
        count = int(round((stop - start) / step))
        if count < 0:
            raise ValueError(f"Invalid range {spec!r} -- stop is not reachable from start with this step")
        return [round(start + i * step, 10) for i in range(count + 1)]
    return [float(x.strip()) for x in spec.split(",") if x.strip() != ""]


def split_swept_and_fixed(
    params: List[SweepParameter],
) -> Tuple[List[SweepParameter], Dict[str, float]]:
    """Separate the parameters that actually vary from the ones pinned to
    a single value.

    A parameter given only one value ("4", or a range that expands to a
    single point) is not a sweep dimension -- multiplying the cartesian
    product by 1 just adds its value to every combo's name. It is instead
    treated as a new default for the whole run: applied to every
    combination, but not part of what distinguishes them.
    """
    swept = [p for p in params if len(p.values) > 1]
    fixed = {p.name: p.values[0] for p in params if len(p.values) == 1}
    return swept, fixed


def combinations(sweep_params: List[SweepParameter]) -> List[Dict[str, float]]:
    """Cartesian product across every swept parameter's values.

    With nothing swept this is a single combination with no varying
    parameters -- a run that only pins defaults still builds one model,
    rather than none at all.
    """
    if not sweep_params:
        return [{}]
    names = [p.name for p in sweep_params]
    value_lists = [p.values for p in sweep_params]
    return [dict(zip(names, combo)) for combo in itertools.product(*value_lists)]


def combo_id_for(combo: Dict[str, float]) -> str:
    """A filesystem-safe, human-readable id for one combination, e.g.
    "pedestal_depth-4_chip_width-381um", used as its folder name.

    Values are written in the unit the parameter is presented in, so the
    id matches what was typed into the form -- the sub-millimetre ones
    carry a `um` suffix to say so. The combo's own values stay in
    millimetres; only this label changes.
    """
    parts = []
    for name, value in combo.items():
        safe_name = re.sub(r"[^A-Za-z0-9_]+", "_", name)
        safe_value = re.sub(r"[^A-Za-z0-9.]+", "_", parameter_meta.combo_id_value(name, value))
        parts.append(f"{safe_name}-{safe_value}")
    return "_".join(parts) if parts else "default"
