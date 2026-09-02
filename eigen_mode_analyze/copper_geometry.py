"""
Copper geometry read out of Altium, and rebuilt in HFSS.

Altium cannot hand us copper as CAD. Its STEP export drops copper by
design, and the Parasolid exporter -- which does support copper, even
per layer -- exposes its option setters to scripting but not the call
that performs the export (see `base_parts/altium/README.md`). Reading
the primitives, however, works perfectly.

So the copper crosses as data and is rebuilt directly in HFSS. That is
not merely a workaround; it is better in three ways than the CAD round
trip would have been:

  * every solid gets exactly the name its rule in `model_builder`
    expects, instead of whatever a STEP translator decided to call it,
  * nothing depends on imported face ids, which change on every rebuild
    and are what makes face-based mates fragile,
  * the copper thickness comes from the same `default.txt` as the rest
    of the geometry.

Coordinates arrive in millimetres relative to the board centre -- the
datum shared with SolidWorks. Only the builder knows where a board sits
in the assembly, so Z is applied here rather than in Altium.
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


# --- rebuilding it in HFSS -------------------------------------------
#
# One HFSS solid per record would give thousands of objects and a name
# per object, which neither the material rules nor the mesher want.
# Everything on a layer is therefore united into a single body carrying
# the name that `model_builder.MATERIAL_RULES` looks for.


@dataclass(frozen=True)
class Placement:
    """Where a board's copper sits in the assembly, in millimetres.

    `z_top` is the upper face of the substrate; top-layer copper is
    grown upward from it and bottom-layer copper downward from
    `z_bottom`. `x_offset`/`y_offset` carry the board's position in the
    assembly, which is zero while the board is centred on the origin --
    the case today, and the reason the datum convention matters.
    """
    z_top: float
    z_bottom: float
    thickness: float
    x_offset: float = 0.0
    y_offset: float = 0.0


def build(
    modeler,
    copper: BoardCopper,
    placement: Placement,
    prefix: str,
    log=logger.info,
) -> Dict[str, List[str]]:
    """Create the copper of one board in HFSS.

    `modeler` is a PyAEDT modeler. Returns the object names created, per
    body, so the caller can check that the names the material rules
    expect actually exist.
    """
    created: Dict[str, List[str]] = {}

    for layer, sign in ((TOP, +1.0), (BOTTOM, -1.0)):
        tracks, pads, regions = copper.on_layer(layer)
        if not (tracks or pads or regions):
            continue

        base = placement.z_top if layer == TOP else placement.z_bottom
        height = sign * placement.thickness
        parts: List[str] = []

        for region in regions:
            parts.append(_extrude_polygon(modeler, region.points, base, height, placement))
        for track in tracks:
            parts.extend(_extrude_track(modeler, track, base, height, placement))
        for pad in pads:
            parts.append(
                _extrude_rect(
                    modeler, pad.x - pad.xsize / 2, pad.y - pad.ysize / 2,
                    pad.x + pad.xsize / 2, pad.y + pad.ysize / 2,
                    base, height, placement,
                )
            )

        name = f"{prefix}_copper_{layer}"
        created[name] = _unite_as(modeler, parts, name)

    if copper.vias:
        parts = [
            modeler.create_cylinder(
                orientation="Z",
                origin=[via.x + placement.x_offset, via.y + placement.y_offset, placement.z_bottom],
                radius=via.diameter / 2.0,
                height=placement.z_top - placement.z_bottom,
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


def _shift(placement: Placement, x: float, y: float) -> Tuple[float, float]:
    return x + placement.x_offset, y + placement.y_offset


def _extrude_rect(modeler, x1, y1, x2, y2, base, height, placement) -> str:
    x1, y1 = _shift(placement, x1, y1)
    x2, y2 = _shift(placement, x2, y2)
    return modeler.create_box(
        origin=[min(x1, x2), min(y1, y2), base],
        sizes=[abs(x2 - x1), abs(y2 - y1), height],
    ).name


def _extrude_polygon(modeler, points, base, height, placement) -> str:
    shifted = [list(_shift(placement, x, y)) + [base] for x, y in points]
    poly = modeler.create_polyline(points=shifted, close_surface=True, cover_surface=True)
    modeler.thicken_sheet(poly, height)
    return poly.name


def _extrude_track(modeler, track: Track, base, height, placement) -> List[str]:
    """A track is a rectangle plus a round cap at each end.

    The caps are not decoration: without them a fence of tracks meeting
    at an angle leaves wedge-shaped gaps in the ground, and a gap in a
    ground plane is exactly the kind of defect that moves a mode.
    """
    x1, y1 = _shift(placement, track.x1, track.y1)
    x2, y2 = _shift(placement, track.x2, track.y2)
    half = track.width / 2.0

    dx, dy = x2 - x1, y2 - y1
    length = (dx * dx + dy * dy) ** 0.5
    if length == 0:
        return [
            modeler.create_cylinder(
                orientation="Z", origin=[x1, y1, base], radius=half, height=height
            ).name
        ]

    # Unit normal, to walk out to the edges of the track.
    nx, ny = -dy / length * half, dx / length * half
    corners = [
        [x1 + nx, y1 + ny, base],
        [x2 + nx, y2 + ny, base],
        [x2 - nx, y2 - ny, base],
        [x1 - nx, y1 - ny, base],
    ]
    poly = modeler.create_polyline(points=corners, close_surface=True, cover_surface=True)
    modeler.thicken_sheet(poly, height)

    names = [poly.name]
    for x, y in ((x1, y1), (x2, y2)):
        names.append(
            modeler.create_cylinder(
                orientation="Z", origin=[x, y, base], radius=half, height=height
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
    """One row of bonds, from a board pad across to a die pad."""
    x_start: float
    x_end: float
    z_start: float
    z_end: float
    count: int
    pitch: float
    height: float
    diameter: float


def bond_path(spec: BondSpec, y: float, segments: int = 12) -> List[List[float]]:
    """Points along one bond loop.

    A real bond leaves the pad steeply, arcs over, and comes down onto
    the die -- it is not a straight line, and the height of the loop is
    what keeps it clear of the die edge. A half sine gives that shape
    with the apex at `height` above the higher of the two ends, which is
    close enough to a JEDEC profile for a field solve and needs no
    special-casing when the two ends sit at different heights.
    """
    import math

    points = []
    for i in range(segments + 1):
        t = i / segments
        x = spec.x_start + (spec.x_end - spec.x_start) * t
        z = spec.z_start + (spec.z_end - spec.z_start) * t
        z += spec.height * math.sin(math.pi * t)
        points.append([x, y, z])
    return points


def build_wirebonds(modeler, spec: BondSpec, log=logger.info) -> List[str]:
    """Create one row of bond wires as swept circular profiles."""
    names = []
    for i in range(spec.count):
        y = (i - (spec.count - 1) / 2.0) * spec.pitch
        wire = modeler.create_polyline(
            points=bond_path(spec, y),
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
