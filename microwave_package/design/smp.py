"""
The SMP connector land pattern, measured from the vendor library.

Wuerth Elektronik WR-SMP 60124102122403 today, but nothing below is
about Wuerth. The footprint is not drawn from these numbers -- the
generator places the vendor's own footprint out of the PcbLib, so the
lands, the silkscreen, the courtyard and the 3D body are the vendor's
objects rather than a transcription. What this module works out is what
the BOARD AROUND the footprint has to know: how far the launch reaches,
and the shape of the hole in the ground.

And it works it out rather than carrying it. The numbers come from
`base_parts/altium/vendor/footprint.txt`, which `MeasureFootprint.pas`
writes by reading the library inside Altium -- a PcbLib is a compiled
compound document and Python cannot open one. Measured once when the
library changes, committed, and read from here on.

Coordinates end up in the connector's frame, in millimetres:

    v  runs along the signal axis; the centre pin exits toward +v
    u  runs across it

WHICH WAY THE LIBRARY DRAWS IT IS NOT ASSUMED. Wuerth draw this part
with the centre pin toward -y; another manufacturer may use +x, or -x,
or anything else, and a footprint placed a half turn out is a launch
that looks perfect and faces the board edge. So the frame is derived
from the footprint itself: pin 1 of a coax launch is the centre
conductor, and the whole pattern is rotated until that pad lies on +v.
That is the one convention every vendor agrees on -- unlike axis
orientation, pad naming beyond pin 1, or where the origin sits.

It is a rotation, never a reflection. A mirrored footprint is the sort
of thing nobody notices until the boards arrive.

Worth knowing about this particular part: Wuerth ship DIFFERENT land
patterns for it in their Altium and KiCad libraries -- four 0.5 mm
square ground pads in a +-2.6 land against a U of bars in a +-2.65 one.
Both describe the same ground flange and the signal pad is identical.
The Altium one is authoritative here because it is the one being placed.

The board's own `port_length` and `port_width` parameters describe the
aperture in the box wall, not this land pattern, and are deliberately
not used here.

SWAPPING TO ANOTHER CONNECTOR
-----------------------------
This module plus two paths is the whole swap surface. Nothing
downstream names a manufacturer: the DelphiScript is handed a library
path and a footprint name and places whatever it finds, and the board
around the launch is drawn from the six values `as_parameters` returns.
To change part:

1. Put the vendor's PcbLib in `base_parts/altium/vendor/` and point
   `paths.ALTIUM_SMP_LIBRARY` at it. If all they ship is an IntLib, run
   `vendor/extract_intlib.py` on it first -- an IntLib is compiled, and
   Altium cannot open one as a library.
2. Set FOOTPRINT to the new footprint's name.
3. Run `altium_generation.measure_footprint()`. That rewrites
   footprint.txt, and everything below follows from it.
4. Replace the 3D model (`paths.SMP_MODEL_FILE`) and check the frame
   mapping at the bottom of this file still holds for it.

Then run the tests. They do not check these numbers against a copy of
themselves -- there is no copy any more -- they check that the footprint
which arrived is one this board can actually use: that it has a pin 1,
that its ground clears its own anti-pad, that its land contains its
pads. A connector that fails those is one to find out about before the
boards are ordered.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from .. import paths

# The footprint to place. The name is how the library indexes it, which
# for this vendor is the part number.
FOOTPRINT = "60124102122403"


class FootprintError(ValueError):
    """The measured footprint is missing, malformed, or is not something
    this board knows how to build a launch around.

    Raised at import rather than warned about. Every one of these means
    the launch would be built from numbers nobody checked, and a launch
    is the one part of this board where being quietly wrong costs a
    fabrication run.
    """


@dataclass(frozen=True)
class MeasuredPad:
    """One pad as the library holds it: centre and size in the pad's own
    frame, plus how that frame is turned relative to the footprint."""
    name: str
    layer: str
    x: float
    y: float
    xsize: float
    ysize: float
    rotation: float
    hole: float = 0.0


def _parse(text: str) -> Tuple[str, List[MeasuredPad], List[Sequence[Tuple[float, float]]], List[str]]:
    """Read what MeasureFootprint.pas wrote."""
    name = ""
    pads: List[MeasuredPad] = []
    coppers: List[Sequence[Tuple[float, float]]] = []
    bodies: List[str] = []

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("footprint="):
            name = line.partition("=")[2].strip()
            continue
        kind, _, rest = line.partition("|")
        fields = rest.split("|")
        try:
            if kind == "pad":
                # The hole is optional only so that a file written
                # before through-hole pads were measured still reads.
                pad_name, layer, x, y, sx, sy, rot = fields[:7]
                hole = float(fields[7]) if len(fields) > 7 else 0.0
                pads.append(MeasuredPad(pad_name, layer, float(x), float(y),
                                        float(sx), float(sy), float(rot), hole))
            elif kind == "copper":
                numbers = [float(v) for v in fields[0].split(",") if v != ""]
                if len(numbers) < 6 or len(numbers) % 2:
                    raise ValueError(f"{len(numbers)} coordinates")
                coppers.append(tuple(
                    (numbers[i], numbers[i + 1]) for i in range(0, len(numbers), 2)
                ))
            elif kind == "body":
                bodies.append(fields[0])
            else:
                raise ValueError(f"unknown record {kind!r}")
        except (ValueError, TypeError) as exc:
            raise FootprintError(
                f"{paths.ALTIUM_SMP_FOOTPRINT_FILE}, line {number}: bad {kind} "
                f"record ({exc}): {line!r}"
            ) from None

    return name, pads, coppers, bodies


def _orientation(signal: MeasuredPad) -> float:
    """The angle to rotate the library frame by so the centre pin exits
    toward +v.

    Taken from where pin 1 sits, not from an axis convention: the whole
    point is to accept a footprint drawn any way up.
    """
    if math.hypot(signal.x, signal.y) < 1e-9:
        raise FootprintError(
            "pin 1 sits on the footprint origin, so there is nothing to take the "
            "signal direction from. A coax launch should have its centre pin "
            "offset from the origin along the signal axis."
        )
    return math.pi / 2.0 - math.atan2(signal.y, signal.x)


def _rotate(point: Tuple[float, float], angle: float) -> Tuple[float, float]:
    x, y = point
    cos, sin = math.cos(angle), math.sin(angle)
    return (x * cos - y * sin, x * sin + y * cos)


def _in_connector_frame(pad: MeasuredPad, angle: float) -> Tuple[float, float, float, float]:
    """A pad as (u, v, size_u, size_v) once the frame is turned.

    A pad whose net rotation is a quarter turn has its sizes swapped;
    one at some other angle has no (size_u, size_v) at all, and saying
    so is better than picking whichever pair looks plausible.
    """
    u, v = _rotate((pad.x, pad.y), angle)
    net = (pad.rotation + math.degrees(angle)) % 180.0
    if min(net, 180.0 - net) < 1e-6:
        size_u, size_v = pad.xsize, pad.ysize
    elif abs(net - 90.0) < 1e-6:
        size_u, size_v = pad.ysize, pad.xsize
    else:
        raise FootprintError(
            f"pad {pad.name} sits at {net:g} degrees to the signal axis. This "
            f"board draws its launch from axis-aligned lands, so a footprint "
            f"with a skewed pad needs code here, not just a new library."
        )
    return (round(u, 6) + 0.0, round(v, 6) + 0.0,
            round(size_u, 6), round(size_v, 6))


def _land_geometry(points: Sequence[Tuple[float, float]]) -> Tuple[float, float, float]:
    """(land half-width, anti-pad radius, escape-slot half-width).

    All three are measured off the shape rather than stated anywhere, so
    a vendor who draws the void with a different radius or a wider slot
    is followed rather than assumed to match the last one.
    """
    half_u = max(abs(u) for u, _v in points)
    half_v = max(abs(v) for _u, v in points)
    if abs(half_u - half_v) > 1e-6:
        raise FootprintError(
            f"the ground land is {2 * half_u:g} x {2 * half_v:g} mm, not square. "
            f"The board's coplanar planes step around it as a square; an oblong "
            f"land needs that step taught the difference."
        )
    half = half_u

    # The void is the largest group of vertices sharing one radius, and
    # it has to lie inside the land -- which is what rules out the
    # land's own four corners, who also share a radius.
    #
    # Averaged over the group rather than taken from one member: Altium
    # holds coordinates in 1/10000 mil, so an arc drawn at exactly
    # 2.3 mm comes back as vertices scattered by tens of nanometres
    # around it, and the smallest of them reads as 2.29999. Physically
    # meaningless, but it propagates into the parameter file and the
    # reports, where it looks like an error rather than rounding.
    radii = sorted(math.hypot(u, v) for u, v in points)
    best_radius, best_count = 0.0, 0
    start = 0
    for end in range(len(radii) + 1):
        if end == len(radii) or radii[end] - radii[start] > 1e-4:
            group = radii[start:end]
            if len(group) > best_count and group[0] < half - 1e-6:
                best_radius = sum(group) / len(group)
                best_count = len(group)
            start = end
    best_radius = round(best_radius, 4)
    if best_count < 8:
        raise FootprintError(
            "no circular anti-pad found in the connector's ground land. That void "
            "is what holds ground away from the centre pin and sets the impedance "
            "of the transition -- a land without one is not a launch this board "
            "can draw a coplanar plane around."
        )

    # The slot: where the land's edge is broken by something that is not
    # a corner. Both breaks lie on the same edge, the signal side.
    mouth = sorted({round(u, 6) for u, v in points
                    if abs(abs(round(v, 6)) - half) < 1e-9 and abs(u) < half - 1e-9})
    if len(mouth) != 2 or abs(mouth[0] + mouth[1]) > 1e-6:
        raise FootprintError(
            f"expected one escape slot centred on the signal axis, found breaks at "
            f"{mouth}. That slot is where the centre conductor leaves the land."
        )
    return half, best_radius, mouth[1]


def derive(text: str, expect: str = "", source: str = "the measured footprint") -> dict:
    """Everything the board needs, worked out from a measured footprint.

    Separate from reading the file so that a footprint drawn any way up
    can be put through it directly. That matters more than it sounds:
    the orientation logic is the part with no second chance -- a launch
    placed a half turn out faces the board edge and looks perfect doing
    it -- and the only footprint on disk is one that happens to be drawn
    the way Wuerth draw it.
    """
    name, pads, coppers, bodies = _parse(text)
    if expect and name != expect:
        raise FootprintError(
            f"{source} measures {name!r} but FOOTPRINT is {expect!r}. Re-run "
            f"measure_footprint() after changing which footprint is placed."
        )

    # A plated hole under the launch is not something this generator
    # can draw: it places the footprint's copper, and nothing drills
    # for it. Refused by name rather than placed with the holes missing,
    # which would look like a perfectly good surface-mount launch.
    drilled = [p for p in pads if p.hole > 0 or p.layer == "multi"]
    if drilled:
        raise FootprintError(
            f"pad {drilled[0].name!r} is a through-hole pad ({drilled[0].hole:g} mm "
            f"drill on the multi-layer). This board places a surface-mount launch "
            f"and drills nothing for its component pads, so the holes would simply "
            f"be missing. A through-hole connector needs the generator taught to "
            f"drill them."
        )

    if not pads:
        raise FootprintError(
            "the footprint has no copper pads at all. If the part is real, its "
            "lands are on a layer MeasureFootprint.pas is not collecting."
        )

    if len(coppers) != 1:
        raise FootprintError(
            f"the footprint has {len(coppers)} copper shapes that are not pads, "
            f"and this board models exactly one -- the ground land with the "
            f"anti-pad in it. Anything else would silently not exist on the board."
        )

    signal = [p for p in pads if p.name.strip() == "1"]
    if len(signal) != 1:
        raise FootprintError(
            f"expected exactly one pad named '1' -- the centre conductor, which is "
            f"what orients the whole footprint -- but found {len(signal)}."
        )

    angle = _orientation(signal[0])
    land = tuple(_rotate(pt, angle) for pt in coppers[0])
    half, void, slot = _land_geometry(land)

    signal_pad = _in_connector_frame(signal[0], angle)
    ground_pads = tuple(_in_connector_frame(p, angle) for p in pads if p is not signal[0])
    every = (signal_pad,) + ground_pads

    return {
        "signal_pad": signal_pad,
        "ground_pads": ground_pads,
        "pads": every,
        "land": tuple((round(u, 6), round(v, 6)) for u, v in land),
        "land_half": round(half, 6),
        "void_radius": round(void, 6),
        "slot_half": round(slot, 6),
        "body": bodies[0] if bodies else "",
        # The outermost and innermost copper along the signal axis. The
        # LAND is included, not just the pads: on this footprint nothing
        # reaches as far back as the land does, and an envelope taken
        # from the pads alone would place the connector 0.35 mm too far
        # out. The inner edge is how far the launch reaches back onto
        # the board, which everything else has to keep clear of.
        "outer_v": round(min([-half] + [v - sv / 2 for _u, v, _su, sv in every]), 6),
        "inner_v": round(max([half] + [v + sv / 2 for _u, v, _su, sv in every]), 6),
    }


def _load() -> dict:
    """`derive`, applied to the committed measurement."""
    path = paths.ALTIUM_SMP_FOOTPRINT_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FootprintError(
            f"no measured footprint at {path} ({exc}). Run "
            f"microwave_package.design.altium_generation.measure_footprint() to "
            f"produce it -- it reads the library named by paths.ALTIUM_SMP_LIBRARY "
            f"inside Altium, which is the only thing that can open a PcbLib."
        ) from None
    return derive(text, expect=FOOTPRINT, source=str(path))


_MEASURED = _load()

# (u, v, size_u, size_v) per pad, in the connector frame.
SIGNAL_PAD = _MEASURED["signal_pad"]
GROUND_PADS = _MEASURED["ground_pads"]
PADS = _MEASURED["pads"]

# --- the ground land, and the hole in the middle of it ----------------
#
# The pads are not the whole footprint. The vendor library carries a
# filled polygon on the copper layer -- a square land with a circular
# void at its centre and a slot for the signal pad to escape through.
# Reading only the footprint's pads, which is how this file started,
# misses it completely.
#
# The circle is the anti-pad. It holds ground away from the centre pin
# and the dielectric around it, and it is what sets the impedance of the
# coax-to-coplanar transition. It is not decoration and not solder-mask:
# the vendor cut it out of copper deliberately.
#
# It still matters even though the vendor's own land is placed with the
# footprint, because the board's coplanar ground planes butt up against
# that land and are drawn by us. A plane drawn without knowing about the
# void simply runs straight through it -- leaving METAL, not a gap. On
# this board that put ground 0.371 mm from the pin where the vendor
# asked for 2.30 mm.
GROUND_LAND = _MEASURED["land"]
GROUND_LAND_HALF = _MEASURED["land_half"]
VOID_RADIUS = _MEASURED["void_radius"]
SLOT_HALF_U = _MEASURED["slot_half"]

# The 3D body the footprint carries, if it has one. This is what makes
# the placed component show up in Altium's own 3D view and STEP export,
# which four loose pads never could.
BODY_IDENTIFIER = _MEASURED["body"]

OUTER_V = _MEASURED["outer_v"]
INNER_V = _MEASURED["inner_v"]

# How much room the whole launch needs, measured from the board edge.
LENGTH = INNER_V - OUTER_V

# Where the centre conductor ends, measured from the outer edge inward.
SIGNAL_V = SIGNAL_PAD[1]


def as_parameters() -> Dict[str, float]:
    """What the DelphiScript needs to draw the board AROUND the launch.

    Not the pads: those arrive with the footprint the generator places
    out of the vendor library, so re-sending them here would be a second
    copy of the land pattern free to disagree with the first.

    What does cross is the envelope -- how far the launch reaches at
    each end -- and the anti-pad, because the coplanar ground planes are
    drawn here and have to be cut around the vendor's void rather than
    running through it.
    """
    return {
        "smp_sig_v": SIGNAL_V,
        "smp_outer_v": OUTER_V,
        "smp_inner_v": INNER_V,
        "smp_land_half": GROUND_LAND_HALF,
        "smp_void_r": VOID_RADIUS,
        "smp_slot_half": SLOT_HALF_U,
    }


def launch_origin(half_length: float, port_space: float) -> float:
    """Where the connector's own origin sits along the board axis.

    Placed so the outermost copper of the launch is exactly `port_space`
    from the board edge.
    """
    return half_length - port_space + OUTER_V


def launch_centre(half_length: float, port_space: float) -> float:
    """Where the centre conductor should end: the signal pad's centre."""
    return launch_origin(half_length, port_space) - SIGNAL_V


def launch_inner_edge(half_length: float, port_space: float) -> float:
    """The furthest the launch reaches back onto the board.

    Nothing else -- bond fingers, the die, the shield ring -- may come
    past this point.
    """
    return launch_origin(half_length, port_space) - INNER_V


# --- the connector's own 3D model -------------------------------------
#
# The vendor ships one solid -- STEP, IGES, Cadence, Eagle and KiCad all
# carry a single closed shell, with no separated pin / dielectric / shell.
# The companion project github.com/d-s-t/wr-smp-connector reconstructs the
# split from the STEP's per-face colour and the datasheet and ships
# base_parts/smp_connector_split.step: three named solids, imported as
# three bodies. The two conductors are a PEC lump; the dielectric bead is
# LCP (permittivity ~3.6). The old approximation -- the whole connector as
# one perfect conductor, with no dielectric in the launch -- is gone.
#
# The STEP sits in the connector's own frame, which is NOT the footprint
# frame and not the board's:
#
#     Xs  along the signal axis, but negated against the footprint's v
#     Ys  across, matching the footprint's u
#     Zs  height above the board
#
# so v = -Xs and u = Ys.

# Indicative extents of the vendor solid, kept for reference. Placement
# does not use them -- `place_connectors` measures the three imported
# bodies' combined bounding box, so it follows the split file exactly.
MODEL_X_SPAN = (-3.30, 2.50)   # along the signal axis
MODEL_Y_SPAN = (-2.50, 2.50)   # across


def model_rotation_degrees() -> float:
    """Rotation about X that stands the model up in assembly axes.

    The model has Z up; the assembly has Y up. -90 degrees about X sends
    (x, y, z) to (x, z, -y), which is the same mapping `Frame` applies to
    everything else -- they have to agree or the connector lands in a
    different plane from the board it sits on.
    """
    return -90.0


def model_offset(frame, board_top: float, half_length: float,
                 port_space: float, sign: float) -> list:
    """Where to move the model after rotating it.

    `sign` picks the end of the board: +1 is the +x end. The far
    connector is the near one turned through half a revolution about the
    up axis, so its offset is mirrored rather than recomputed.
    """
    origin = launch_origin(half_length, port_space)
    x_along = sign * origin
    return [x_along + frame.x_offset, board_top, frame.z_offset]


def model_alignment(bounding_box, frame, board_top: float, half_length: float,
                    port_space: float, sign: float) -> list:
    """How far to move an imported connector so it sits on its launch.

    Takes the model's measured bounding box -- `[xmin, ymin, zmin, xmax,
    ymax, zmax]` in assembly axes, after it has been stood upright -- and
    returns the translation that puts:

      * its outer face exactly `port_space` from the board edge,
      * its underside on the board's top face,
      * its body centred across the board.

    Working from measured faces rather than the model's own origin is
    deliberate. A vendor model's datum can be anywhere -- this one's is
    not the footprint origin -- and assuming otherwise is what left the
    connectors sitting away from their launches.
    """
    x_min, y_min, z_min, x_max, y_max, z_max = bounding_box

    outer = frame.x_offset + sign * (half_length - port_space)
    # The outer face is whichever end points at the nearer board edge.
    dx = outer - (x_max if sign > 0 else x_min)
    dy = board_top - y_min
    dz = frame.z_offset - (z_min + z_max) / 2.0
    return [dx, dy, dz]
