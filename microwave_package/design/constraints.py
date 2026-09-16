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

# How far the die's own copper is pulled back from the die's physical
# edge -- MUST MATCH ChipCopperInset in base_parts/altium/
# GenerateFromParams.pas. It lives as a duplicated constant rather than
# a shared import because the Pascal side cannot read a Python module;
# both copies carry this same cross-reference so a change to one is a
# reminder to change the other.
CHIP_COPPER_INSET = 0.15


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

    # The die's opening, which everything below has to keep out of. It
    # is cut larger than the die by cavity_clearance, so "clears the
    # die" and "clears the hole the die sits in" are no longer the same
    # statement -- and the second is the one that matters.
    from . import cavity as cavity_plan

    opening = cavity_plan.plan(values)

    if get("cavity_clearance") < 0:
        return (
            "cavity_clearance is negative, which would cut the opening SMALLER than "
            "the die -- there would be nowhere for it to go. Use 0 for an exact fit, "
            "or a positive value for the wiggle room a milled edge actually needs"
        )

    # The coplanar ground planes are each drawn as one region with the
    # opening stepped out of their inner edge. An opening reaching the
    # copper edge would cut a plane in two, which one region cannot be,
    # so the generator would fall back to leaving copper across the hole.
    if opening.half_side >= half_side:
        return (
            f"the die's opening reaches {_fmt(opening.half_side)} mm across the feedline "
            f"but the PCB only offers {_fmt(half_side)} mm -- reduce chip_side or "
            f"cavity_clearance, or widen pcb_side"
        )
    if opening.half_length >= half_length:
        return (
            f"the die's opening reaches {_fmt(opening.half_length)} mm along the feedline "
            f"but the PCB only offers {_fmt(half_length)} mm -- reduce chip_length or "
            f"cavity_clearance, or lengthen pcb_length"
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

    # The bond fingers sit on the board, so they have to be clear of the
    # opening -- not merely clear of the die. `bonding_clearence` is
    # measured from the die edge, and the opening is cut past that edge
    # by cavity_clearance, so a finger can be comfortably clear of the
    # die and still half over the hole. Each row is checked on the axis
    # it runs along, because a pad is bond_pad_length across at the
    # feedline ends and bond_pad_width across along the sides.
    for span, pad, extent, axis, pad_name in (
        (get("chip_length") / 2.0, get("bond_pad_length"),
         opening.half_length, "along the feedline", "bond_pad_length"),
        (get("chip_side") / 2.0, get("bond_pad_width"),
         opening.half_side, "across the feedline", "bond_pad_width"),
    ):
        finger_inner = span + get("bonding_clearence") - pad / 2.0
        if finger_inner <= extent:
            return (
                f"the bond fingers reach in to {_fmt(finger_inner)} mm {axis}, but the "
                f"die's opening already extends to {_fmt(extent)} mm, so they would hang "
                f"over it -- increase bonding_clearence, or reduce cavity_clearance or "
                f"{pad_name}"
            )

    # The shield ring goes OUTSIDE the bond fingers. Working inward from
    # the die the order is: die, opening, bond fingers, shield ring. Get
    # that backwards and the ring's vias land on the fingers -- which the
    # generator would build without complaint.
    finger_outer = get("chip_length") / 2.0 + get("bonding_clearence") + get("bond_pad_length") / 2.0
    ring_inner = get("chip_length") / 2.0 + get("shield_ring_inset") - via_dia / 2.0
    if ring_inner <= finger_outer:
        return (
            f"the shield ring reaches in to {_fmt(ring_inner)} mm but the bond fingers "
            f"already extend to {_fmt(finger_outer)} mm, so its vias would land on them "
            f"-- increase shield_ring_inset, or reduce bonding_clearence"
        )

    # Bond fingers sit between the die and the launch, and must not run
    # into either. The launch extent comes from the connector's own land
    # pattern, not from port_length -- that parameter describes the
    # aperture in the box wall, not this footprint.
    from . import smp

    finger_x = get("chip_length") / 2.0 + get("bonding_clearence")
    launch_inner = smp.launch_inner_edge(half_length, get("port_space"))
    if finger_x >= launch_inner:
        return (
            f"the bond fingers reach {_fmt(finger_x)} mm but the SMP launch starts at "
            f"{_fmt(launch_inner)} mm -- reduce bonding_clearence, or move the "
            f"connector out with port_space"
        )

    # The die-side bond lands at chip_length/2 - bond_pad_length (see
    # bonds.layout). The die's own copper stops CHIP_COPPER_INSET short
    # of the edge -- see GenerateFromParams.pas's ChipCopperInset, which
    # this must match -- so a bond_pad_length smaller than that inset
    # would land the wire on bare substrate rather than on copper.
    if get("bond_pad_length") <= CHIP_COPPER_INSET:
        return (
            f"bond_pad_length ({_fmt(get('bond_pad_length'))} mm) is not larger than the "
            f"{_fmt(CHIP_COPPER_INSET)} mm the die's own copper is pulled back from its edge "
            f"-- the bond would land on bare substrate instead of on copper. Increase "
            f"bond_pad_length"
        )

    return None
