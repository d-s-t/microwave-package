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

from . import parameter_meta, pedestal


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


def without_chip_room(values: Dict[str, float]) -> Dict[str, float]:
    """The values with the room the chip and its bonds take set to zero,
    when this is the bare board (`no_chip`); otherwise `values` itself.

    The feedline inequality reserves chip_length/2 + bonding_clearence
    for the die and its wires. A board with no die reserves none of it.
    """
    if not parameter_meta.is_bare_board(values):
        return values
    return {**values, "chip_length": 0.0, "bonding_clearence": 0.0}


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
    values = without_chip_room(values)

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
    values = without_chip_room(values)

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
# How much wider than the trace the lid's relief channel has to be (mm).
ROUTE_MARGIN = 0.1
# The narrowest CPW gap the layout accepts (mm). Below it the model solves but
# the line carries no signal (an 86 um gap on RT/duroid 5880 read S11 0 dB,
# S21 -91 dB; 111 um transmitted) and it is under what most PCB fabs etch.
# The cause, found with a 94 um gap (box study B22): AEDT cannot unite the
# board's copper ("would not unite the pieces of 'pcb_copper_top'"), so it is
# left in pieces with no conductor material and the validation then refuses
# the model; a 129 um gap on the same board built. Swapping the 94 um
# ground-plane regions for the 111 um ones made it build, so the trigger is in
# the planes around the launch, not the vias, pads or die. The 86 um board is
# very likely the same failure with the checks not yet there to catch it.
MIN_CPW_GAP = 0.1
# How far the ground is pulled back from the SMP signal pad when the equations
# file does not say (mm) -- MUST MATCH the `launch_clearance` default in
# GenerateFromParams.pas. Chosen from the HFSS sweep in PRE_MANUFACTURING.md.
DEFAULT_LAUNCH_CLEARANCE = 0.2


def launch_clearance(values: Dict[str, float]) -> float:
    """The ground keep-out round the SMP signal pad, `launch_clearance`."""
    return values.get("launch_clearance", DEFAULT_LAUNCH_CLEARANCE)


def pad_ground_edge(values: Dict[str, float], gap: float) -> float:
    """How far from the line's axis the top ground starts beside the SMP signal
    pad: the CPW's own gap edge, or `launch_clearance` off the pad's edge when
    that is further out. The pad is wider than the trace, so the gap edge alone
    leaves only gap - (pad - trace) / 2 of copper-to-copper clearance."""
    from . import smp

    return max(values.get("cpw_trace_width", 0.0) / 2.0 + gap,
               smp.SIGNAL_HALF_W + launch_clearance(values))


def pad_ground_clearance(values: Dict[str, float], gap: float) -> float:
    """The copper-to-copper distance from the SMP signal pad to the ground
    beside it, as the generator draws it (keep-out included)."""
    from . import smp

    return pad_ground_edge(values, gap) - smp.SIGNAL_HALF_W


def check_launch_clearance(values: Dict[str, float], gap: float) -> Optional[str]:
    """Why the ground beside the SMP signal pad is too close to it, or None.

    The pad is wider than the trace, and the ground edge follows the trace, so
    without a keep-out the clearance is the line's gap minus half the
    difference (71 um for a 0.4 mm trace with a 111 um gap on the 0.48 mm pad).
    The generator pulls the ground back to `launch_clearance` off the pad; that
    has to be at least MIN_CPW_GAP, the same floor as the line's own gap.
    """
    from . import smp

    clearance = pad_ground_clearance(values, gap)
    if clearance < MIN_CPW_GAP - VIA_GAP_TOLERANCE / 10.0:
        trace = values.get("cpw_trace_width", 0.0)
        bare = gap - (2.0 * smp.SIGNAL_HALF_W - trace) / 2.0
        return (
            f"the ground beside the {_fmt(2.0 * smp.SIGNAL_HALF_W)} mm SMP signal pad is only "
            f"{clearance * 1000.0:.0f} um from it (launch_clearance = "
            f"{launch_clearance(values) * 1000.0:.0f} um; without a keep-out the {gap * 1000.0:.0f} um "
            f"gap of a {_fmt(trace)} mm trace would leave {bare * 1000.0:.0f} um), under the "
            f"{_fmt(MIN_CPW_GAP * 1000.0)} um minimum -- raise launch_clearance to at least "
            f"{_fmt(MIN_CPW_GAP)} mm"
        )
    return None


def lid_cover(values: Dict[str, float]) -> Dict[str, float]:
    """The lid over the trace, as the keyword arguments `cpw.solve_gap_for_z0`
    takes: `cover_height` (the relief channel's depth, `route_clearence`) and
    `channel_width` (`route_width`). Empty when the values carry no lid, so a
    caller with only a board still gets an open-top line.

    The lid rests on the board's top copper, so the ceiling over the trace is
    the channel's depth above the copper, and the CPW has to fit the channel's
    width: this is the line that is built, and the one the gap is solved for.
    """
    width = values.get("route_width")
    depth = values.get("route_clearence")
    if not width or depth is None:
        return {}
    return {"cover_height": depth, "channel_width": width}


def chip_trace(values: Dict[str, float]) -> float:
    """The width of the die's own signal trace: `chip_trace_width`, or the
    board's `cpw_trace_width` for values that have no such parameter (runs
    from before the die's line had a width of its own)."""
    return values.get("chip_trace_width") or values.get("cpw_trace_width", 0.2)


def _relief_reason(exc, values: Dict[str, float]) -> str:
    """The layout check's sentence for a lid channel the solve refused."""
    trace = values["cpw_trace_width"]
    width = values.get("route_width", 0.0)
    depth = values.get("route_clearence", 0.0)
    if exc.kind == "narrow":
        return (
            f"the lid's relief channel is route_width = {_fmt(width)} mm wide, but a {_fmt(trace)} mm "
            f"trace under a {depth * 1000.0:.0f} um relief needs {exc.gap * 1000.0:.0f} um gaps for 50 ohm, "
            f"a CPW {trace + 2.0 * exc.gap:.2f} mm across, and the channel's walls have to stand "
            f"{(exc.needed_width - trace - 2.0 * exc.gap) / 2.0:.2f} mm outside it or they sit over the "
            f"slots and pull the line well under 50 ohm -- raise route_width to at least "
            f"{exc.needed_width:.2f} mm"
        )
    reach = (f" -- deepen route_clearence to at least {exc.min_height * 1000.0:.0f} um, or narrow "
             f"cpw_trace_width" if exc.min_height else " -- narrow cpw_trace_width")
    return (
        f"the lid's relief is only {depth * 1000.0:.0f} um deep (route_clearence), which puts the lid so "
        f"close over a {_fmt(trace)} mm trace that the line stays under 50 ohm whatever the gap{reach}"
    )


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



def check_screws(values: Dict[str, float], gap: float) -> Optional[str]:
    """Why the four corner screws do not fit this board, or None if they do
    (or if `screws_on` is off).

    Each screw is a hole in the PCB with a copper keep-out round it, so
    nothing on the board may come within that circle. The vias are not on
    this list: the generator keeps them out of the circle by itself. What
    cannot move is the CPW, the connector's launch and, on a board with a
    die, the die and the bond fingers round it.
    """
    from . import screws as screw_plan, smp

    screws = screw_plan.plan(values)
    if screws is None:
        return None

    def get(name: str, default: float = 0.0) -> float:
        return values.get(name, default)

    inset = get("screw_inset", 2.5)
    clear = screws.clearance_diameter
    sink = screw_plan.COUNTERSINK_DIAMETER
    if clear < screw_plan.SCREW_DIAMETER + 0.1:
        return (f"screw_clearance_diameter {_fmt(clear)} mm leaves no room for an "
                f"{_fmt(screw_plan.SCREW_DIAMETER)} mm screw -- increase it")
    if clear >= sink:
        return (f"screw_clearance_diameter {_fmt(clear)} mm is as wide as the {_fmt(sink)} mm "
                f"countersink, so the head would have nothing to sit on -- reduce it")
    if screws.tap_diameter >= screw_plan.SCREW_DIAMETER:
        return (f"screw_tap_diameter {_fmt(screws.tap_diameter)} mm is not smaller than the "
                f"{_fmt(screw_plan.SCREW_DIAMETER)} mm screw, so there would be no thread to cut")
    if screws.keepout_diameter < clear:
        return (f"screw_keepout_diameter {_fmt(screws.keepout_diameter)} mm is smaller than the "
                f"{_fmt(clear)} mm hole it is meant to keep copper out of")
    if screws.engagement < screw_plan.MIN_ENGAGEMENT:
        return (f"screw_engagement {_fmt(screws.engagement)} mm is under the "
                f"{_fmt(screw_plan.MIN_ENGAGEMENT)} mm a thread needs to hold anything")
    if inset - clear / 2.0 < screw_plan.EDGE_MARGIN:
        return (f"screw_inset {_fmt(inset)} mm leaves only {_fmt(inset - clear / 2.0)} mm between "
                f"the PCB hole and the board edge, under the {_fmt(screw_plan.EDGE_MARGIN)} mm "
                f"a router needs -- increase screw_inset or reduce screw_clearance_diameter")
    if inset - sink / 2.0 < 0.2:
        return (f"screw_inset {_fmt(inset)} mm puts the {_fmt(sink)} mm countersink within "
                f"{_fmt(inset - sink / 2.0)} mm of the lid's edge -- increase screw_inset "
                f"to at least {_fmt(sink / 2.0 + 0.2)} mm")

    # the stack: lid on top, floor below
    lid = get("lid_width", 1.0)
    if lid < screws.min_lid_thickness:
        return (f"lid_width {_fmt(lid)} mm is under the {_fmt(screws.min_lid_thickness)} mm the "
                f"countersunk head needs ({_fmt(screw_plan.HEAD_HEIGHT)} mm of head plus "
                f"{_fmt(screw_plan.MIN_LID_SEAT)} mm of lid under it) -- increase lid_width")
    half_length = get("pcb_length") / 2.0
    half_side = get("pcb_side") / 2.0
    u, v = half_length - inset, half_side - inset
    if u <= 0 or v <= 0:
        return (f"screw_inset {_fmt(inset)} mm is more than half the board -- the screws "
                f"would cross over. Reduce screw_inset or enlarge pcb_length/pcb_side")
    # The ground plane gives up a square round each screw, out to the
    # board's corner (see screws.block_half), and the keep-out sits inside
    # it. That square is what has to stay clear of everything else on the
    # board, so it is what is tested -- not just the circle.
    s = screw_plan.block_half(screws)
    x_in, y_in = u - s, v - s       # the square's inner edges, from the centre

    # the CPW and its gaps run down the middle
    corridor = get("cpw_trace_width") / 2.0 + gap
    if y_in < corridor:
        return (f"the copper the plane gives up round a screw reaches {_fmt(y_in)} mm from the "
                f"centre line but the CPW gap extends to {_fmt(corridor)} mm -- widen pcb_side or "
                f"increase screw_inset / reduce screw_keepout_diameter")

    # the connector's launch at each end. Both it and the square run out
    # to the board's end, so only the way across matters.
    launch_inner = smp.launch_inner_edge(half_length, get("port_space"))
    if y_in < smp.GROUND_LAND_HALF:
        return (f"the copper the plane gives up round a screw reaches {_fmt(y_in)} mm from the "
                f"centre line but the SMP launch (from {_fmt(launch_inner)} mm to the board's end) "
                f"is {_fmt(smp.GROUND_LAND_HALF)} mm either side -- widen pcb_side, or increase "
                f"screw_inset / reduce screw_keepout_diameter")

    # the die and the bond fingers round it
    if not parameter_meta.is_bare_board(values):
        reach_u = get("chip_length") / 2.0 + get("bonding_clearence")
        reach_v = get("chip_side") / 2.0 + get("bonding_clearence")
        if x_in < reach_u and y_in < reach_v:
            return (f"the copper the plane gives up round a screw runs into the die and its bond "
                    f"fingers (out to {_fmt(reach_u)} x {_fmt(reach_v)} mm from the centre) -- "
                    f"enlarge the board, or reduce chip size or bonding_clearence")
    return None


def ground_bond_start(values: Dict[str, float], board_gap: float, chip_gap: Optional[float] = None) -> float:
    """How far from the line's axis the edge of the first ground bond pad at a
    feedline end has to be: beyond the wider of the board's and the die's CPW
    (trace / 2 + gap), plus the setback the via fence uses. `bonds.layout` and
    the layout check both use this, so what is placed is what was checked."""
    clearance = values.get("via_gnd_clearance", 0.15)
    edge = values.get("cpw_trace_width", 0.2) / 2.0 + board_gap
    if chip_gap is not None:
        edge = max(edge, chip_trace(values) / 2.0 + chip_gap)
    return edge + clearance


def check_chip_line(values: Dict[str, float], mat_chip: Optional[str], board_gap: float) -> Optional[str]:
    """Why the die's CPW cannot be built, or None if it can: a 50 ohm gap
    for the die's trace on the die's silicon that is a real CPW, and room
    on the die's copper for the ground bonds beside it."""
    from . import cpw

    def get(name: str, default: float = 0.0) -> float:
        return values.get(name, default)

    trace = chip_trace(values)
    if trace <= 0:
        return "chip_trace_width must be positive"
    try:
        chip_gap = cpw.solve_chip_gap_for_z0(
            trace_width=trace,
            substrate_height=get("chip_width", 0.381),
            eps_r=cpw.permittivity_of(mat_chip or "silicon"),
            conductor_thickness=get("chip_metal_thickness"),
        )
    except ValueError as exc:
        return (
            f"no 50 ohm CPW for a {_fmt(trace)} mm trace on the {_fmt(get('chip_width', 0.381))} mm die: "
            f"{exc} -- reduce chip_trace_width (the die's own trace; it does not have to equal "
            f"the board's cpw_trace_width)"
        )

    copper_half = get("chip_side") / 2.0 - CHIP_COPPER_INSET
    pad = get("bond_pad_width")
    need = ground_bond_start(values, board_gap, chip_gap) + pad
    if need > copper_half:
        return (
            f"the die's CPW (a {_fmt(trace)} mm trace with {chip_gap * 1000.0:.0f} um gaps) and the "
            f"board's leave the ground bonds no room: they start {need - pad:.3f} mm from the line and "
            f"need {_fmt(pad)} mm more, but the die's copper ends {copper_half:.3f} mm out -- "
            f"reduce chip_trace_width, or widen chip_side"
        )
    return None


def check_bare_pedestal(values: Dict[str, float]) -> Optional[str]:
    """A bare board (no_chip) has no die, so its bottom ground plane has no
    opening for the pedestal recess under the die to sit in: the recess would
    run into a whole plane (AEDT: "pcb_copper_bottom and box intersect").
    Checked at run creation so such a combination is skipped up front rather
    than failing in the Altium step after its STEP files are built."""
    depth = values.get("pedestal_depth", 0.0)
    if parameter_meta.is_bare_board(values) and depth > 0:
        return (f"pedestal_depth {_fmt(depth)} mm cuts a recess in the box floor "
                f"under the die, but a bare board (no_chip) has no die and a whole bottom ground "
                f"plane for it to run into -- set pedestal_depth to 0")
    return None


def check_layout(values: Dict[str, float], mat_pcb: Optional[str] = None,
                 mat_chip: Optional[str] = None) -> Optional[str]:
    """Why this combination's layout cannot be built, or None if it can.

    `mat_pcb` is the run's PCB material: the CPW gap is solved from its
    permittivity, so a check made with another one refuses boards that can be
    built and accepts ones that cannot. Left out, it is the default TMM 10i.
    `mat_chip` is the die's, likewise (default silicon).

    The gap is solved for the line under the lid (`lid_cover`), so a relief
    channel too shallow to reach 50 ohm, or too narrow to hold the CPW with
    its walls clear of the slots, is refused here too.

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

    reason = check_bare_pedestal(values)
    if reason:
        return reason

    # Already checked when the run was created. Checked again here so
    # a run from before that check, or a combination that reaches the
    # generator some other way, still cannot quietly build a sparse grid.
    reason = check_via_grid(values)
    if reason:
        return reason

    # The lid sits directly on the board's top copper, and the relief channel
    # it has over the trace (route_width wide) is all that keeps it off the
    # trace. A channel no wider than the trace puts the lid's walls on the
    # trace's edges, which shorts the line to the box: the model solves, and
    # reads as a line with no transmission (S11 0 dB, S21 -90 dB at 2 GHz).
    route_width = get("route_width")
    if route_width and route_width < trace + ROUTE_MARGIN:
        return (
            f"the lid's relief channel is route_width = {_fmt(route_width)} mm wide but the trace is "
            f"{_fmt(trace)} mm: with less than {_fmt(ROUTE_MARGIN)} mm to spare the lid lands on the "
            f"trace edges and shorts it -- raise route_width to at least "
            f"{_fmt(trace + ROUTE_MARGIN)} mm or narrow cpw_trace_width"
        )
    relief = values.get("route_clearence")
    if route_width and relief is not None and relief <= 0:
        return (
            f"the lid's relief is route_clearence = {_fmt(relief)} mm deep: the lid would rest on the "
            f"trace and short it -- give the relief a depth (a few hundred um; the 50 ohm solve says "
            f"how much it needs)"
        )

    # The CPW gap is solved rather than drawn, so the first thing that
    # can fail is the solve itself: too wide a trace over a thin
    # substrate is microstrip-dominated and cannot reach 50 ohm at any
    # gap. Better to say so here than to let the solver raise mid-sweep.
    #
    # The lid is part of the line: the solve takes the channel's depth as the
    # ceiling over the trace, and refuses a channel that is too shallow to
    # reach 50 ohm or too narrow for the CPW it comes to.
    from . import cpw

    try:
        gap = cpw.solve_gap_for_z0(
            trace_width=trace,
            substrate_height=get("pcb_width", 0.381),
            eps_r=cpw.permittivity_of(mat_pcb or "Rogers TMM 10i (tm)"),
            conductor_thickness=get("copper_thickness"),
            **lid_cover(values),
        )
    except cpw.ReliefChannelError as exc:
        return _relief_reason(exc, values)
    except ValueError as exc:
        return f"no 50 ohm geometry for this trace width: {exc}"
    if gap < MIN_CPW_GAP:
        return (
            f"the 50 ohm CPW gap for a {_fmt(trace)} mm trace on this substrate is only "
            f"{gap * 1000.0:.0f} um, under the {_fmt(MIN_CPW_GAP * 1000.0)} um minimum (AEDT could not "
            f"unite the copper of a board with a 94 um gap, and one with 86 um did not transmit; it "
            f"is also below what most fabs etch) -- "
            f"widen cpw_trace_width"
        )

    reason = check_launch_clearance(values, gap)
    if reason:
        return reason

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

    # Checked once the board's own gap is known, ahead of the die's rules:
    # a screw that cannot fit is a plainer thing to be told than the
    # die-and-bonds arithmetic after it.
    reason = check_screws(values, gap) if not parameter_meta.is_bare_board(values) else None
    if reason:
        return reason
    from . import venting
    reason = venting.check(values)
    if reason:
        return reason

    # A bare board has no die: no opening, no bond fingers and nothing
    # to keep clear of them, so the room a die would need is not asked
    # for. The connector launch still has to fit on the board.
    if parameter_meta.is_bare_board(values):
        from . import smp

        if smp.launch_inner_edge(half_length, get("port_space")) <= 0.0:
            return (
                "the SMP launch reaches past the middle of the board -- lengthen "
                "pcb_length, or move the connectors out with port_space"
            )
        return check_screws(values, gap)

    # The die's own line. Its gap is solved on the silicon, for its own trace
    # (`chip_trace_width`): a trace that is a CPW on the board can be no CPW at
    # all on a 0.381 mm die, where 0.4 mm of trace levels off just over 50
    # ohm and the old solve put the ground 29 mm away. And the ground bonds
    # land beside the gap, on copper, so the die has to have the room.
    reason = check_chip_line(values, mat_chip, board_gap=gap)
    if reason:
        return reason

    # The recess's rounded corners must fit its legs, and the legs must hold
    # the die clear of its play (see design/pedestal.py).
    reason = pedestal.check(values)
    if reason:
        return reason

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
    # The ground keep-out round the signal pad reaches launch_clearance past it.
    launch_inner = smp.launch_inner_edge(half_length, get("port_space")) - launch_clearance(values)
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
