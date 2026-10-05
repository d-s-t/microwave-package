# Driving Altium from the sweep

Everything here exists to let `base_parts/default.txt` generate the package PCB
and the chip, so a parameter sweep can rebuild real copper geometry per
combination instead of a featureless slab.

This file is the record of how Altium actually behaves when scripted. Almost
none of it is in Altium's documentation, and every item cost at least one failed
run to establish.

## Layout

| Path | What it is |
|---|---|
| `GenerateFromParams.pas` | The generator. Creates both boards from `params.txt`. |
| `MeasureFootprint.pas` | Reads a connector footprint out of a PcbLib into `vendor/footprint.txt`. |
| `VerifyGenerated.pas` | Reads the saved files back and counts what is in them. |
| `Ping.pas` | Does nothing. Launched ahead of each script to see whether Altium will take one (item 6). |
| `params.txt` | Written by `eigen_mode_analyze.altium_generation`. Not source — regenerated each run. |
| `generated/` | Output boards. Not tracked; recreated from `default.txt` every run. |
| `spikes/` | The feasibility probes, kept for their findings. |

## Running it

```bash
"C:\Program Files\Altium\AD26\X2.EXE" '-RScriptingSystem:RunScriptFile(FileName="<abs path>.pas"|ProcName="<ProcName>")'
```

From Python, use `eigen_mode_analyze.altium_generation.run_script`, which handles
the timeout and sentinel polling described below, and waits for Altium to be
free before it launches anything (item 6).

## The six things that will bite you

**1. The launcher is not the script.** Altium is single-instance. `X2.EXE`
forwards the request to the session already running and the stub exits as soon
as the session takes it, so its exit code says nothing about whether your script
ran, let alone finished. The only trustworthy completion signal is a sentinel
file the script writes itself. Poll for it; never wait on the process.

**2. There is no headless mode, and errors are silent-but-blocking.** A *parse*
error raises a modal dialog that waits for a human indefinitely. A *runtime*
error parks the script in the debugger. Neither produces a stack trace, an exit
code, or anything on stdout. Consequences:

- Every call must be bounded by a timeout, not a wait.
- Write progress to the sentinel **before** each risky call and flush it. When a
  script dies, the last line it managed to write is the entire diagnostic.
- Wrong *arity* on a method raises a catchable exception rather than a dialog,
  so many candidate spellings can be swept safely in one run. Unknown
  *identifiers* cannot — they take the whole script down.

**3. Pascal block comments do not nest.** An inner `}` closes the comment early
and the prose after it is parsed as code. This produced
`Error in declaration block at Line:9` from a header comment containing
`{ a property, no args }`.

**4. Order matters on a cold start.** With no PCB document open, the PCB server
is not loaded and `PCBServer` is not even a declared identifier — the script dies
on `Undeclared identifier: GetCurrentPCBBoard` before any `Try` can catch it.
Open a document through `Client` *first*. A warm session hides this completely,
because a document is already open.

**5. Stale open documents lie to you.** This one cost two separate debugging
rounds, and it is the failure mode most likely to waste your afternoon.

`Client.OpenDocument` on a path Altium already has open returns the in-memory
copy rather than reading the file, and `GetCurrentPCBBoard` may return a
*different*, previously focused board. A correctly generated 62-via board once
read back as the previous run's single track.

Worse, closing *one* document is not enough. Documents accumulate across runs all
claiming the same path, so the copper export later reported a board exactly as it
stood one run earlier — before its bond fingers existed — while the file on disk
was perfectly correct. Drain them:

```pascal
For i := 1 To 10 Do
Begin
    Doc := Client.GetDocumentByPath(Path);
    If Doc = Nil Then Break;
    Client.CloseDocument(Doc);
End;
```

Better still, close each document as soon as it is saved, so stale copies never
accumulate in the first place. The generator does this.

The general lesson: **measure the file, not the session.** `GenerateFromParams`
counts its own primitives in memory before saving and `VerifyGenerated` counts
them again from disk, because "the objects were never added" and "they were added
but something else was read back" look identical from the outside.

**6. Giving up on a script does not stop it — and the next one runs inside it.**
A timeout on the Python side is only Python giving up. Altium carries on, and a
script launched meanwhile does not wait for it to end: its stub waits only until
the running script next lets Windows messages through, and Altium then runs the
new script right there, nested inside the old one. Closing a document is enough
to let it in. Watched on 2026-09-27 with a 70 × 30 mm board given an 8 s timeout
and the default board launched straight after: the second generator started as
the first closed its package board, drew and saved its own, and handed back to
the first, which finished by saving over the chip. The second call then read the
first one's report as its own (2839 stitching vias, on a board that has 169).
That is how a sweep lost its first combination to the previous run's script with
"produced no output at all", and why later launches piled up behind it.

Established against AD26 on the way to the fix:

- A script that never lets messages through (a plain busy loop) *does* hold
  later requests back until it ends, in order. That is why this looked like a
  queue at first, and it is not what the generator does.
- A stub that has not been taken yet can be killed, and that withdraws its
  request: the script never runs.
- While a script sits on a modal dialog, no stub is taken. One already waiting
  when the dialog closed stayed stuck for about 90 s, and held up a second stub
  behind it. One sent after the dialog closed got through at once.
- When Altium is free, a stub is taken in about a second.

So `run_script` never launches until two things hold. **Every report of ours has
reached `stage=end`**, which is the only thing that says a script has really
finished. A report written in the last two seconds is being written and is not
opened (see the comment in `run_script`: a read that collides with
`SaveToFile` is a modal dialog). A report nobody has written for the whole
waiting budget, or written before this Altium session started, belongs to a
script that is no longer running. **And `Ping.pas`, which does nothing, gets
through.** That is what catches a dialog. The ping's stub is withdrawn and
re-sent every 30 s, so a closed dialog is noticed promptly. At the timeout, a
script whose stub was never taken is withdrawn rather than left to run later.
Within one process (the web app runs every run's pipeline in its own thread),
a lock keeps two runs' Altium jobs from interleaving too.

## API spellings that work

Established by probing; the plausible-looking alternatives all fail.

```pascal
{ Documents -- FOUR arguments. The three-argument form returns nil. }
Doc := Client.OpenNewDocument('PCB', 'package', 'package', False);

{ Saving -- both DoFileSaveAs spellings fail. }
Doc.SetFileName(Path);
Doc.DoFileSave('');

{ Primitives }
T := PCBServer.PCBObjectFactory(eTrackObject, eNoDimension, eCreate_Default);
Board.AddPCBObject(T);
PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
                              PCBM_BoardRegisteration, T.I_ObjectAddress);

{ Region outlines }
C := PCBServer.PCBContourFactory;
C.AddPoint(MMsToCoord(x), MMsToCoord(y));
R.SetOutlineContour(C);

{ Region kinds -- a region is only a cutout or a cavity if you say so }
R.Kind := eRegionKind_BoardCutout;   { on eKeepOutLayer }
R.Kind := eRegionKind_Cavity;        { on a mechanical layer }
R.CavityHeight := MMsToCoord(depth); { only means anything on a Cavity }
```

`TRegionKind` is `eRegionKind_Copper`, `_Cutout`, `_NamedRegion`,
`_BoardCutout`, `_Cavity`. Altium's published *PCB API Types* page lists
a three-value `TPolyRegionKind` and no cavity at all, so these five were
read straight out of `AD26/System/Advpcb.dll` instead of trusted from the
documentation:

```powershell
$a = [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes("C:\Program Files\Altium\AD26\System\Advpcb.dll"))
[regex]::Matches($a, 'eRegionKind_[A-Za-z0-9_]+') | % { $_.Value } | Sort-Object -Unique
```

Worth the trouble, and worth repeating for any constant you are not sure
of: **a wrong identifier is a parse error, which is the modal-dialog
kind.** No `Try` around it will ever run, and the run costs a full
timeout. A wrong *member* (`R.SomeProperty`) is resolved at run time and
does land in the `Except`, which is why `AddCavity` wraps every call and
reports each one separately but names its constants bare.

The same trick settles layer constants: `eKeepOutLayer`, `eMechanical1`
and the rest of `TLayer` live together in `Altium.SDK.dll` and
`Altium.Edp.Interfaces.dll`, not in `Advpcb.dll`.

Wrap batches of edits in `PCBServer.PreProcess` / `PostProcess`.

### The board outline, and a trap worth remembering

`BoardOutline.Segments[i]` returns the record **by value**. Writing
`Segments[i].vx := ...` edits a temporary that is then discarded — and it reports
success while leaving the board at its default 6 × 4 inches. Read the record out,
edit it, assign it back:

```pascal
Seg := Board.BoardOutline.Segments[i];
Seg.Kind := ePolySegmentLine;
Seg.vx := MMsToCoord(x);
Seg.vy := MMsToCoord(y);
Board.BoardOutline.Segments[i] := Seg;
```

Use **four** corner points for a rectangle, not five. Altium closes the outline
itself.

## Conventions the generator keeps

**Both boards are centred on the origin.** This is the only datum shared with
SolidWorks. Copper is positioned in HFSS by arithmetic rather than by mates, so a
drifting centre would put copper in the wrong place with nothing to catch it.

A side effect: the boards sit at negative coordinates, so a freshly opened
document **looks empty** until the view is fitted. Press `Ctrl+PgDn`
(*View » Fit Board*). The document is not broken.

**The generated `.PcbDoc` files are output, never source.** They are recreated
from `default.txt` on every run and are not tracked in git. Do not hand-edit
them — change `default.txt` or the generator.

**The CPW gap is never a drawn input.** It is solved for 50 Ω in
`eigen_mode_analyze/cpw.py`, per substrate, and arrives as `pcb_cpw_gap` and
`chip_cpw_gap`. Copper thickness matters here more than it looks: 35 µm across a
~112 µm gap moves the 50 Ω gap to 271 µm, so ignoring it lands you at 35 Ω.

**The launch is the vendor's own footprint, placed from the vendor's own
library.** Not four pads standing where the lands are — a real component, with
its designator (`J1`/`J2`), paste and mask layers, silkscreen, courtyard and 3D
body. That is the difference between a board that describes the fields correctly
and a board somebody can have made: DRC, the BOM, the assembly drawing and the
3D/STEP export all need a component, and no output job can invent one from loose
pads.

The library is `vendor/Altium_WR-SMP (rev22a).PcbLib`, extracted from the shipped
IntLib by `vendor/extract_intlib.py` — an IntLib is a compiled compound document
(and its embedded PcbLib is zlib-wrapped inside it), so Altium cannot open one as
a library directly.

Worth knowing: **Würth ship different land patterns for this part in their Altium
and KiCad libraries.** Four 0.5 mm square ground pads in a ±2.6 land with an
r=2.30 anti-pad, against a U of 0.3 × 4.0 bars in a ±2.65 land with r=2.25. The
signal pad is identical in both, and both come to the same −2.60 outermost
copper, which is why moving to the Altium one left the launch position, the via
fence and the HFSS connector placement untouched. The Altium one is
authoritative here because it is the one being placed.

**Nothing in this script names a manufacturer,** and neither does Python. The
library path and footprint name arrive in `params.txt`; the board around the
launch is drawn from six values, and those are *measured*, not typed.

`MeasureFootprint.pas` reads the named footprint inside Altium and writes
`vendor/footprint.txt` — every pad, the ground land's vertices, the 3D body.
`eigen_mode_analyze/smp.py` derives the rest from that file: the envelope, the
anti-pad radius, the escape slot. Run it with
`altium_generation.measure_footprint()`; a sweep never does, because its output
is a committed source file that only changes when the connector does.

**Which way the vendor drew it is not assumed.** The frame is taken from pin 1 —
the centre conductor, the one convention every coax launch shares — and the
pattern rotated until that pad lies on +v. A rotation, never a reflection. Würth
point the pin at −y; the next manufacturer need not.

A footprint this board cannot build a launch around is refused by name, which is
the point: three of the nine footprints in this library are, each for a real
reason. `60124002122502` is through-hole (pads on the multi-layer with a 1.3 mm
drill, which nothing here drills for) and `60114202122305` is an edge-mount
variant with three copper shapes instead of one.

That last pair cost a bug worth recording: `IsCopper` originally accepted only
the top and bottom layers, so **a through-hole footprint measured as having no
pads at all** and passed silently. Through-hole pads live on `eMultiLayer`.

Two traps the placement cost:

- A replicated primitive must be added to **both** the component and the board.
  With only `Comp.AddPCBObject`, the footprint is placed, looks perfectly right,
  and is invisible to every board iterator — so it never reaches `copper.txt`
  and never reaches the field solve. The in-memory pad count is what caught it.
- `TopXSize`/`TopYSize` are in the **pad's** frame, not the board's. A rotated
  1.4 × 0.48 signal land still reports 0.48 × 1.4, so `ExportCopper` now writes
  `Pad.Rotation` and the builder works from the pad's corners. Without it the
  launch is built across the line instead of along it, at full width into both
  grounds.

**The die's opening is a real Altium feature, not a note for later.** A
**Board Cutout** region on the keep-out layer where the die is as thick as the
board or thicker, and an **embedded Cavity** region on Mechanical 1 carrying the
milled depth in `CavityHeight` where it is thinner. It used to be cut only in
HFSS, which left the `.PcbDoc` claiming a board that was never going to be made:
ground plane, trace and back plane all ran straight across the hole.

Everything that used to cross it now stops at it — the centre conductor picks up
again on the far side, both coplanar planes step out around it, and the back
plane is split into four pieces where the cut goes through. (Four, not one with a
hole in it: `ExportCopper` reads `GetMainContour`, so a region's interior holes
do not survive the export.)

The opening is **larger than the die by `cavity_clearance` on every side** — a
die and a milled pocket of equal size do not assemble. Depth gets no matching
allowance: cutting deeper does not help the die fit, it drops the die below the
board's top face and lengthens every bond wire.

Which of the two shapes, how big and how deep are all decided in
`eigen_mode_analyze/cavity.py` and arrive in `params.txt` already decided, so
this script only draws. HFSS asks that same module the same question when it
builds the model, which is the only reason the hole in the model is the feature
on the board. The rule is *not* `chip_width >= pcb_width`: a pocket leaving less
than `MIN_FLOOR` of laminate under the die cannot be milled, so it becomes a
cutout instead — and with the shipped defaults the two thicknesses are the same
number, sitting exactly on that knife edge.

**Stitching and shielding vias are placed directly, not via Altium's Tools ▸
Via Stitching/Shielding menu.** Those commands act on a selected *net*, and
nothing this generator draws carries one (see `Announce`) — there is no net for
them to select. They are also dialog-only, with no scriptable path found that
skips showing it, and a dialog is exactly what a script with no headless mode
cannot risk opening. `AddViaLattice` places every via, one at a time through
`TryVia`, which checks each candidate against the board itself — every pad,
track and via already placed, via `IPCB_Primitive.BoundingRectangle`,
restricted to eTopLayer/eBottomLayer/eMultiLayer so a footprint's silkscreen
and courtyard outlines don't count as copper — rather than trusting a
hand-derived shape or clearance to still be right. It runs **last**, after the
connector launch and the bond pads are on the board, precisely so that check has
something real to check against; running earlier is the mistake that once let
the fence land on pads neither it nor a via already placed knew existed.

**The vias are one lattice, laid out from the route outward and mirror-symmetric.**
Row 0 is the fence, `via_gnd_clearance` outside each edge of the CPW gap; row *K*
is *K* steps (`via_max_spacing`) further out on each side, and alternate rows
start half a step over. Along a row the sites are anchored at the board's centre,
and each is worked out for one quadrant and tried in all four, so the pattern is
the same on both sides of the line and at both ends of the board. What counts as
"the fence" is only row 0 between the two signal lands (`package_shielding_vias`
in the report); the rest is `package_stitching_vias`. A site `TryVia` refuses is
left out, so around the die's opening, the bond fingers and the connector the
pattern is the same lattice with the pieces that do not fit taken away, not a
shifted one.

It used to be two grids — a fence stepped from one connector's land, and a fill
stepped from the board's *corner* — which put the first fill row 1.2 mm from the
fence on one side of the line and 0.6 mm on the other, and left 1602 of 1670 vias
on the 50 × 25 mm board with no mirror image. The total is unchanged (1670 there;
2936 against 2959 on 70 × 30), because the lattice keeps the old one's density.

`via_min_spacing` is the floor `TryVia` enforces between any two vias and between
a via and everything else, **the board edge included**: the last row falls
wherever the steps leave it, which can be right at the edge, and a drilled hole
breaking the outline cannot be made. `via_max_spacing` is the lattice step, and so
the practical ceiling on spacing in open copper — together they are what stops two
accepted vias landing on top of each other, and what a suppression target's
wavelength sets a maximum spacing for. A via that sits *exactly* `via_min_spacing`
from something is accepted (`GapTolerance`): the lattice lands on round numbers,
where that case is common rather than rare. Whether the fence fits the board at
all, edge margin included, is `constraints.check_layout`'s question, asked before
Altium runs — because a fence that does not fit leaves no first row, and so no
vias, not a short fence.

## What the spikes settled

`spikes/s1_launch.pas` — Altium can be driven from the command line at all.
Roughly 3 s on a warm session against a minute cold, because a warm session is
reused rather than replaced.

`spikes/s2_probe.pas` — the PCB API is fully readable from an externally launched
script. Also: `Board.LayerStack` raises where `LayerStack_V7` succeeds.

`spikes/s2_export*.pas` — the 3D export. **Altium's STEP export silently drops
all copper**, so STEP cannot carry the traces and vias into simulation.
Parasolid can, and `PCBServer.Board3DModelExporter` returns a live exporter whose
option setters all work — including
`eBoard3DModelExportCopperOptions_CopperLayer`, which would split copper per
layer. But `ExportBoard3DModel` itself could not be reached across eight arities,
and neither could `GetCopperBodyList`. Setters taking an enum bind; anything
returning an interface or performing the export does not, which reads as a
partially exposed interface rather than eight unlucky guesses.

Hence the current plan: read the primitives out of Altium (which works — the
probe enumerated 262 vias on the reference board) and rebuild copper directly in
HFSS with PyAEDT. SolidWorks kept building the box, lid, vacuum and substrates,
and kept its mates, for a while; only copper moved at first. The substrates
moved to PyAEDT next (see `copper_geometry.build_substrates`), and later still
the lid did too — `lid_inside` replaces the old lid/chip_lid pair, is exported
from its own part with no assembly involved, and is positioned in AEDT by
measuring its imported geometry (`model_builder.place_lid_inside`), the same
way `place_connectors` already positions the SMP connector. The vacuum fill
followed for the same reason: `vacuum.SLDPRT` turned out not to be linked to
`default.txt` at all, so `model_builder.build_vacuum` now builds it from box's
own measured envelope instead. SolidWorks' only remaining job is rebuilding
box.SLDPRT and lid_inside.SLDPRT per combination and exporting each straight to
its own STEP file (`step_generation.py`) — there is no assembly and no mate in
this pipeline any more.

### Wire bonds are modelled but not scriptable

AD26 clearly models bond wires: it has wirebond rules, violation types
(`eViolation_WirebondLength`, `eViolation_BondFingerToWireAlignment`), JEDEC
profile constants, and export support. But there is **no wirebond entry in the
scriptable object-id enum at all** — of its 52 object ids, none is a wirebond, so
`PCBObjectFactory` cannot create one. The same partial exposure that blocked the
3D exporter.

This turned out not to matter. A bond is EM geometry rather than layout, so
`copper_geometry.build_wirebonds` builds the loops directly in HFSS from
`bond_count`, `bond_height` and `bond_wire_diameter`, where the arc profile can
be exactly what the field solve needs. What the layout owes the bonds is the pads
at each end, and those the generator does place.
