"""
The board geometry Altium describes, rebuilt in HFSS.

Altium cannot hand us solids. Its STEP export drops copper by design,
and the Parasolid exporter -- which does support copper, even per layer
-- exposes its option setters to scripting but not the call that
performs the export (see `base_parts/altium/README.md`). Reading the
primitives, however, works perfectly.

So the layout crosses as data and is rebuilt here: substrates, copper,
vias and wire bonds. That is better than the CAD round trip would have
been in three ways:

  * every solid gets exactly the name its rule in `model_builder`
    expects, instead of whatever a STEP translator decided to call it,
  * nothing depends on imported face ids, which change on every rebuild
    and are what makes face-based mates fragile,
  * substrate and copper are built in one frame from one set of
    parameters, so they cannot drift apart.

The coordinate frames are the subtle part, and getting them wrong does
not look wrong -- it produces a model that solves and reports plausible
nonsense. See `Frame`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("eigen_mode_analyze")

TOP = "top"
BOTTOM = "bottom"


@dataclass(frozen=True)
class Track:
    layer: str
    x1: float
    y1: float
    x2: float
    y2: float
    width: float


@dataclass(frozen=True)
class Via:
    x: float
    y: float
    diameter: float
    hole: float


@dataclass(frozen=True)
class Pad:
    layer: str
    x: float
    y: float
    xsize: float
    ysize: float


@dataclass(frozen=True)
class Region:
    layer: str
    points: Tuple[Tuple[float, float], ...]


@dataclass
class BoardCopper:
    """Everything conductive on one board."""
    name: str
    tracks: List[Track] = field(default_factory=list)
    vias: List[Via] = field(default_factory=list)
    pads: List[Pad] = field(default_factory=list)
    regions: List[Region] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.tracks) + len(self.vias) + len(self.pads) + len(self.regions)

    def on_layer(self, layer: str) -> Tuple[List[Track], List[Pad], List[Region]]:
        return (
            [t for t in self.tracks if t.layer == layer],
            [p for p in self.pads if p.layer == layer],
            [r for r in self.regions if r.layer == layer],
        )


class CopperParseError(ValueError):
    """A malformed record, reported with its line number.

    Worth being strict about: a silently skipped via is a missing piece
    of shielding that nothing downstream would notice.
    """


def parse(text: str) -> Dict[str, BoardCopper]:
    """Parse the file written by `base_parts/altium/ExportCopper.pas`."""
    boards: Dict[str, BoardCopper] = {}
    current: Optional[BoardCopper] = None

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("board="):
            name = line.partition("=")[2].strip()
            current = boards.setdefault(name, BoardCopper(name=name))
            continue

        if current is None:
            raise CopperParseError(f"line {number}: record before any 'board=' line")

        kind, _, rest = line.partition("|")
        fields = rest.split("|")
        try:
            if kind == "track":
                layer, x1, y1, x2, y2, w = fields
                current.tracks.append(
                    Track(layer, float(x1), float(y1), float(x2), float(y2), float(w))
                )
            elif kind == "via":
                x, y, dia, hole = fields
                current.vias.append(Via(float(x), float(y), float(dia), float(hole)))
            elif kind == "pad":
                layer, x, y, xs, ys = fields
                current.pads.append(Pad(layer, float(x), float(y), float(xs), float(ys)))
            elif kind == "region":
                layer, coords = fields
                numbers = [float(v) for v in coords.split(",") if v != ""]
                if len(numbers) < 6 or len(numbers) % 2:
                    raise ValueError(f"{len(numbers)} coordinates")
                points = tuple(
                    (numbers[i], numbers[i + 1]) for i in range(0, len(numbers), 2)
                )
                current.regions.append(Region(layer, points))
            else:
                raise CopperParseError(f"line {number}: unknown record type {kind!r}")
        except CopperParseError:
            raise
        except (ValueError, TypeError) as exc:
            raise CopperParseError(f"line {number}: bad {kind} record ({exc}): {line!r}") from None

    return boards


def read_file(path: str) -> Dict[str, BoardCopper]:
    with open(path, "r", encoding="utf-8") as f:
        return parse(f.read())


# --- placing it in the model -----------------------------------------
#
# Altium and SolidWorks do not agree on which way is up, and that
# disagreement is invisible if you get it wrong: the model still solves,
# and reports plausible nonsense with the copper lying in the wrong
# plane entirely.
#
# Altium works in (u, v, w): u along the feedline, v across it, w normal
# to the board. The SolidWorks assembly has Y normal to the board and X
# along the feedline. Mapping u to X and w to Y forces the third axis --
# for a right-handed frame Z must be -v, since u x w = -v. Nothing here
# may quietly "tidy away" that sign.


@dataclass(frozen=True)
class Frame:
    """Maps Altium board coordinates into assembly coordinates.

    `x_offset` and `z_offset` carry the board centre to where it sits in
    the box. The assembly origin is a corner of the box, so these are
    half the box size rather than zero: the board is centred in the
    cavity, not on the origin.
    """
    x_offset: float = 0.0
    z_offset: float = 0.0

    def point(self, u: float, v: float, w: float) -> List[float]:
        """One Altium point in assembly coordinates."""
        return [u + self.x_offset, w, -v + self.z_offset]

    def box(self, u1: float, v1: float, u2: float, v2: float,
            w_base: float, w_height: float) -> Tuple[List[float], List[float]]:
        """A slab in the board plane, as (origin, sizes) for a box.

        Sizes come back positive with the origin at the low corner,
        which is what the modeller wants however the inputs were ordered
        and whichever way the axis flip turned them.
        """
        p1 = self.point(u1, v1, w_base)
        p2 = self.point(u2, v2, w_base + w_height)
        origin = [min(p1[i], p2[i]) for i in range(3)]
        sizes = [abs(p2[i] - p1[i]) for i in range(3)]
        return origin, sizes


@dataclass(frozen=True)
class Stack:
    """Where each face of the stack sits along the up axis.

    The die lies IN the board, not on it: the PCB is pocketed to the
    depth of the die so their top faces finish flush. That is what keeps
    the wire bonds short and roughly level, and it is why `chip_top` and
    `pcb_top` are normally equal.
    """
    pcb_bottom: float
    pcb_top: float
    chip_bottom: float
    chip_top: float
    copper_thickness: float
    chip_metal_thickness: float


def build_substrates(
    modeler,
    frame: Frame,
    stack: Stack,
    pcb_half_length: float,
    pcb_half_side: float,
    chip_half_length: float,
    chip_half_side: float,
    log=logger.info,
) -> List[str]:
    """Create the PCB and die dielectrics, with the die in its pocket.

    Named `pcb_substrate` and `chip_substrate` so the material rules give
    them the board and die permittivities.

    Altium would have been the natural place to build these, but its
    solid export is precisely the call that cannot be reached from a
    script. Building them here is the better answer anyway: the pocket
    can be cut exactly, and substrate and copper share one frame, so
    they cannot drift apart.
    """
    origin, sizes = frame.box(
        -pcb_half_length, -pcb_half_side, pcb_half_length, pcb_half_side,
        stack.pcb_bottom, stack.pcb_top - stack.pcb_bottom,
    )
    pcb = modeler.create_box(origin=origin, sizes=sizes)
    pcb.name = "pcb_substrate"

    pocket_origin, pocket_sizes = frame.box(
        -chip_half_length, -chip_half_side, chip_half_length, chip_half_side,
        stack.chip_bottom, stack.chip_top - stack.chip_bottom,
    )
    pocket = modeler.create_box(origin=pocket_origin, sizes=pocket_sizes)
    modeler.subtract("pcb_substrate", [pocket.name], keep_originals=False)

    chip_origin, chip_sizes = frame.box(
        -chip_half_length, -chip_half_side, chip_half_length, chip_half_side,
        stack.chip_bottom, stack.chip_top - stack.chip_bottom,
    )
    chip = modeler.create_box(origin=chip_origin, sizes=chip_sizes)
    chip.name = "chip_substrate"

    log(
        "Built pcb_substrate (pocketed for the die) and chip_substrate, "
        f"top faces flush at {stack.chip_top:g} mm."
    )
    return ["pcb_substrate", "chip_substrate"]


def build(
    modeler,
    copper: BoardCopper,
    frame: Frame,
    top_face: float,
    bottom_face: float,
    thickness: float,
    prefix: str,
    log=logger.info,
) -> Dict[str, List[str]]:
    """Create one board's copper.

    Top-layer copper grows outward from `top_face`, bottom-layer copper
    outward from `bottom_face`, and vias span between them. Returns the
    names created, so the caller can check they are the ones the material
    rules match on.
    """
    created: Dict[str, List[str]] = {}

    for layer, base, height in (
        (TOP, top_face, thickness),
        (BOTTOM, bottom_face, -thickness),
    ):
        tracks, pads, regions = copper.on_layer(layer)
        if not (tracks or pads or regions):
            continue

        parts: List[str] = []
        for region in regions:
            parts.append(_extrude_polygon(modeler, region.points, base, height, frame))
        for track in tracks:
            parts.extend(_extrude_track(modeler, track, base, height, frame))
        for pad in pads:
            origin, sizes = frame.box(
                pad.x - pad.xsize / 2, pad.y - pad.ysize / 2,
                pad.x + pad.xsize / 2, pad.y + pad.ysize / 2,
                base, height,
            )
            parts.append(modeler.create_box(origin=origin, sizes=sizes).name)

        name = f"{prefix}_copper_{layer}"
        created[name] = _unite_as(modeler, parts, name)

    if copper.vias:
        # Vias run along the up axis, which is Y here rather than Z.
        parts = [
            modeler.create_cylinder(
                orientation="Y",
                origin=frame.point(via.x, via.y, bottom_face),
                radius=via.diameter / 2.0,
                height=top_face - bottom_face,
            ).name
            for via in copper.vias
        ]
        name = f"{prefix}_vias"
        created[name] = _unite_as(modeler, parts, name)

    log(
        f"Built {len(copper)} copper records for {copper.name} as "
        + ", ".join(sorted(created))
    )
    return created


def _extrude_polygon(modeler, points, base, height, frame: Frame) -> str:
    poly = modeler.create_polyline(
        points=[frame.point(u, v, base) for u, v in points],
        close_surface=True,
        cover_surface=True,
    )
    modeler.thicken_sheet(poly, height)
    return poly.name


def _extrude_track(modeler, track: Track, base, height, frame: Frame) -> List[str]:
    """A track is a rectangle plus a round cap at each end.

    The caps are not decoration: without them a run of tracks meeting at
    an angle leaves wedge-shaped gaps, and a gap in a ground plane is
    exactly the kind of defect that moves a mode.
    """
    half = track.width / 2.0
    du, dv = track.x2 - track.x1, track.y2 - track.y1
    length = (du * du + dv * dv) ** 0.5

    if length == 0:
        return [
            modeler.create_cylinder(
                orientation="Y",
                origin=frame.point(track.x1, track.y1, base),
                radius=half, height=height,
            ).name
        ]

    # Unit normal in the board plane, to reach the edges of the track.
    nu, nv = -dv / length * half, du / length * half
    corners = [
        frame.point(track.x1 + nu, track.y1 + nv, base),
        frame.point(track.x2 + nu, track.y2 + nv, base),
        frame.point(track.x2 - nu, track.y2 - nv, base),
        frame.point(track.x1 - nu, track.y1 - nv, base),
    ]
    poly = modeler.create_polyline(points=corners, close_surface=True, cover_surface=True)
    modeler.thicken_sheet(poly, height)

    names = [poly.name]
    for u, v in ((track.x1, track.y1), (track.x2, track.y2)):
        names.append(
            modeler.create_cylinder(
                orientation="Y", origin=frame.point(u, v, base),
                radius=half, height=height,
            ).name
        )
    return names


def _unite_as(modeler, parts: Sequence[str], name: str) -> List[str]:
    """Unite `parts` into one solid called `name`.

    The name is what matters: `model_builder.MATERIAL_RULES` matches on
    it, so a rename here silently changes a material.
    """
    parts = [p for p in parts if p]
    if not parts:
        return []
    if len(parts) > 1:
        modeler.unite(parts)
    modeler[parts[0]].name = name
    return [name]


# --- wire bonds -------------------------------------------------------
#
# Bonds are built here rather than in Altium. AD26 models bond wires --
# it has wirebond rules, violation types and JEDEC profile constants --
# but exposes no wirebond entry in its scriptable object-id enum, so a
# script cannot create one. That turned out not to matter: a bond is EM
# geometry, not layout, and building it here means the loop profile is
# exactly what the model needs and the solid is named `wirebond`, which
# is what the material rules look for.


@dataclass(frozen=True)
class BondSpec:
    """One row of bonds, from a board pad across to a die pad.

    `u_start`/`u_end` are along the feedline and `w_start`/`w_end` are
    the two pad heights -- equal, when the die sits flush in its pocket.
    """
    u_start: float
    u_end: float
    w_start: float
    w_end: float
    count: int
    pitch: float
    height: float
    diameter: float


def bond_path(spec: BondSpec, frame: Frame, v: float, segments: int = 12) -> List[List[float]]:
    """Points along one bond loop, in assembly coordinates.

    A real bond leaves the pad steeply, arcs over, and comes down on the
    die -- it is not a straight line, and the loop is what keeps it clear
    of the die edge. A half sine gives that shape with the apex `height`
    above the ends, and needs no special-casing when the two ends sit at
    different heights.
    """
    import math

    points = []
    for i in range(segments + 1):
        t = i / segments
        u = spec.u_start + (spec.u_end - spec.u_start) * t
        w = spec.w_start + (spec.w_end - spec.w_start) * t
        w += spec.height * math.sin(math.pi * t)
        points.append(frame.point(u, v, w))
    return points


def build_wirebonds(modeler, spec: BondSpec, frame: Frame, log=logger.info) -> List[str]:
    """Create one row of bond wires as swept circular profiles."""
    names = []
    for i in range(spec.count):
        v = (i - (spec.count - 1) / 2.0) * spec.pitch
        wire = modeler.create_polyline(
            points=bond_path(spec, frame, v),
            xsection_type="Circle",
            xsection_width=spec.diameter,
        )
        names.append(wire.name)

    if not names:
        return []
    if len(names) > 1:
        modeler.unite(names)
    modeler[names[0]].name = "wirebond"
    log(f"Built {spec.count} wire bonds as 'wirebond'")
    return ["wirebond"]
