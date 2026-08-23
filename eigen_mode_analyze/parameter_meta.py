"""
How each sweep parameter is presented in the web UI: a readable name, a
unit, the order and grouping it appears in, and -- for the sub-millimetre
ones -- the conversion between what you type and what the model stores.

The equation-file names and their millimetre values are the source of
truth everywhere else. Nothing here changes the geometry: SolidWorks
always reads and writes millimetres, exactly as `base_parts/default.txt`
holds them. Micrometres exist only at the edges of the system -- the
form, the combination ids, and the results table -- because 0.381 is a
worse thing to read and type than 381.

To rename or regroup something, edit LABELS and GROUPS. The keys are
equation-file names and must keep matching `base_parts/default.txt` (and
the drawing named after each one in `base_parts/parameters_visual/`).
"""
from __future__ import annotations

from typing import Dict, List, Optional

# Equation-file name -> what the web UI calls it.
LABELS: Dict[str, str] = {
    # cavity and chip
    "pcb_length": "PCB length (along feedline)",
    "pcb_side": "PCB width (across feedline)",
    "chip_length": "Chip length (along feedline)",
    "chip_side": "Chip width (across feedline)",
    "pcb_width": "PCB thickness",
    "chip_width": "Chip thickness",
    "pedestal_depth": "Pedestal depth",
    # ports and clearances
    "port_count": "Number of ports",
    "port_length": "Port length",
    "port_width": "Port width",
    "port_space": "Port distance from edge",
    "chip_leg": "Chip support leg height",
    "bonding_clearence": "Wire-bond clearance",
    "route_width": "Lid relief width",
    "route_clearence": "Lid relief depth",
    "route_chip_clearence": "Lid relief clearance to chip",
    # additional
    "wall_width": "Box wall thickness",
    "wing_length": "Mounting wing length",
    "wing_width": "Mounting wing width",
}

# Shown, typed, and reported in micrometres instead of millimetres.
# These are the sub-millimetre dimensions, where "381" beats "0.381".
MICRON_PARAMETERS = {"pcb_width", "chip_width", "route_clearence", "chip_leg"}

# Counts rather than lengths: no unit at all.
INTEGER_PARAMETERS = {"port_count"}

MILLIMETRE = "mm"
MICROMETRE = "µm"
MICRONS_PER_MM = 1000.0

# The order the form lists parameters in, and how they are split up. The
# first group is what actually moves the eigenmodes; the last is folded
# away behind a disclosure arrow because it never does.
GROUPS: List[dict] = [
    {
        "title": "Cavity and chip",
        "hint": "These set the resonant volume and how it is loaded -- the parameters "
                "that move the eigenmodes.",
        "collapsed": False,
        "names": [
            "pcb_length", "pcb_side",
            "chip_length", "chip_side",
            "pcb_width", "chip_width",
            "pedestal_depth",
        ],
    },
    {
        "title": "Ports and clearances",
        "hint": "Coupling apertures and the local features around the chip and the "
                "signal routes.",
        "collapsed": False,
        "names": [
            "port_count", "port_length", "port_width", "port_space",
            "chip_leg", "bonding_clearence",
            "route_width", "route_clearence", "route_chip_clearence",
        ],
    },
    {
        "title": "Additional parameters",
        "hint": "Mechanical features and margins that do not affect the interior fields.",
        "collapsed": True,
        "names": ["wall_width", "wing_length", "wing_width"],
    },
]


def label_for(name: str) -> str:
    """The UI name for a parameter, falling back to the raw name so a
    parameter added to the equations file still shows up (unlabelled)
    instead of disappearing from the form."""
    return LABELS.get(name, name)


def is_micron(name: str) -> bool:
    return name in MICRON_PARAMETERS


def is_integer(name: str) -> bool:
    """Whether the displayed value is a whole number: counts always, and
    micrometres because that is the point of using them."""
    return name in INTEGER_PARAMETERS or is_micron(name)


def unit_for(name: str) -> Optional[str]:
    if name in INTEGER_PARAMETERS:
        return None
    return MICROMETRE if is_micron(name) else MILLIMETRE


def to_display(name: str, value):
    """Millimetres (what the model holds) -> what the UI shows."""
    if value is None:
        return None
    return float(value) * MICRONS_PER_MM if is_micron(name) else float(value)


def to_model(name: str, value):
    """What the UI shows -> millimetres (what the model holds)."""
    if value is None:
        return None
    return float(value) / MICRONS_PER_MM if is_micron(name) else float(value)


def _render(name: str, display_value: float) -> str:
    """Whole numbers where the parameter calls for them, but never at the
    cost of the actual value: 0.3815 mm still reads as 381.5 rather than
    being silently rounded to 382."""
    if is_integer(name) and abs(display_value - round(display_value)) < 1e-9:
        return str(int(round(display_value)))
    return f"{display_value:g}"


def format_value(name: str, value) -> str:
    """Render a millimetre value the way this parameter should read."""
    if value is None:
        return ""
    try:
        return _render(name, to_display(name, value))
    except (TypeError, ValueError):
        return str(value)


def combo_id_value(name: str, value) -> str:
    """The value as it appears in a combination id / folder name.

    Micrometre parameters carry a `um` suffix: a bare "381" sitting next
    to "pcb_length-40" would read as millimetres months later.
    """
    text = format_value(name, value)
    return f"{text}um" if is_micron(name) else text


def column_name(name: str) -> str:
    """Results-table column for a parameter, tagged with its unit when
    that is not the millimetres the rest of the pipeline uses."""
    return f"{name}_um" if is_micron(name) else name


def axis_label(name: str) -> str:
    unit = unit_for(name)
    return f"{label_for(name)} ({unit})" if unit else label_for(name)


def grouped(names) -> List[dict]:
    """The parameters that exist, in display order and split into groups.

    Anything in the equations file that GROUPS does not mention is added
    to the last group, so a newly added parameter is still reachable in
    the form rather than silently missing.
    """
    remaining = list(names)
    result = []
    for group in GROUPS:
        present = [n for n in group["names"] if n in remaining]
        for n in present:
            remaining.remove(n)
        result.append({**group, "names": present})
    if remaining and result:
        result[-1]["names"] = result[-1]["names"] + remaining
    return [g for g in result if g["names"]]
