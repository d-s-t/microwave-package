"""
Geometry constraints the distortion has to satisfy, and the check that
decides whether one combination is buildable.

The distortion is a round feature cut into the PCB, offset from centre by
(`distortion_x`, `distortion_y`). It has to stay clear of everything
already occupying the board -- the chip and its wire bonds and the ports
along the feedline, the signal route across it -- and inside the PCB
outline. That gives two independent inequalities, one per axis, both
written about a half-span because the layout is symmetric about centre:

    along the feedline (x):
        chip_length/2 + bonding_clearence + 2*distortion_radius
            + distortion_x + port_length + port_space  <  pcb_length/2

    across the feedline (y):
        route_width/2 + 2*distortion_radius + distortion_y  <  pcb_side/2

A combination that fails either one is not built: `step_generation`
skips it and the run page shows why on its row (see `check_combination`).

Order matters when reporting. The radius is checked first, with the
offsets taken as 0 -- a radius that does not fit even centred is the
real problem, and reporting it as "distortion_x is too large" would send
you off adjusting the wrong number. Only once the radius fits are the
two offsets checked, each against its own axis and independently of the
other, so the message names exactly one parameter to change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from . import parameter_meta


@dataclass(frozen=True)
class Constraint:
    """One inequality: `terms` (a sum) must stay below `limit`.

    Every name is an equations-file parameter; `limit` is `limit_param`
    scaled by `limit_factor` (always a half-span here). `terms` maps a
    parameter to how many times it appears in the sum.
    """
    axis: str
    terms: Dict[str, float]
    limit_param: str
    limit_factor: float
    offset_param: str          # the distortion offset this inequality bounds
    description: str

    def total(self, values: Dict[str, float], overrides: Optional[Dict[str, float]] = None) -> float:
        merged = {**values, **(overrides or {})}
        return sum(merged.get(name, 0.0) * weight for name, weight in self.terms.items())

    def limit(self, values: Dict[str, float]) -> float:
        return values.get(self.limit_param, 0.0) * self.limit_factor


# 2 * distortion_radius in both: the feature has to clear its neighbour
# on the near side and still fit its own width before the PCB edge.
CONSTRAINTS: List[Constraint] = [
    Constraint(
        axis="x",
        terms={
            "chip_length": 0.5,
            "bonding_clearence": 1.0,
            "distortion_radius": 2.0,
            "distortion_x": 1.0,
            "port_length": 1.0,
            "port_space": 1.0,
        },
        limit_param="pcb_length",
        limit_factor=0.5,
        offset_param="distortion_x",
        description="along the feedline (chip, bonds, distortion and port must fit in half the PCB length)",
    ),
    Constraint(
        axis="y",
        terms={
            "route_width": 0.5,
            "distortion_radius": 2.0,
            "distortion_y": 1.0,
        },
        limit_param="pcb_side",
        limit_factor=0.5,
        offset_param="distortion_y",
        description="across the feedline (route and distortion must fit in half the PCB width)",
    ),
]

RADIUS_PARAM = "distortion_radius"


def _fmt(value: float) -> str:
    return f"{value:g}"


def _headroom(constraint: Constraint, values: Dict[str, float], free: str,
              overrides: Optional[Dict[str, float]] = None) -> float:
    """How large `free` can grow before this inequality is violated, with
    every other term held at `values` (plus `overrides`)."""
    without_free = constraint.total(values, {**(overrides or {}), free: 0.0})
    return (constraint.limit(values) - without_free) / constraint.terms[free]


def max_values(values: Dict[str, float]) -> Dict[str, float]:
    """The largest each distortion parameter can take before a
    combination stops fitting -- an exclusive bound, since the
    inequalities are strict.

    Computed the same way `check_combination` rejects, so the two never
    disagree: the radius against both axes with the offsets at 0 (its
    ceiling, whatever the offsets end up being), then each offset against
    its own axis with the radius as given. A negative result means there
    is no room at all and is reported as 0.
    """
    maxima: Dict[str, float] = {}

    zeroed = {constraint.offset_param: 0.0 for constraint in CONSTRAINTS}
    maxima[RADIUS_PARAM] = min(
        _headroom(constraint, values, RADIUS_PARAM, zeroed) for constraint in CONSTRAINTS
    )
    for constraint in CONSTRAINTS:
        maxima[constraint.offset_param] = _headroom(constraint, values, constraint.offset_param)

    return {name: max(value, 0.0) for name, value in maxima.items()}


def limit_note(name: str, maximum: float) -> str:
    """The one-line hint the form shows under a distortion parameter."""
    if maximum <= 0:
        return "no room for this on the current PCB"
    if name == RADIUS_PARAM:
        return f"must stay under {_fmt(maximum)} mm (with both offsets at 0)"
    return f"must stay under {_fmt(maximum)} mm (at this radius)"


# Everything the form's live hint needs to recompute these in the
# browser, so the numbers follow what is being typed rather than going
# stale the moment a PCB dimension changes.
def client_payload() -> dict:
    involved = sorted({name for c in CONSTRAINTS for name in c.terms} |
                      {c.limit_param for c in CONSTRAINTS})
    return {
        "radiusParam": RADIUS_PARAM,
        "constraints": [
            {
                "terms": c.terms,
                "limitParam": c.limit_param,
                "limitFactor": c.limit_factor,
                "offsetParam": c.offset_param,
            }
            for c in CONSTRAINTS
        ],
        # the unit each involved parameter is typed in, so the hint reads
        # the boxes the same way the form submits them
        "micron": {name: parameter_meta.is_micron(name) for name in involved},
    }


def check_combination(values: Dict[str, float]) -> Optional[str]:
    """Why this combination cannot be built, or None if it can.

    `values` is the full equations-file picture for one combination --
    the file's own values with the run's pinned values and the combo's
    own swept values applied on top. Only distorted combinations are
    checked; see the module docstring for the order and why.
    """
    if float(values.get(parameter_meta.DISTORTION_PARAM, 1)) != 0:
        return None  # no distortion in this one, so nothing to place

    # 1. the radius alone, with both offsets taken as 0. A radius that
    #    does not fit even centred is what has to change.
    zeroed = {constraint.offset_param: 0.0 for constraint in CONSTRAINTS}
    for constraint in CONSTRAINTS:
        total = constraint.total(values, zeroed)
        limit = constraint.limit(values)
        if total >= limit:
            largest = max(_headroom(constraint, values, RADIUS_PARAM, zeroed), 0.0)
            return (
                f"{parameter_meta.label_for(RADIUS_PARAM)} "
                f"{_fmt(values.get(RADIUS_PARAM, 0.0))} mm is too large {constraint.description}: "
                f"even centred it needs {_fmt(total)} mm of the {_fmt(limit)} mm available. "
                f"Largest that fits here: under {_fmt(largest)} mm."
            )

    # 2. each offset, on its own axis, independently of the other.
    for constraint in CONSTRAINTS:
        total = constraint.total(values)
        limit = constraint.limit(values)
        if total >= limit:
            offset = constraint.offset_param
            largest = max(_headroom(constraint, values, offset), 0.0)
            return (
                f"{parameter_meta.label_for(offset)} "
                f"{_fmt(values.get(offset, 0.0))} mm does not fit {constraint.description}: "
                f"needs {_fmt(total)} mm of the {_fmt(limit)} mm available. "
                f"Largest that fits here: under {_fmt(largest)} mm."
            )

    return None


# --- layout feasibility ----------------------------------------------
#
# The inequalities above are about the distortion, and are checked at run
# creation so an impossible combination is never handed to SolidWorks.
# These are the equivalent for the Altium layout, and exist for the same
# reason: it is much better to say "the via fence does not fit" up front
# than to let the generator produce a board that is quietly wrong.
#
# They are kept separate from CONSTRAINTS rather than folded into it
# because that machinery is shaped around one distortion offset per axis
# -- it drives the max-value hints in the form -- and these are plain
# geometric facts with no free offset to bound.

def check_layout(values: Dict[str, float]) -> Optional[str]:
    """Why this combination's layout cannot be built, or None if it can.

    Returns a sentence naming the parameter to change, in the same style
    as `check_combination`.
    """
    def get(name: str, default: float = 0.0) -> float:
        return values.get(name, default)

    trace = get("cpw_trace_width")
    if trace <= 0:
        return "cpw_trace_width must be positive"

    half_side = get("pcb_side") / 2.0
    half_length = get("pcb_length") / 2.0
    via_dia = get("via_diameter")

    # The CPW gap is solved rather than drawn, so the first thing that
    # can fail is the solve itself: too wide a trace over a thin
    # substrate is microstrip-dominated and cannot reach 50 ohm at any
    # gap. Better to say so here than to let the solver raise mid-sweep.
    from . import cpw

    try:
        gap = cpw.solve_gap_for_z0(
            trace_width=trace,
            substrate_height=get("pcb_width", 0.381),
            eps_r=cpw.PERMITTIVITY["Rogers TMM 10i (tm)"],
            conductor_thickness=get("copper_thickness"),
        )
    except ValueError as exc:
        return f"no 50 ohm geometry for this trace width: {exc}"

    # The fence sits outside the gap, and needs its own diameter of room
    # before the board edge.
    fence_edge = trace / 2.0 + gap + get("via_gnd_clearance") + via_dia
    if fence_edge >= half_side:
        return (
            f"the via fence needs {_fmt(fence_edge)} mm either side of the centre line "
            f"but only {_fmt(half_side)} mm is available -- reduce via_diameter, "
            f"via_gnd_clearance or cpw_trace_width, or widen pcb_side"
        )

    # The shield ring surrounds the chip pocket and must stay on the board.
    ring_x = get("chip_length") / 2.0 + get("shield_ring_inset") + via_dia / 2.0
    ring_y = get("chip_side") / 2.0 + get("shield_ring_inset") + via_dia / 2.0
    if ring_x >= half_length:
        return (
            f"the shield ring reaches {_fmt(ring_x)} mm along the feedline but the PCB "
            f"only offers {_fmt(half_length)} mm -- reduce shield_ring_inset or "
            f"chip_length, or lengthen pcb_length"
        )
    if ring_y >= half_side:
        return (
            f"the shield ring reaches {_fmt(ring_y)} mm across the feedline but the PCB "
            f"only offers {_fmt(half_side)} mm -- reduce shield_ring_inset or "
            f"chip_side, or widen pcb_side"
        )

    # Bond fingers sit between the die and the port, and must not run
    # into either.
    finger_x = get("chip_length") / 2.0 + get("bonding_clearence")
    port_inner = half_length - get("port_space") - get("port_length") / 4.0
    if finger_x >= port_inner:
        return (
            f"the bond fingers reach {_fmt(finger_x)} mm but the port starts at "
            f"{_fmt(port_inner)} mm -- reduce bonding_clearence, or move the port out "
            f"with port_space"
        )

    # A row of bonds has to fit across the die.
    bond_span = (get("bond_count") - 1) * get("bond_pad_pitch") + get("bond_pad_width")
    if get("bond_count") > 0 and bond_span >= get("chip_side"):
        return (
            f"{_fmt(get('bond_count'))} bonds at {_fmt(get('bond_pad_pitch'))} mm pitch span "
            f"{_fmt(bond_span)} mm, wider than the {_fmt(get('chip_side'))} mm die -- "
            f"reduce bond_count or bond_pad_pitch"
        )

    return None
