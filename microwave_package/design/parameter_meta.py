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

import re
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
    "cavity_clearance": "Cavity clearance to chip",
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
    # transmission line, shielding and bonds
    "cpw_trace_width": "CPW trace width",
    "via_diameter": "Via diameter",
    "via_max_spacing": "Maximum via spacing",
    "via_gnd_clearance": "Via fence setback from gap",
    "via_min_spacing": "Minimum via spacing",
    "bond_pad_length": "Bond pad length",
    "bond_pad_width": "Bond pad width",
    "bond_pad_pitch": "Bond pad pitch",
    "bond_height": "Bond loop height",
    "bond_wire_diameter": "Bond wire diameter",
    # metallisation
    "copper_thickness": "PCB copper thickness",
    "chip_metal_thickness": "Chip metal thickness",
    "no_distortion": "Distortion",
    "no_chip": "Bare board (no chip)",
    "screws_on": "Corner screws",
    "screw_inset": "Screw inset from PCB edge",
    "lid_width": "Lid thickness at the screws",
    "screw_length": "Screw length (0 = from the stack)",
    "screw_clearance_diameter": "Screw clearance hole (lid, PCB)",
    "screw_tap_diameter": "Screw tap-drill hole (box)",
    "screw_keepout_diameter": "Copper keep-out round the screw",
    "screw_engagement": "Thread engagement in the box floor",
    "vent_width": "Vent groove width",
    "vent_depth": "Vent groove depth (below the floor top)",
    # distortion geometry
    "distortion_radius": "Distortion radius",
    "distortion_x": "Distortion offset along feedline",
    "distortion_y": "Distortion offset across feedline",
}

# Shown, typed, and reported in micrometres instead of millimetres.
# These are the sub-millimetre dimensions, where "381" beats "0.381".
MICRON_PARAMETERS = {
    "pcb_width", "chip_width", "route_clearence", "chip_leg",
    "copper_thickness", "chip_metal_thickness",
    "via_gnd_clearance", "via_min_spacing", "bond_wire_diameter", "cavity_clearance",
}

# Counts rather than lengths: no unit at all.
INTEGER_PARAMETERS = {"port_count"}

# On/off flags in the equations file (a plain 0 or 1). The form gives
# these their own named control instead of a value-or-range box, so they
# are left out of the parameter groups below. `no_distortion` is the only
# one: 1 builds the clean geometry, 0 adds distortions to the box and
# holes to the PCB.
DISTORTION_PARAM = "no_distortion"

# The bare-board flag: 1 builds only the PCB and its connectors -- a CPW
# straight from one SMP to the other, with no die, opening, pedestal,
# bond pads or wire bonds. It is a plain on/off pinned for the whole
# run, never swept, so it has no mode table like the distortion does.
NO_CHIP_PARAM = "no_chip"

# The corner-screws flag: 1 adds four M2 countersunk screws at the PCB's
# corners, with their holes in the lid, the PCB and the box floor. A plain
# on/off pinned for the whole run, like no_chip. Its dimensions are swept
# like any other parameter -- see SCREW_GEOMETRY_PARAMETERS.
SCREWS_PARAM = "screws_on"

# The vents flag: 1 cuts the narrow grooves that let the pedestal recess and
# the screw bores breathe out of the box's side (see design.venting). A plain
# on/off pinned for the whole run; the groove's size is swept like any other.
VENTS_PARAM = "vents_on"
BOOLEAN_PARAMETERS = {DISTORTION_PARAM, NO_CHIP_PARAM, SCREWS_PARAM, VENTS_PARAM}

VENT_GEOMETRY_PARAMETERS = ["vent_width", "vent_depth"]

SCREW_GEOMETRY_PARAMETERS = [
    "screw_inset", "screw_clearance_diameter", "screw_tap_diameter",
    "screw_keepout_diameter", "screw_engagement", "screw_length",
]


def is_bare_board(values) -> bool:
    """Whether this combination's values ask for the board without a chip."""
    try:
        return float(values.get(NO_CHIP_PARAM, 0)) > 0.5
    except (TypeError, ValueError):
        return False

# The distortion's own geometry. These only mean anything when the
# distortion is actually built (no_distortion = 0), so they are swept
# across the distorted combinations only -- see sweep.combinations.
#
# TODO: draw these three in base_parts/parameters_visual.SLDDRW and
# export them (base_parts/parameters_svg.py) as distortion_radius.svg,
# distortion_x.svg and distortion_y.svg. Until those files exist the
# form just leaves their drawing cell empty -- nothing else to change.
DISTORTION_GEOMETRY_PARAMETERS = ["distortion_radius", "distortion_x", "distortion_y"]

# The three choices the form's "Distortions" control offers, and the
# `no_distortion` sweep values each expands to. A single value pins the
# flag for the whole run; [0, 1] builds every other combination both ways.
DISTORTION_MODES: Dict[str, List[int]] = {
    "none": [1],
    "with": [0],
    "both": [0, 1],
}
DEFAULT_DISTORTION_MODE = "none"

# How the flag's two values read as words -- used for combination ids and
# anywhere the value is shown. The equation value is inverted from plain
# English (no_distortion = 0 is the distorted model), so a folder named
# "no_distortion-0" reads backwards; these do not.
DISTORTION_VALUE_LABELS = {0: "with_distortion", 1: "without_distortion"}

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
            "cavity_clearance",
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
        "title": "Transmission line and shielding",
        "hint": "The CPW itself, the vias that shield its fence and stitch the ground planes "
                "elsewhere, and the bonds across to the chip. The CPW gap is absent on purpose: "
                "it is solved for 50 ohm from the trace width, substrate and copper thickness "
                "rather than typed.",
        "collapsed": False,
        "names": [
            "cpw_trace_width",
            "via_diameter", "via_max_spacing", "via_gnd_clearance", "via_min_spacing",
            "bond_pad_length", "bond_pad_width", "bond_pad_pitch",
            "bond_height", "bond_wire_diameter",
        ],
    },
    {
        "title": "Metallisation",
        "hint": "Conductor thicknesses. These matter more than they look: 35 um of copper "
                "across a ~100 um gap pulls the CPW impedance down by more than 10 ohm, "
                "which is why the 50 ohm gap is solved with the thickness rather than "
                "without it.",
        "collapsed": False,
        "names": ["copper_thickness", "chip_metal_thickness"],
    },
    {
        "title": "Additional parameters",
        "hint": "Mechanical features and margins that do not affect the interior fields.",
        "collapsed": True,
        "names": ["wall_width", "lid_width", "wing_length", "wing_width"],
    },
    {
        "title": "Corner screws",
        "hint": "The four M2 countersunk screws at the PCB corners and the holes for them. "
                "Only used when the Corner screws box above is ticked.",
        "collapsed": True,
        "names": SCREW_GEOMETRY_PARAMETERS,
    },
    {
        "title": "Vent grooves",
        "hint": "The narrow grooves in the box floor that let trapped air out to the box's "
                "side (the pedestal recess, the screw bores). Far below cutoff, below the "
                "board's ground plane. Only used when Vent grooves is ticked.",
        "collapsed": True,
        "names": VENT_GEOMETRY_PARAMETERS,
    },
]

# The distortion's geometry gets its own group, rendered next to the
# Distortions radio rather than among the groups above -- it only applies
# to the combinations that are actually built distorted.
DISTORTION_GROUP: dict = {
    "title": "Distortion geometry",
    "hint": "The size and placement of the distortion itself. These are only used by the "
            "combinations built with distortions -- sweeping one adds its values to those "
            "combinations alone.",
    "names": DISTORTION_GEOMETRY_PARAMETERS,
}


def label_for(name: str) -> str:
    """The UI name for a parameter, falling back to the raw name so a
    parameter added to the equations file still shows up (unlabelled)
    instead of disappearing from the form."""
    return LABELS.get(name, name)


def is_micron(name: str) -> bool:
    return name in MICRON_PARAMETERS


def is_integer(name: str) -> bool:
    """Whether the displayed value is a whole number: counts and boolean
    flags always, and micrometres because that is the point of using them."""
    return name in INTEGER_PARAMETERS or name in BOOLEAN_PARAMETERS or is_micron(name)


def unit_for(name: str) -> Optional[str]:
    if name in INTEGER_PARAMETERS or name in BOOLEAN_PARAMETERS:
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


def distortion_word(value) -> Optional[str]:
    """"with_distortion" / "without_distortion" for a no_distortion flag
    value (0 / 1), or None if it is neither."""
    try:
        return DISTORTION_VALUE_LABELS.get(int(round(float(value))))
    except (TypeError, ValueError):
        return None


def format_value(name: str, value) -> str:
    """Render a millimetre value the way this parameter should read."""
    if value is None:
        return ""
    if name == DISTORTION_PARAM:
        word = distortion_word(value)
        if word is not None:
            return word
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


def combo_id_part(name: str, value) -> str:
    """One parameter's filesystem-safe piece of a combination id.

    Usually `name-value` (e.g. "pcb_length-40", "chip_width-381um"). The
    distortion flag is the exception: it contributes a single word,
    "with_distortion" / "without_distortion", because "no_distortion-0"
    as a folder name reads backwards (0 is the distorted one).
    """
    if name == DISTORTION_PARAM:
        word = distortion_word(value)
        if word is not None:
            return word
    safe_name = re.sub(r"[^A-Za-z0-9_]+", "_", name)
    safe_value = re.sub(r"[^A-Za-z0-9.]+", "_", combo_id_value(name, value))
    return f"{safe_name}-{safe_value}"


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
    the form rather than silently missing. Two kinds are left out here
    because the form gives them their own place: boolean flags (see
    BOOLEAN_PARAMETERS), which get a named control rather than a
    value-or-range box, and the distortion geometry, which is rendered as
    DISTORTION_GROUP beside the Distortions radio.
    """
    excluded = BOOLEAN_PARAMETERS | set(DISTORTION_GEOMETRY_PARAMETERS)
    remaining = [n for n in names if n not in excluded]
    result = []
    for group in GROUPS:
        present = [n for n in group["names"] if n in remaining]
        for n in present:
            remaining.remove(n)
        result.append({**group, "names": present})
    if remaining and result:
        result[-1]["names"] = result[-1]["names"] + remaining
    return [g for g in result if g["names"]]
