"""
How each sweep parameter is presented in the web UI: a readable name, a
unit, and whether it is a whole number.

The equation-file names are the source of truth everywhere else in the
pipeline, but they are not what you want to read in a form -- and a few
of them are actively misleading ("pcb_width" and "chip_width" are
thicknesses, not widths). Only the labels live here; nothing in this
module affects the geometry or the analysis.

To rename something, edit LABELS -- the key is the equation-file name and
must keep matching `base_parts/default.txt` (and the drawing file named
after it in `base_parts/parameters_visual/`).
"""
from __future__ import annotations

from typing import Dict, Optional

# Equation-file name -> what the web UI calls it.
LABELS: Dict[str, str] = {
    "pcb_length": "PCB length",
    "pcb_side": "PCB width",
    "pcb_width": "PCB thickness",
    "wall_width": "Box wall thickness",
    "route_width": "Routing track width",
    "route_clearence": "Routing clearance",
    "route_chip_clearence": "Routing-to-chip clearance",
    "port_count": "Number of ports",
    "port_space": "Port spacing",
    "port_length": "Port length",
    "port_width": "Port width",
    "chip_length": "Chip length",
    "chip_side": "Chip width",
    "chip_width": "Chip thickness",
    "pedestal_depth": "Pedestal depth",
    "chip_leg": "Chip leg height",
    "wing_length": "Wing length",
    "wing_width": "Wing width",
    "bonding_clearence": "Bonding clearance",
}

# Counts rather than lengths: shown without a unit and as whole numbers.
INTEGER_PARAMETERS = {"port_count"}

# Everything else in this model is a length in millimetres.
DEFAULT_UNIT = "mm"


def label_for(name: str) -> str:
    """The UI name for a parameter, falling back to the raw name so a
    parameter added to the equations file still shows up (unlabelled)
    instead of disappearing from the form."""
    return LABELS.get(name, name)


def unit_for(name: str) -> Optional[str]:
    return None if name in INTEGER_PARAMETERS else DEFAULT_UNIT


def is_integer(name: str) -> bool:
    return name in INTEGER_PARAMETERS


def format_value(name: str, value) -> str:
    """Render a value the way this parameter should read: whole numbers
    for counts ("2", not "2.0"), and no trailing ".0" noise elsewhere."""
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if is_integer(name):
        return str(int(round(number)))
    return f"{number:g}"
