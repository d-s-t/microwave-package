"""
Which fields a field-image part asks the worker for, and where.

The planes are read off the model, not typed in: the substrate, the vacuum
and the box are taken from their bounding boxes while the model is open
(see `plan_for_model`), so a board of another size gets planes of its own.
Each export is one entry of the task's `field_exports` (the worker's
ARCHITECTURE.md section 3): a grid in millimetres, the expression |E| or
|H| (both scalars), and the setup's adaptive solution at the part's
frequency. The worker writes each to fields/<name>.fld, and
workflow/field_images.py draws them.

The grids, for the substrate's extent (x along the board, y up, z across):

  substrate_mid   a plane through the middle of the board's thickness
  substrate_top   a plane 0.5 mm above the board's top face
  cavity_mid      a plane through the middle of the air above the board
  section_xy      a vertical section along the line, through the middle of the width
  vacuum_volume   the whole air region, on a coarse 1 mm grid

Every grid is written for each quantity asked for (E, H).
"""
from __future__ import annotations

import logging
import math
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import setup as driven_setup

logger = logging.getLogger("microwave_package")

FIELD_QUANTITIES = {"E": "Mag_E", "H": "Mag_H"}
PLANE_STEP_MM = 0.25   # in each plane, and in the vertical section
NORMAL_STEP_MM = 1.0   # across a plane: a plane is one point deep, but the worker still needs a step
VOLUME_STEP_MM = 1.0   # the coarse volume
ABOVE_BOARD_MM = 0.5   # the second plane sits this far above the board's top face

# The model's length units, as they convert to millimetres.
_TO_MM = {"mm": 1.0, "um": 0.001, "cm": 10.0, "meter": 1000.0, "m": 1000.0, "in": 25.4, "mil": 0.0254}

# The objects a field part needs, by their key in the model's `objects`
# (see model_builder.MATERIAL_RULES) and the role each plays here.
_ROLE_KEYS = (("substrate", "pcb"), ("vacuum", "vacuum"), ("box", "box"))


def solution_name() -> str:
    """The solution a field export reads: the part's one adaptive pass."""
    return f"{driven_setup.SETUP_NAME} : LastAdaptive"


def _check_box(role: str, box: Sequence[float]) -> None:
    if (len(box) != 6 or any(not math.isfinite(v) for v in box)
            or box[3] <= box[0] or box[4] <= box[1] or box[5] <= box[2]):
        raise ValueError(f"The {role} bounding box {list(box)} is not a box with volume.")


def sources_for(port_names: Sequence[str], available: Sequence[str]) -> Tuple[Dict[str, list], List[str]]:
    """The excitations that drive the field export: port 1 at 1 W, and every
    other excitation in the project at 0 W.

    "Every other" matters: on the launch, bonds and chip planes the lumped
    ports and the connectors' terminations are excitations too, so leaving
    them to AEDT's defaults would add their fields to port 1's. Names are the
    ones AEDT reports for the open project ("port_1:1", or "port_1").

    Returns `(sources, missing)`. The sources are only given when port 1's
    excitation is in the project; otherwise `sources` is empty and `missing`
    names the ports whose excitation was not found, so the export is not sent
    with a name the project does not have.
    """
    if not port_names:
        return {}, []
    names = list(dict.fromkeys(available))

    def name_of(port: str) -> Optional[str]:
        for candidate in (f"{port}:1", port):
            if candidate in names:
                return candidate
        return None

    missing = [port for port in port_names if name_of(port) is None]
    driven = name_of(port_names[0])
    if missing or driven is None:
        return {}, missing
    return {name: ["1W", "0deg"] if name == driven else ["0W", "0deg"] for name in names}, []


def plan_exports(boxes_mm: Dict[str, Sequence[float]], frequency_ghz: float,
                 quantities: Sequence[str], sources: Optional[Dict[str, list]] = None) -> List[dict]:
    """The `field_exports` entries for one frequency, from the boxes of the model.

    `boxes_mm` maps "substrate", "vacuum" and "box" to bounding boxes
    `[xmin, ymin, zmin, xmax, ymax, zmax]` in millimetres. Raises ValueError
    when a box is not one, when the vacuum does not reach above the board,
    or when a quantity is not one of E and H.
    """
    for role, _key in _ROLE_KEYS:
        _check_box(role, boxes_mm[role])
    quantities = list(dict.fromkeys(quantities))
    unknown = [q for q in quantities if q not in FIELD_QUANTITIES]
    if not quantities or unknown:
        raise ValueError(f"A field export asks for E and/or H, not {unknown or 'nothing'}.")

    sub, vac, box = boxes_mm["substrate"], boxes_mm["vacuum"], boxes_mm["box"]
    if vac[4] <= sub[4]:
        raise ValueError("The vacuum does not reach above the board, so there is no air above it to take a plane from.")
    mid_y = (sub[1] + sub[4]) / 2.0
    top_y = sub[4] + ABOVE_BOARD_MM
    cavity_y = (sub[4] + vac[4]) / 2.0
    width_z = (sub[2] + sub[5]) / 2.0
    in_plane = [PLANE_STEP_MM, NORMAL_STEP_MM, PLANE_STEP_MM]

    grids = [
        ("substrate_mid", [sub[0], mid_y, sub[2]], [sub[3], mid_y, sub[5]], in_plane),
        ("substrate_top", [sub[0], top_y, sub[2]], [sub[3], top_y, sub[5]], in_plane),
        ("cavity_mid", [vac[0], cavity_y, vac[2]], [vac[3], cavity_y, vac[5]], in_plane),
        ("section_xy", [box[0], box[1], width_z], [box[3], box[4], width_z],
         [PLANE_STEP_MM, PLANE_STEP_MM, NORMAL_STEP_MM]),
        ("vacuum_volume", list(vac[:3]), list(vac[3:]), [VOLUME_STEP_MM] * 3),
    ]
    frequency = driven_setup.ghz_text(frequency_ghz)
    entries = []
    for label, start, stop, step in grids:
        for letter in quantities:
            entry = {
                "name": f"{label}_{letter}",
                "solution": solution_name(),
                "intrinsics": {"Freq": frequency, "Phase": "0deg"},
                "expression": [FIELD_QUANTITIES[letter]],
                "grid": {"type": "Cartesian",
                         "start": [round(v, 6) for v in start],
                         "stop": [round(v, 6) for v in stop],
                         "step": step, "units": "mm"},
            }
            if sources:
                entry["sources"] = {name: list(value) for name, value in sources.items()}
            entries.append(entry)
    return entries


def plan_for_model(hfss, objects: Dict[str, object], port_names: Sequence[str],
                   frequency_ghz: float, quantities: Sequence[str],
                   log: Callable[[str], None] = logger.info) -> List[dict]:
    """The `field_exports` entries for a model that is open in `hfss` and has
    just been given its setup.

    `objects` is the model's own map of role keys to objects (the one
    `assign_materials_and_boundaries` returns): the board is "pcb", the air
    "vacuum" and the box "box". Their bounding boxes are read here, while the
    project is open, and converted from the model's units to millimetres.
    """
    units = hfss.modeler.model_units
    if units not in _TO_MM:
        raise ValueError(f"The model is in {units!r}, which cannot be converted to millimetres for the field grids.")
    boxes = {}
    for role, key in _ROLE_KEYS:
        obj = objects.get(key)
        if obj is None:
            raise ValueError(f"The model has no {key!r} object, so the field grids cannot be placed.")
        boxes[role] = [round(v * _TO_MM[units], 6) for v in obj.bounding_box]

    sources, missing = sources_for(port_names, list(hfss.excitation_names))
    if missing:
        log(f"WARNING: the project has no excitation for {', '.join(missing)} under the name "
            "the field exports use, so they take the project's own excitations instead.")
    entries = plan_exports(boxes, frequency_ghz, quantities, sources)
    log(f"Field exports planned: {len(entries)} grid(s) for {', '.join(quantities)}, "
        f"substrate {boxes['substrate']}, vacuum {boxes['vacuum']} mm.")
    return entries
