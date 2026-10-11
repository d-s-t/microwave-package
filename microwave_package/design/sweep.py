"""
Sweep configuration: which equation-file parameters to vary, over what
values, and the cartesian-product combinations that produces -- one
combination becomes one STEP file / one AEDT model / one queued task.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

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


def pin_bare_board_pedestal(
    sweep_params: List[SweepParameter],
    parameter_defaults: Optional[Dict[str, float]],
    base_values: Dict[str, float],
) -> Dict[str, float]:
    """`parameter_defaults`, with `pedestal_depth` pinned to 0 when every
    combination of the run is a bare board and nothing says otherwise.

    The pedestal is a recess in the box floor under the die, where an
    ordinary board's bottom ground plane has its opening. A bare board has
    no die and its plane is whole, so a recess would run into it (AEDT: "box
    and pcb_copper_bottom intersect"). Zero is the value the box rebuilds
    cleanly with. A value typed or swept for it still wins, and a run that
    mixes bare and ordinary boards (`no_chip` swept) is left alone: the
    layout check refuses the bare ones that kept a pedestal, saying why.
    `base_values` is the equations file's own numbers.
    """
    defaults = dict(parameter_defaults or {})
    if "pedestal_depth" not in base_values or "pedestal_depth" in defaults:
        return defaults
    if any(p.name == "pedestal_depth" for p in sweep_params):
        return defaults
    no_chip = next((list(p.values) for p in sweep_params if p.name == "no_chip"), None)
    if no_chip is None:
        no_chip = [defaults.get("no_chip", base_values.get("no_chip", 0.0))]
    if no_chip and all(float(v) > 0.5 for v in no_chip):
        defaults["pedestal_depth"] = 0.0
    return defaults


def combinations(
    sweep_params: List[SweepParameter],
    parameter_defaults: Optional[Dict[str, float]] = None,
) -> List[Dict[str, float]]:
    """Cartesian product across every swept parameter's values.

    With nothing swept this is a single combination with no varying
    parameters -- a run that only pins defaults still builds one model,
    rather than none at all.

    The distortion's own geometry is the exception: those parameters only
    mean anything where the distortion is actually built, so they expand
    the distorted combinations alone rather than every combination. See
    `_expand_distortion_geometry`. `parameter_defaults` is what the run
    pinned, which is where the distortion flag sits when it is not itself
    being swept.
    """
    geometry = [p for p in sweep_params if p.name in parameter_meta.DISTORTION_GEOMETRY_PARAMETERS]
    rest = [p for p in sweep_params if p.name not in parameter_meta.DISTORTION_GEOMETRY_PARAMETERS]

    if not rest:
        base = [{}]
    else:
        names = [p.name for p in rest]
        value_lists = [p.values for p in rest]
        base = [dict(zip(names, combo)) for combo in itertools.product(*value_lists)]

    if not geometry:
        return base
    return _expand_distortion_geometry(base, geometry, parameter_defaults or {})


def is_distorted(combo: Dict[str, float], parameter_defaults: Dict[str, float]) -> bool:
    """Whether this combination builds the distorted geometry.

    The flag is in the combo itself when the run sweeps both variants,
    and in the run's pinned values when it does not. Absent from both --
    which only happens if the equations file has no such flag -- means
    the undistorted model, matching the file's own default.
    """
    flag = combo.get(parameter_meta.DISTORTION_PARAM,
                     parameter_defaults.get(parameter_meta.DISTORTION_PARAM, 1))
    return float(flag) == 0


def _expand_distortion_geometry(
    base: List[Dict[str, float]], geometry: List[SweepParameter],
    parameter_defaults: Dict[str, float],
) -> List[Dict[str, float]]:
    """Multiply out the distortion geometry, but only over the distorted
    combinations.

    A run doing both variants of a two-value PCB length, sweeping the
    distortion radius over two values, gets 2 clean combinations (one per
    PCB length) and 4 distorted ones (per PCB length, per radius) -- not
    8. Sweeping the geometry of a run with no distorted combination at
    all adds nothing, and the caller warns about that rather than
    silently multiplying the run out.
    """
    names = [p.name for p in geometry]
    value_lists = [p.values for p in geometry]
    products = [dict(zip(names, values)) for values in itertools.product(*value_lists)]

    expanded = []
    for combo in base:
        if not is_distorted(combo, parameter_defaults):
            expanded.append(combo)
            continue
        expanded.extend({**combo, **product} for product in products)
    return expanded


def combo_id_for(combo: Dict[str, float]) -> str:
    """A filesystem-safe, human-readable id for one combination, e.g.
    "pedestal_depth-4_chip_width-381um", used as its folder name.

    Values are written in the unit the parameter is presented in, so the
    id matches what was typed into the form -- the sub-millimetre ones
    carry a `um` suffix to say so. The combo's own values stay in
    millimetres; only this label changes. The distortion flag is spelled
    out as "with_distortion" / "without_distortion" rather than 0 / 1
    (see parameter_meta.combo_id_part).
    """
    parts = [parameter_meta.combo_id_part(name, value) for name, value in combo.items()]
    return "_".join(parts) if parts else "default"
