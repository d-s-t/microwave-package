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

# How far short of via_min_spacing a gap may fall and still count as
# meeting it -- MUST MATCH GapTolerance in base_parts/altium/
# GenerateFromParams.pas, duplicated for the same reason as
# CHIP_COPPER_INSET above. Without it the rule below would disagree with
# the generator on exactly the boards that meet it exactly: 0.4 + 0.2
# comes out a hair over 0.6 in floating point, so 0.4 mm vias, 0.2 mm
# apart, every 0.6 mm would be refused here and built fine there.
VIA_GAP_TOLERANCE = 0.001


def check_via_grid(values: Dict[str, float]) -> Optional[str]:
    """Why this combination's via grid would come out sparser than asked
    for, or None if it will not.

    The ground-plane fill and the shield fence both step candidate vias
    every via_max_spacing, and the generator refuses any candidate that
    would sit closer than via_min_spacing, edge to edge, to a via already
    placed (TryVia and ViaObstructed in GenerateFromParams.pas). So two
    neighbouring grid points can both be placed only if

        via_diameter + via_min_spacing  <=  via_max_spacing

    When they cannot, the generator does not fail. It skips every other
    candidate across the whole fill and fence and carries on, and the
    board comes out with at least twice the spacing its parameters say.
    Nothing in the results would show that anything went wrong.

    Checked when a run is created, as well as by `check_layout` just
    before Altium, so a sweep over the via parameters skips these
    combinations up front rather than building their solids first.
    """
    dia = values.get("via_diameter", 0.0)
    step = values.get("via_max_spacing", 0.0)
    min_gap = values.get("via_min_spacing", 0.0)
    needed = dia + min_gap
    if step <= 0:
        # Not a sparse grid but no grid: the generator lays its rows out
        # a step apart, and a step of nothing leaves no via on the board.
        # The sum below does not catch it when the via has no size either.
        return f"via_max_spacing is {_fmt(step)} mm -- it is the step the vias are laid out by, and must be positive"
    if step - dia < min_gap - VIA_GAP_TOLERANCE:
        return (
            f"a {_fmt(dia)} mm via (via_diameter) plus the {_fmt(min_gap)} mm between vias "
            f"(via_min_spacing) needs grid points {_fmt(needed)} mm apart, but "
            f"via_max_spacing is {_fmt(step)} mm -- the generator would skip every other via "
            f"across the ground fill and shield fence, at least doubling the real spacing. "
            f"Raise via_max_spacing to at least {_fmt(needed)} mm, or reduce via_diameter "
            f"or via_min_spacing"
        )
    return None


def finger_offset(bonding_clearence: float, cavity_clearance: float) -> float:
    """How far past the die's edge the board's bond finger sits.

    `bonding_clearence` is not that distance. It is how far out from the
    die the LID has to stay clear, so that closing the package does not
    put metal on the wires -- which is why the SolidWorks equations file
    drives the chip lid from it. The finger belongs INSIDE that, not on
    its edge: a finger at the outer edge lands every wire against the
    very surface the clearance was reserved to hold away from it.

    So the finger goes in the middle. The middle of WHAT is the part
    that is easy to get wrong, and did get wrong once: the band runs
    from the edge of the PCB's opening out to
    `chip_half + bonding_clearence`, NOT from the die's own edge. The
    die sits in a pocket cut `cavity_clearance` wider than it is, so the
    innermost slice of the die-to-lid gap is over a hole -- there is no
    board there for a finger to sit on, and centring on the die's edge
    aims at thin air. Splitting the difference between the opening and
    the lid's limit puts equal metal on both sides of the pad, which is
    what the parameter is for.

    That also asks the least of `bonding_clearence` that geometry
    allows: the band has to be wider than the pad, and no more, so

        bonding_clearence > bond_pad_length + cavity_clearance

    It lives here rather than in `bonds` because `bonds` already imports
    this module for CHIP_COPPER_INSET, and the checks below need the
    same number. One definition, so a finger and the rule that says it
    fits cannot drift apart.
    """
    return (cavity_clearance + bonding_clearence) / 2.0


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

    # Already checked when the run was created. Checked again here so
    # a run from before that check, or a combination that reaches the
    # generator some other way, still cannot quietly build a sparse grid.
    reason = check_via_grid(values)
    if reason:
        return reason

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
    # before the board edge -- plus via_min_spacing, because the generator
    # keeps a via that far from the edge as it does from everything else
    # (TryVia in GenerateFromParams.pas). The fence is the first row of the
    # lattice every other via is laid out from, so a board it does not fit
    # on gets no vias at all, not a short fence.
    fence_edge = trace / 2.0 + gap + get("via_gnd_clearance") + via_dia + get("via_min_spacing")
    if fence_edge - VIA_GAP_TOLERANCE > half_side:
        return (
            f"the via fence needs {_fmt(fence_edge)} mm either side of the centre line "
            f"(including via_min_spacing to the board edge) but only {_fmt(half_side)} mm "
            f"is available -- reduce via_diameter, via_gnd_clearance, via_min_spacing or "
            f"cpw_trace_width, or widen pcb_side"
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

    # The bond fingers sit on the board, so they have to be clear of the
    # opening -- not merely clear of the die. `finger_offset` already
    # centres them between the opening and the lid's limit, so what is
    # left to check is whether that gap was ever wide enough for the pad
    # in the first place. Each row is checked on the axis it runs along,
    # because a pad is bond_pad_length across at the feedline ends and
    # bond_pad_width across along the sides.
    for span, pad, extent, axis, pad_name in (
        (get("chip_length") / 2.0, get("bond_pad_length"),
         opening.half_length, "along the feedline", "bond_pad_length"),
        (get("chip_side") / 2.0, get("bond_pad_width"),
         opening.half_side, "across the feedline", "bond_pad_width"),
    ):
        offset = finger_offset(get("bonding_clearence"), get("cavity_clearance"))
        finger_inner = span + offset - pad / 2.0
        if finger_inner <= extent:
            return (
                f"the bond fingers reach in to {_fmt(finger_inner)} mm {axis}, but the "
                f"die's opening already extends to {_fmt(extent)} mm, so they would hang "
                f"over it -- there is only {_fmt(get('bonding_clearence') - get('cavity_clearance'))} "
                f"mm of board between the opening and the lid's limit and the pad is "
                f"{_fmt(pad)} mm, so bonding_clearence wants to be more than "
                f"{_fmt(pad + get('cavity_clearance'))} mm here; increase it, or reduce "
                f"cavity_clearance or {pad_name}"
            )

    # Bond fingers sit between the die and the launch, and must not run
    # into either. The launch extent comes from the connector's own land
    # pattern, not from port_length -- that parameter describes the
    # aperture in the box wall, not this footprint.
    from . import smp

    finger_x = (get("chip_length") / 2.0
                + finger_offset(get("bonding_clearence"), get("cavity_clearance")))
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
