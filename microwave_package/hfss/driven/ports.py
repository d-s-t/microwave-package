"""
Where a driven run's two ports go, and how they are made.

An eigenmode run has no ports at all -- it is source-free. A driven run
has to be told where the signal enters and leaves, and that choice is not
a detail: it fixes the reference plane, and an S11 referenced at the
connector is a different number from an S11 referenced at the CPW. So the
run picks one of two, and this module builds it.

**`connector`** -- a wave port across the SMP's air bore, just above the
bead. `github.com/d-s-t/wr-smp-connector` reconstructs the bead as a
Ø0.9 mm centre post inside a Ø4.4 mm LCP insulator (50.2 ohm, the
datasheet's number), topped by a short Ø2.0 mm air bore around the
Ø0.6 mm pin. The bead's own top face is not a coax cross-section -- it
is the step from the Ø4.4 insulator to the Ø2.0 bore, split into several
faces -- so the port goes in the bore instead, which is a plain air coax
(about 72 ohm, renormalised to the port impedance). Everything from
there down -- the bead, SMD tab, land pattern, CPW -- is inside the
measurement.

The connector stands well above the box, whose vacuum stops at its
ceiling, so the bore is not otherwise in the model: `_bore_section`
adds the short air section between the bead and the port, and closes it
with a thin PEC annulus, because a wave port has to be on the outside
of the solved region. The empty barrel above stays out of the solve: it
is the unmated stub a real measurement has a plug in, and its
resonances would be an artefact of the model, not something the bench
sees.

**`launch`** -- a lumped port under the CPW trace, spanning the substrate
from the backing ground plane up to the trace, placed just inboard of
where the land pattern stops (`smp.launch_inner_edge`). The connector and
its launch are outboard of it and take no part in the measurement. This
is the board on its own.

Neither option changes the board. The connectors are placed either way
and the cavity is the same cavity; beyond the excitation, the only
difference is the connector plane's bore caps, which sit above the
port and outside the measurement.

Port 1 is always the -x end of the board and port 2 the +x end, matching
`place_connectors`' own index-to-end convention, so S21 always runs the
same direction from run to run.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List

from ...design import smp
from ..registry import PortPlacementError


logger = logging.getLogger("microwave_package")

# The two reference planes, as the run's `reference_plane` setting spells
# them and as the form offers them. They live here rather than with the
# solver's other settings because the help text is a description of what
# this module builds, and the two should not be able to drift apart.
REFERENCE_PLANE_CONNECTOR = "connector"
REFERENCE_PLANE_LAUNCH = "launch"

REFERENCE_PLANES = [
    {
        "value": REFERENCE_PLANE_CONNECTOR,
        "label": "Connector -- wave port in the SMP bore",
        "help": "Includes the connector transition and the launch. The port "
                "sits across the air bore just above the vendor bead, with the "
                "barrel above it capped off. Closest to what a VNA sees at "
                "the connector.",
    },
    {
        "value": REFERENCE_PLANE_LAUNCH,
        "label": "Launch -- lumped port at the CPW",
        "help": "Board only: excludes the connector and its land pattern. "
                "The port sits under the CPW trace, through the substrate to "
                "the backing ground plane, just inboard of the launch.",
    },
]

DEFAULT_REFERENCE_PLANE = REFERENCE_PLANE_CONNECTOR

# The up axis in assembly coordinates. The SolidWorks assembly has Y
# normal to the board (copper_geometry.Frame maps Altium's w to Y), and
# `place_connectors` stands each connector up so its mating direction is
# +Y as well.
UP = 1
# Along the feedline. Frame maps Altium's u to X.
ALONG = 0

# The air bore right above the bead, from the reconstruction in
# github.com/d-s-t/wr-smp-connector: a Ø2.0 bore, 0.25 mm tall, around
# the Ø0.6 pin, before the barrel opens out to Ø3.2. The port goes
# halfway up it and the cap stops short of the step.
BORE_RADIUS = 1.00
BORE_PORT_HEIGHT = 0.125
BORE_CAP_HEIGHT = 0.10

# What the port face should measure: pin inside, bore outside. Only a
# sanity check on what is measured off the real face, so a re-split of
# the vendor model that moves the pin is reported rather than silent.
_EXPECTED_OUTER_RADIUS = BORE_RADIUS
_EXPECTED_INNER_RADIUS = 0.30
_RADIUS_TOLERANCE = 0.05


def place_ports(hfss, layout, analysis_settings: Dict[str, Any], log=logger.info) -> List[str]:
    """Create the driven run's ports and return their names, port 1 first.

    `layout` is the `LayoutContext` `build_layout` returns -- None when
    the combination has no copper file, which also means no connectors
    were placed and there is nothing to attach a port to either way.

    Only a driven run has ports at all: a solver without them leaves
    `place_ports` at the registry's default, which builds nothing.
    """
    if layout is None:
        raise PortPlacementError(
            "A driven run needs ports, and this combination has no layout to put "
            "them on (no copper.txt -- the run was built with --no-altium, or "
            "layout generation failed). Either turn the Altium stage back on or "
            "use an Eigenmode run."
        )

    plane = analysis_settings.get("reference_plane", DEFAULT_REFERENCE_PLANE)
    impedance = float(analysis_settings.get("port_impedance_ohm", 50.0))

    if plane == REFERENCE_PLANE_CONNECTOR:
        names = _connector_wave_ports(hfss, impedance, log=log)
    elif plane == REFERENCE_PLANE_LAUNCH:
        names = _launch_lumped_ports(hfss, layout, impedance, log=log)
    else:
        raise PortPlacementError(
            f"Unknown reference plane {plane!r}. Known: "
            f"{[p['value'] for p in REFERENCE_PLANES]}."
        )

    if len(names) != 2:
        raise PortPlacementError(
            f"A driven run needs exactly 2 ports to report S21; the "
            f"{plane!r} reference plane produced {len(names)}: {names}. "
            "Check port_count in the parameters."
        )
    log(f"Ports for this driven run ({plane} reference plane): {names[0]} = port 1, {names[1]} = port 2.")
    return names


# --- the connector reference plane ------------------------------------

def _connector_wave_ports(hfss, impedance: float, log) -> List[str]:
    """A PEC-backed wave port across each SMP connector's air bore."""
    beads = sorted(
        name for name in hfss.modeler.object_names
        if name.startswith("connector_") and name.endswith("_dielectric")
    )
    if not beads:
        raise PortPlacementError(
            "No connector bead bodies (connector_N_dielectric) in the model, so "
            "there is no coax cross-section to put a wave port on. The SMP model "
            "either did not import or did not split into three bodies -- see the "
            "build log above. Use the 'launch' reference plane instead, which "
            "does not need the connector."
        )
    vacuum = next((n for n in hfss.modeler.object_names if n.lower() == "vacuum"), None)

    names: List[str] = []
    for bead in beads:
        index = bead.split("_")[1]
        centre = f"connector_{index}_center"
        if centre not in hfss.modeler.object_names:
            raise PortPlacementError(
                f"{bead} has no {centre} beside it, so there is no centre conductor "
                "to put the port's inner edge on."
            )
        port_name = f"port_{index}"
        air = _bore_section(hfss, bead, centre, vacuum, port_name, log=log)
        face = max(
            (f for f in hfss.modeler[air].faces if f.is_planar),
            key=lambda f: f.center[UP],
        )
        inner, outer = _annulus_radii(face, air, log=log)
        start, stop = _radial_integration_line(face, inner, outer)
        try:
            port = hfss.wave_port(
                assignment=face.id,
                integration_line=[start, stop],
                modes=1,
                impedance=impedance,
                name=port_name,
                renormalize=True,
            )
        except Exception as exc:
            raise PortPlacementError(
                f"Could not create a wave port on top of {air} (id {face.id}, "
                f"centre {face.center}): {exc}"
            ) from exc
        if not port:
            raise PortPlacementError(f"HFSS refused a wave port on top of {air}.")
        log(
            f"Wave port {port_name} across connector_{index}'s bore: annulus "
            f"r={inner:.3f}..{outer:.3f} mm at height {face.center[UP]:.3f} mm, "
            f"renormalised to {impedance} ohm."
        )
        names.append(port_name)
    return names


def _bore_section(hfss, bead: str, centre: str, vacuum, port_name: str, log) -> str:
    """Model the air bore just above the bead and close it; return the air body.

    Two annuli stacked on the bead's top face, both cut to fit between
    the pin and the bore wall:

    * `<port>_bore` -- vacuum, solved, BORE_PORT_HEIGHT tall. The
      connector stands well above the box, and the model's vacuum stops
      at the box ceiling, so without this the bore is not in the model at
      all and there is no air coax to put a port on. Its top face is the
      port.
    * `<port>_cap` -- PEC, BORE_CAP_HEIGHT tall, right above it. A wave
      port has to be on the outside of the solved region -- HFSS solves
      its 2D mode as if the guide ran on forever behind it -- and the
      cap guarantees that even in a box tall enough for its vacuum to
      reach the bore. It also leaves the empty barrel above out of the
      solve: that is the unmated stub a real measurement has a plug in.

    The port sits in the plain Ø2.0 air section above the bead, whose top
    face is the highest flat face of the bead body. The pin is Ø0.6 there,
    so the port sees a uniform air coax, and the bead's step out to Ø4.4
    is BORE_PORT_HEIGHT below it -- inside the measurement, as the
    connector transition is meant to be.
    """
    top = _mating_end_face(hfss, bead).center[UP]
    along, across = _connector_axis(hfss, bead, top)
    outer = f"connector_{bead.split('_')[1]}_outer"
    conductors = [body for body in (centre, outer) if body in hfss.modeler.object_names]

    bodies = []
    for name, bottom, height, material in (
        (f"{port_name}_bore", top, BORE_PORT_HEIGHT, "vacuum"),
        (f"{port_name}_cap", top + BORE_PORT_HEIGHT, BORE_CAP_HEIGHT, "pec"),
    ):
        # Drawn wider than the bore and cut back by both conductors, so
        # each meets the pin and the bore wall exactly -- no sliver
        # between them and no metal overlap, whatever the true radii are.
        body = hfss.modeler.create_cylinder(
            orientation="Y", origin=[along, bottom, across], radius=BORE_RADIUS + 0.1,
            height=height, name=name, material=material,
        )
        if not body:
            raise PortPlacementError(f"Could not create {name} for the wave port.")
        body.solve_inside = material != "pec"
        hfss.modeler.subtract(name, conductors, keep_originals=True)
        bodies.append(name)
    if vacuum is not None:
        hfss.modeler.subtract(vacuum, bodies, keep_originals=True)
    log(
        f"{bodies[0]}: air bore from {top:.3f} to {top + BORE_PORT_HEIGHT:.3f} mm, "
        f"closed by {bodies[1]} above it."
    )
    return bodies[0]


def _connector_axis(hfss, bead: str, top: float) -> tuple:
    """The connector's axis, as its (along, across) position.

    Not a bounding box: the centre conductor's includes the SMD tab
    running off to one side, and the bead's the tab's slot. The bead's
    top carries at least one face that is an exact annulus about the
    axis -- two full circles, area pi*(R^2 - r^2) -- and its centroid is
    the axis.
    """
    across = [axis for axis in range(3) if axis not in (UP, ALONG)][0]
    for face in hfss.modeler[bead].faces:
        if not face.is_planar or abs(face.center[UP] - top) > 1e-6:
            continue
        lengths = [edge.length for edge in face.edges]
        if len(lengths) != 2 or not all(lengths):
            continue
        r, big_r = sorted(length / (2.0 * math.pi) for length in lengths)
        if abs(face.area - math.pi * (big_r ** 2 - r ** 2)) < 0.01 * face.area:
            return face.center[ALONG], face.center[across]
    raise PortPlacementError(
        f"{bead}'s top has no annular face to find the connector's axis from; "
        "the connector model is not the one this code expects."
    )


def _mating_end_face(hfss, bead: str):
    """The bead's flat end face on the mating side -- the topmost one.

    The bead is a right circular annulus standing on the board with its
    mating direction up (`place_connectors` rotates it there). Its only
    planar faces are therefore its two flat ends -- the inner and outer
    walls are cylinders -- and the one toward the mating end is simply
    the higher of the two.

    The upper face is the right one because the lower face's far side is
    the connector's SMD layer -- ground pad, void ring and an off-axis
    centre tab -- which is neither axisymmetric nor a clean coax
    cross-section. The upper face's far side is the outer conductor's
    shoulder and the air bore, which is.

    Faces are told apart by `is_planar` rather than by their normals:
    PyAEDT computes a face normal from two of its vertices, and an
    annulus bounded by two circles does not reliably have two usable ones
    -- a circular edge carries a single seam vertex, and two seam
    vertices at the same angle are radially parallel, so the cross
    product that would give the normal collapses. `is_planar` asks AEDT
    for the face centre instead, which succeeds for exactly the flat
    faces and fails for the curved ones.
    """
    flat_ends = [face for face in hfss.modeler[bead].faces if face.is_planar]
    if len(flat_ends) < 2:
        raise PortPlacementError(
            f"{bead} has {len(flat_ends)} planar face(s), expected 2 (the annulus' "
            "two flat ends). Either the connector was not stood upright or the "
            "vendor split produced a bead this code does not recognise."
        )
    return max(flat_ends, key=lambda face: face.center[UP])


def _annulus_radii(face, bead: str, log) -> tuple:
    """The coax cross-section's inner and outer radii, in millimetres.

    Measured off the face rather than taken from the datasheet, so the
    port always matches the geometry actually being solved. The two
    bounding circles' circumferences give both radii directly; the face
    area is an independent check on the pair, since an annulus of these
    radii has area pi*(R^2 - r^2) and anything else means the face is not
    the annulus this code thinks it is.
    """
    circumferences = sorted(edge.length for edge in face.edges if edge.length)
    if len(circumferences) != 2:
        raise PortPlacementError(
            f"{bead}'s port face has {len(circumferences)} edges, expected 2 "
            "(the inner and outer circles of an annulus). It is not the coax "
            "cross-section this code expects."
        )
    inner, outer = (length / (2.0 * math.pi) for length in circumferences)

    expected_area = math.pi * (outer ** 2 - inner ** 2)
    if face.area and abs(face.area - expected_area) > 0.02 * expected_area:
        raise PortPlacementError(
            f"{bead}'s port face measures r={inner:.3f}, R={outer:.3f} mm from "
            f"its edges but has area {face.area:.4f} mm^2, not the "
            f"{expected_area:.4f} mm^2 an annulus of those radii would have. The face "
            "is not a plain annulus, so a coax wave port on it would not mean what "
            "this code says it means."
        )

    for measured, expected, what in (
        (outer, _EXPECTED_OUTER_RADIUS, "outer"),
        (inner, _EXPECTED_INNER_RADIUS, "inner"),
    ):
        if abs(measured - expected) > _RADIUS_TOLERANCE:
            log(
                f"WARNING: {bead}'s {what} radius measures {measured:.3f} mm, not the "
                f"{expected:.3f} mm the vendor reconstruction documents. The port is "
                "built to the measured geometry, but the connector model may have "
                "changed under it."
            )
    return inner, outer


def _radial_integration_line(face, inner: float, outer: float) -> tuple:
    """Where the port's voltage is measured: straight across the annulus.

    A coax's TEM mode has its E field along the radius, so the line runs
    from the centre conductor to the outer one. Which radial direction is
    arbitrary -- the mode is axisymmetric -- so it goes along the board's
    across-axis, the one that is neither the up axis nor the feedline.

    Without this, Zpi has no voltage to integrate and the port impedance
    HFSS renormalises to is not the one this reference plane is chosen
    for.
    """
    across = [axis for axis in range(3) if axis not in (UP, ALONG)][0]
    start = list(face.center)
    stop = list(face.center)
    start[across] += inner
    stop[across] += outer
    return start, stop


# --- the launch reference plane ---------------------------------------

def _launch_lumped_ports(hfss, layout, impedance: float, log) -> List[str]:
    """A lumped port under the trace at each end of the CPW.

    The sheet stands in the transverse plane, spanning the substrate from
    the backing ground plane's top face up to the trace's underside, and
    the trace's width across. Both of its horizontal edges therefore land
    on copper, which is what a lumped port needs; the vertical edges are
    free in the dielectric, as they are for the ordinary microstrip port
    this is shaped like.

    The CPW here is conductor-backed -- there is a ground plane on the
    underside of the board, not just the coplanar grounds -- so the mode
    this excites is the one the line actually carries.
    """

    frame, stack = layout.frame, layout.stack
    trace_width = layout.values.get("cpw_trace_width", 0.2)
    half_length = layout.values.get("pcb_length", 26.0) / 2.0
    port_space = layout.values.get("port_space", 1.5)
    count = int(layout.values.get("port_count", 2))

    # Where the land pattern stops reaching back onto the board. Nothing
    # of the launch comes past this, so a port here is clear of it.
    inner_edge = smp.launch_inner_edge(half_length, port_space)
    substrate_thickness = stack.pcb_top - stack.pcb_bottom

    names: List[str] = []
    for index in range(count):
        sign = -1.0 if index == 0 else 1.0
        # frame.point maps Altium (u, v, w) to assembly (x, y, z); the
        # corner the rectangle grows from is the low one in both spanned
        # axes, hence -trace/2 across and the substrate's bottom face up.
        origin = frame.point(sign * inner_edge, trace_width / 2.0, stack.pcb_bottom)
        port_name = f"port_{index + 1}"
        sheet = hfss.modeler.create_rectangle(
            orientation="X",
            origin=origin,
            # WhichAxis="X" puts width along Y and height along Z.
            sizes=[substrate_thickness, trace_width],
            name=f"{port_name}_sheet",
        )
        if not sheet:
            raise PortPlacementError(f"Could not create the port sheet for {port_name}.")

        # Ground plane up to the trace: the reference conductor first, so
        # every port's positive terminal is the signal line.
        start = frame.point(sign * inner_edge, 0.0, stack.pcb_bottom)
        stop = frame.point(sign * inner_edge, 0.0, stack.pcb_top)
        try:
            port = hfss.lumped_port(
                assignment=sheet.name,
                integration_line=[start, stop],
                impedance=impedance,
                name=port_name,
                renormalize=True,
            )
        except Exception as exc:
            raise PortPlacementError(
                f"Could not create a lumped port on {sheet.name} at u={sign * inner_edge:.3f} mm: {exc}"
            ) from exc
        if not port:
            raise PortPlacementError(f"HFSS refused a lumped port on {sheet.name}.")
        log(
            f"Lumped port {port_name} at u={sign * inner_edge:.3f} mm: "
            f"{trace_width:.3f} mm across the trace, {substrate_thickness:.3f} mm "
            f"through the substrate, renormalised to {impedance} ohm."
        )
        names.append(port_name)
    return names
