"""
Where a driven run's two ports go, and how they are made.

An eigenmode run has no ports at all -- it is source-free. A driven run
has to be told where the signal enters and leaves, and that choice is not
a detail: it fixes the reference plane, and an S11 referenced at the
connector is a different number from an S11 referenced at the CPW. So the
run picks one of two, and this module builds it.

**`connector`** -- a wave port on the coax cross-section through the SMP
bead. The vendor bead is the one place in the connector that is genuinely
50 ohm: `github.com/d-s-t/wr-smp-connector` reconstructs it as a Ø0.9 mm
centre post inside a Ø4.4 mm LCP insulator, which is
`60/sqrt(3.6) * ln(2.2/0.45) = 50.2 ohm` -- the datasheet's number. Its
face toward the mating end is a flat annulus bounded by the centre post
inside and the outer conductor outside, which is exactly what an internal
wave port wants. Everything from there down -- bead, SMD tab, land
pattern, CPW -- is inside the measurement.

What is NOT inside it: the centre pin and the empty barrel above the
port, which stay in the model as an unmated stub. A real measurement has
a plug mated there instead, filling that space with the mating coax. So
the barrel's own resonances are an artefact of measuring an unmated
connector, and they are real in the model even though they would not be
on the bench. This is the honest cost of referencing an unmated jack;
there is no port plane that avoids it.

**`launch`** -- a lumped port under the CPW trace, spanning the substrate
from the backing ground plane up to the trace, placed just inboard of
where the land pattern stops (`smp.launch_inner_edge`). The connector and
its launch are outboard of it and take no part in the measurement. This
is the board on its own.

Neither option changes the geometry. The connectors are placed either
way, the cavity is the same cavity, and only the excitation moves -- so
two runs that differ only in reference plane are the same model measured
in two places, which is the only way the comparison means anything.

Port 1 is always the -x end of the board and port 2 the +x end, matching
`place_connectors`' own index-to-end convention, so S21 always runs the
same direction from run to run.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

from . import solve_types

logger = logging.getLogger("eigen_mode_analyze")

# The up axis in assembly coordinates. The SolidWorks assembly has Y
# normal to the board (copper_geometry.Frame maps Altium's w to Y), and
# `place_connectors` stands each connector up so its mating direction is
# +Y as well.
UP = 1
# Along the feedline. Frame maps Altium's u to X.
ALONG = 0

# The vendor bead's radii in millimetres, from the reconstruction in
# github.com/d-s-t/wr-smp-connector: Ø4.4 insulator around a Ø0.9 centre
# post. Only used to sanity-check what is measured off the real face --
# never to place anything, so a re-split of the vendor model that moves
# these does not silently disagree with the model being solved.
_EXPECTED_OUTER_RADIUS = 2.20
_EXPECTED_INNER_RADIUS = 0.45
_RADIUS_TOLERANCE = 0.25


class PortPlacementError(RuntimeError):
    """The run's ports could not be built.

    Its own type because a driven model without ports is worthless --
    there is nothing to excite and no S-matrix to read -- so this must
    stop the combination being saved and queued, unlike the warnings
    `place_connectors` raises for cosmetic trouble.
    """


def place_ports(hfss, layout, analysis_settings: Dict[str, Any], log=logger.info) -> List[str]:
    """Create the driven run's ports and return their names, port 1 first.

    `layout` is the `LayoutContext` `build_layout` returns -- None when
    the combination has no copper file, which also means no connectors
    were placed and there is nothing to attach a port to either way.

    A no-op returning `[]` for an eigenmode run, so the caller does not
    have to know which kind of run it is building.
    """
    solution_type = solve_types.solution_type_of(analysis_settings)
    if not solve_types.is_driven(solution_type):
        return []

    if layout is None:
        raise PortPlacementError(
            "A driven run needs ports, and this combination has no layout to put "
            "them on (no copper.txt -- the run was built with --no-altium, or "
            "layout generation failed). Either turn the Altium stage back on or "
            "use an Eigenmode run."
        )

    plane = analysis_settings.get("reference_plane", solve_types.DEFAULT_REFERENCE_PLANE)
    impedance = float(analysis_settings.get("port_impedance_ohm", 50.0))

    if plane == solve_types.REFERENCE_PLANE_CONNECTOR:
        names = _connector_wave_ports(hfss, impedance, log=log)
    elif plane == solve_types.REFERENCE_PLANE_LAUNCH:
        names = _launch_lumped_ports(hfss, layout, impedance, log=log)
    else:
        raise PortPlacementError(
            f"Unknown reference plane {plane!r}. Known: "
            f"{[p['value'] for p in solve_types.REFERENCE_PLANES]}."
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
    """A wave port on each SMP bead's outward coax cross-section."""
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

    names: List[str] = []
    for bead in beads:
        face = _mating_end_face(hfss, bead)
        inner, outer = _annulus_radii(face, bead, log=log)
        start, stop = _radial_integration_line(face, inner, outer)
        port_name = f"port_{bead.split('_')[1]}"
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
                f"Could not create a wave port on {bead}'s coax face (id {face.id}, "
                f"centre {face.center}): {exc}"
            ) from exc
        if not port:
            raise PortPlacementError(f"HFSS refused a wave port on {bead}'s coax face.")
        log(
            f"Wave port {port_name} on {bead}: annulus r={inner:.3f}..{outer:.3f} mm "
            f"at height {face.center[UP]:.3f} mm, renormalised to {impedance} ohm."
        )
        names.append(port_name)
    return names


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
            f"{bead}'s mating-end face has {len(circumferences)} edges, expected 2 "
            "(the inner and outer circles of an annulus). It is not the coax "
            "cross-section this code expects."
        )
    inner, outer = (length / (2.0 * math.pi) for length in circumferences)

    expected_area = math.pi * (outer ** 2 - inner ** 2)
    if face.area and abs(face.area - expected_area) > 0.02 * expected_area:
        raise PortPlacementError(
            f"{bead}'s mating-end face measures r={inner:.3f}, R={outer:.3f} mm from "
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
                "built to the measured geometry, but the bead may no longer be the "
                "50 ohm section this reference plane assumes."
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
    from . import smp

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
