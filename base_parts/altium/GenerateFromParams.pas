{ Generate the package PCB and the chip from default.txt.

  Reads the parameter file written by
  eigen_mode_analyze.altium_generation, creates both boards, and draws a
  shielded 50 ohm CPW on each.

  Conventions, and why they matter:

  * Both boards are CENTRED ON THE ORIGIN. This is the only datum shared
    with SolidWorks. Copper is positioned in HFSS by arithmetic, not by
    mates, so a drifting centre would put copper in the wrong place with
    nothing to catch it. It also means a board sits at negative
    coordinates, so a freshly opened document looks empty until the view
    is fitted -- Ctrl+PgDn, or View then Fit Board.

  * Idempotent. Documents are created fresh each run and are OUTPUT,
    never hand-edited, so there is no state to drift.

  * The CPW gap is never a drawn input. It is solved for 50 ohm in
    Python, per substrate, arriving as pcb_cpw_gap and chip_cpw_gap.

  * Bond pad positions are not computed here either. They come from
    bonds.txt, which HFSS also reads to build the wires, so a wire
    cannot land beside its pad.

  * The die's opening in the board is drawn HERE, as a real Altium
    feature -- a Board Cutout region where it goes through, an embedded
    Cavity region carrying its own depth where it does not. Its size,
    its depth and which of the two it is all arrive in the parameter
    file already decided (eigen_mode_analyze/cavity.py); this file only
    draws. Copper that used to run straight across that footprint is
    cut back here too, rather than downstream: a board document with a
    ground plane spanning its own cutout is wrong on its own terms,
    whatever a later reader does about it.

  * Every feature is guarded and counted separately, because Altium
    leaves no stack trace, no exit code and no dialog for a runtime
    error. The report file is the only diagnostic there is.

  * Nothing drawn here carries a NET. This is copper for a field solve,
    not a design ExportCopper's reader ever routes or fabricates, so no
    pad, track, via or region is ever assigned one -- see Announce and
    everything it is called after. That is also why the stitching and
    shielding vias are placed directly (AddViaLattice) rather than by
    calling Altium's own Tools -> Via
    Stitching/Shielding commands: those act on a selected NET, and a
    board with none has nothing for them to select. They are also
    dialog-driven, with no scriptable parameter path found for them
    that skips showing it -- and a dialog is exactly what this script
    cannot risk opening. Altium has no headless mode (see
    microwave_package/design/altium_generation.py): a dialog left on
    screen waits for a human forever, and every run behind it in the
    same sweep along with it.

  DelphiScript traps this file has already fallen into:

  * Block comments do not nest. An inner closing brace ends the header
    early and the prose after it is parsed as code.
  * There are no closures. A nested procedure reading a variable from
    its enclosing scope fails with "Can't access top level variable".
  * Continue, and Try/Finally wrapped around Try/Except, appear to leave
    the parser out of step; it then reports the confusion as a
    redeclaration at the NEXT declaration rather than where it happened.
  * A routine defined twice is not rejected outright but eventually
    breaks parsing in ways that point somewhere else entirely.

  API spellings established empirically:

      Client.OpenNewDocument -- four arguments; three returns nil
      BoardOutline.Segments[i] returns the record BY VALUE, so it must
          be read out, edited and assigned back
      PCBServer.PCBObjectFactory(kind, eNoDimension, eCreate_Default)
      PCBServer.PCBContourFactory for region outlines
      Doc.SetFileName then Doc.DoFileSave
      Region.Kind takes eRegionKind_Copper / _Cutout / _NamedRegion /
          _BoardCutout / _Cavity. The published API types list stops at
          three of those, so the five were read out of AD26's own
          Advpcb.dll rather than trusted from the documentation -- which
          matters, because an identifier that does not exist is not a
          catchable error here, it is a modal dialog.
      Region.CavityHeight is a TCoord, and is only meaningful on a
          region whose Kind is eRegionKind_Cavity.

  Altium internal units are 1/10000 mil; MMsToCoord converts.
}

Const
    ParamPath = '<ALTIUM_DIR>\params.txt';
    OutPath   = '<ALTIUM_DIR>\generate_report.txt';

    { How far the die's own copper (trace + coplanar grounds) is pulled
      in from the die's physical edge. The SUBSTRATE outline stays the
      die's real size -- only the copper on it shrinks.

      Without this, the die's copper ran exactly to the die edge, and
      the PCB's copper was cut back to the same edge (plus a small
      keep-out) to make room for the pocket. Two independently-drawn
      edges meeting at the same line is exactly what an interference
      check flags, coincident-face or not. Pulling the die's own copper
      in gives the two a real gap regardless of exactly how the PCB
      side's keep-out margin was computed.

      A fixed constant, not a parameter: it is a drafting margin, not a
      value that would ever have a reason to be tuned per board. Small
      enough that it stays inside where the bond pad already lands
      (chip_length/2 - bond_pad_length), so the wire still lands on
      copper rather than bare substrate -- and that relationship is
      exactly what CHIP_COPPER_INSET in eigen_mode_analyze/constraints.py
      checks up front. MUST MATCH that value; there is no shared import
      between Pascal and Python, so both copies carry this same
      cross-reference as the reminder. }
    ChipCopperInset = 0.15;

    { Facets per launch anti-pad arc. At r = 2.3 mm and roughly 145
      degrees of arc, 32 puts the chord 0.002 mm off the true circle --
      far below anything the mesh resolves. }
    VoidSteps = 32;

    { How far short of MinGap a gap may fall and still count as meeting
      it. A via on a grid step of exactly its own diameter plus MinGap
      (0.3 mm vias, 0.2 mm apart, every 0.5 mm) meets the rule exactly,
      and whether "exactly" came out as a hair over or a hair under
      after converting to Altium's integer units was left to rounding:
      alternate grid points were refused at random, and a 0.5 mm fill
      came out with 1 mm holes in it. One micron is far below anything
      a board house holds, and far above the rounding. }
    GapTolerance = 0.001;

    { ArcTan is relied on below; Pi is spelled out rather than assumed to
      exist, in keeping with the rest of this file's API caution. }
    PiVal = 3.14159265358979;

Var
    Rpt : TStringList;

    { The four corner screws' copper keep-outs, when screws_on is set:
      each screw's centre sits at (+-ScrewU, +-ScrewV) and no via may come
      within ScrewKeepR of it (the keep-out's radius). ScrewKeepR <= 0
      means no screws. Held here rather than threaded through TryVia and
      AddViaLattice's argument lists, which already carry a dozen
      numbers each. }
    ScrewU, ScrewV, ScrewKeepR : Double;

    { The copper round each screw hole is drawn as pieces, not as a plane
      with a hole in it (see AddScrewBlocks). ScrewBlockS is the half-size
      of the square, centred on the screw and running out to the board's
      corner, that the plane gives up to those pieces; it is zero when
      there are no screws AND while the die's board is being drawn, which
      is how the die's planes stay plain. PkgHalfL/PkgHalfS are the
      package's outline, the corner the notch is cut at. }
    ScrewBlockS, PkgHalfL, PkgHalfS : Double;

    { The connector library, held open across both launches.

      Globals rather than passed around because the footprint is a LIVE
      interface into the library document: it stays valid only while
      that document is open, so it cannot be fetched, closed and reused.
      Opening it once per launch would work too, but each open steals
      the front document, and the fewer times that happens the fewer
      chances there are of a primitive landing in the library. }
    SmpDoc  : IServerDocument;
    SmpLib  : IPCB_Library;
    SmpIter : IPCB_LibraryIterator;
    SmpComp : IPCB_LibComponent;

Procedure Say(S : String);
Begin
    Rpt.Add(S);
    Rpt.SaveToFile(OutPath);
End;

Function ParamOf(P : TStringList; Name : String; Default : Double) : Double;
Var
    V : String;
Begin
    V := P.Values[Name];
    If V = '' Then Result := Default Else Result := StrToFloat(V);
End;

{ One field of a pipe-separated record, 1-based. }
Function Field(S : String; Index : Integer) : String;
Var
    fieldNo, sep : Integer;
Begin
    Result := '';
    For fieldNo := 1 To Index Do
    Begin
        sep := Pos('|', S);
        If sep = 0 Then
        Begin
            Result := S;
            S := '';
        End
        Else
        Begin
            Result := Copy(S, 1, sep - 1);
            S := Copy(S, sep + 1, Length(S));
        End;
    End;
End;

{ Objects added from a script have to be announced, or they are not
  properly part of the document's data model. }
Procedure Announce(Board : IPCB_Board; Prim : IPCB_Primitive);
Begin
    PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast,
                                  PCBM_BoardRegisteration, Prim.I_ObjectAddress);
End;

Procedure AddTrack(Board : IPCB_Board; X1, Y1, X2, Y2, W : Double; Lay : TLayer);
Var
    T : IPCB_Track;
Begin
    T := PCBServer.PCBObjectFactory(eTrackObject, eNoDimension, eCreate_Default);
    T.X1 := MMsToCoord(X1);
    T.Y1 := MMsToCoord(Y1);
    T.X2 := MMsToCoord(X2);
    T.Y2 := MMsToCoord(Y2);
    T.Width := MMsToCoord(W);
    T.Layer := Lay;
    Board.AddPCBObject(T);
    Announce(Board, T);
End;

{ Whether (CX, CY) is one of the package's four outer corners while the
  screws' copper voids are being drawn -- the corners a plane gives up a
  notch at. }
Function IsScrewCorner(CX, CY : Double) : Boolean;
Begin
    Result := (ScrewBlockS > 0.0)
              And (Abs(Abs(CX) - PkgHalfL) < 0.0001)
              And (Abs(Abs(CY) - PkgHalfS) < 0.0001);
End;

{ One corner of a plane's outline, as one point or, at a screw corner,
  the three that step round the square the plane gives up there. The
  order depends on whether the outline arrives at the corner along a
  horizontal or a vertical edge. }
Procedure AddPlaneCorner(C : IPCB_Contour; CX, CY : Double; ArriveHorizontal : Boolean);
Var
    XN, YN, SX, SY : Double;
Begin
    If Not IsScrewCorner(CX, CY) Then
    Begin
        C.AddPoint(MMsToCoord(CX), MMsToCoord(CY));
        Exit;
    End;
    If CX > 0.0 Then SX := 1.0 Else SX := -1.0;
    If CY > 0.0 Then SY := 1.0 Else SY := -1.0;
    XN := SX * (Abs(ScrewU) - ScrewBlockS);
    YN := SY * (Abs(ScrewV) - ScrewBlockS);
    If ArriveHorizontal Then
    Begin
        C.AddPoint(MMsToCoord(XN), MMsToCoord(CY));
        C.AddPoint(MMsToCoord(XN), MMsToCoord(YN));
        C.AddPoint(MMsToCoord(CX), MMsToCoord(YN));
    End
    Else
    Begin
        C.AddPoint(MMsToCoord(CX), MMsToCoord(YN));
        C.AddPoint(MMsToCoord(XN), MMsToCoord(YN));
        C.AddPoint(MMsToCoord(XN), MMsToCoord(CY));
    End;
End;

{ A filled rectangle of copper. Ground planes are drawn as plain regions
  rather than polygon pours: a pour needs a net and a repour pass, and
  for a shape this simple the region is both exact and cheaper.

  A corner that is one of the package's outer corners gives up the
  square round a screw hole (see AddScrewBlocks). }
Procedure AddRect(Board : IPCB_Board; X1, Y1, X2, Y2 : Double; Lay : TLayer);
Var
    R : IPCB_Region;
    C : IPCB_Contour;
Begin
    R := PCBServer.PCBObjectFactory(eRegionObject, eNoDimension, eCreate_Default);
    C := PCBServer.PCBContourFactory;
    AddPlaneCorner(C, X1, Y1, False);
    AddPlaneCorner(C, X2, Y1, True);
    AddPlaneCorner(C, X2, Y2, False);
    AddPlaneCorner(C, X1, Y2, True);
    R.SetOutlineContour(C);
    R.Layer := Lay;
    Board.AddPCBObject(R);
    Announce(Board, R);
End;

{ One coplanar ground plane, with the connector's anti-pad bitten out of
  its inner edge.

  The plane is a rectangle from GndInner out to the board edge, except
  where a launch sits. There the vendor's land pattern calls for a
  circular void of VoidR about the connector axis -- the anti-pad that
  holds ground away from the centre pin and the dielectric around it,
  and which sets the impedance of the coax-to-coplanar transition.

  It has to be cut out HERE, as part of the plane's own outline, rather
  than drawn as a separate shape. Copper on a layer is united into one
  body, so a void is not something that can be added; it only exists if
  the metal around it was never drawn in the first place. Leaving it out
  does not leave a gap, it leaves metal -- the plane simply runs
  straight through where the vendor asked for 2.25 mm of clearance.

  The void is not an isolated hole, which is what makes a single contour
  enough: the CPW corridor already runs through its centre, so the two
  merge into one connected clearance and the plane stays simply
  connected. That matters beyond neatness -- ExportCopper reads
  GetMainContour, so a genuine hole in a region would be dropped from
  the export without a word.

  The vendor also cuts a slot of SlotHalf either side of the axis where
  the signal pad leaves the land, from the void out to the land's edge
  at LandHalf. That is wider than the CPW gap, so the plane steps out to
  it and back.

  The die's opening is bitten out of the same inner edge, between the
  two launches, for a different reason: there is no board there at all
  (or, for a blind pocket, no board at this level), so a plane running
  across it is copper over a hole. It is a plain rectangular step rather
  than an arc, and like the anti-pad it has to be part of THIS contour
  -- a region with a genuine interior hole would lose the hole on
  export, since ExportCopper reads GetMainContour.

  Side is +1 for the plane above the line and -1 for the one below.
  LaunchX <= 0 means this board has no connector -- the die -- and the
  plane then has no anti-pads. CavHalfL <= 0 means no opening, which is
  again the die: it is the part that goes IN the pocket. }
Procedure AddGroundPlane(Board : IPCB_Board;
                         Side, CopperHalfL, CopperHalfS, GndInner,
                         LaunchX, VoidR, SlotHalf, LandHalf,
                         CavHalfL, CavHalfS : Double);
Var
    Rgn        : IPCB_Region;
    C          : IPCB_Contour;
    XOut, XSlot : Double;
    A1, A2, Ang : Double;
    i          : Integer;
    HasLaunch, HasCavity : Boolean;
Begin
    Rgn := PCBServer.PCBObjectFactory(eRegionObject, eNoDimension, eCreate_Default);
    C := PCBServer.PCBContourFactory;

    HasLaunch := (LaunchX > 0.0) And (VoidR > GndInner) And (VoidR < CopperHalfS)
                 And (VoidR > SlotHalf) And (LandHalf < LaunchX);

    { The step is only drawn where it is a step. An opening narrower
      than the CPW gap does not reach the plane at all; one as wide as
      the plane would cut it in two, which a single contour cannot
      express; and one reaching past where the launch bites in would run
      back on itself, which is worse than either -- a self-intersecting
      contour has no defined shape at all. All three are refused up
      front by constraints.check_layout; this is the guard that stops a
      slip there from becoming a board nobody can account for. }
    HasCavity := (CavHalfL > 0.0) And (CavHalfS > GndInner)
                 And (CavHalfS < CopperHalfS) And (CavHalfL < CopperHalfL);
    If HasLaunch And (CavHalfL >= LaunchX - LandHalf) Then
        HasCavity := False;

    C.AddPoint(MMsToCoord(-CopperHalfL), MMsToCoord(Side * GndInner));

    If HasLaunch Then
    Begin
        { Where the void meets the gap edge, and where it meets the
          slot. Both ends of the arc have y > 0, so the angles need only
          ArcTan; ArcTan2 is not relied on. }
        XOut  := Sqrt(VoidR * VoidR - GndInner * GndInner);
        XSlot := Sqrt(VoidR * VoidR - SlotHalf * SlotHalf);
        A1 := PiVal / 2.0 - ArcTan(-XOut / GndInner);
        A2 := PiVal / 2.0 - ArcTan(XSlot / SlotHalf);

        { the launch nearer -x: along the gap, over the void, out
          through the slot. The arc runs from A1 down to A2, which
          carries it over the top of the void. }
        For i := 0 To VoidSteps Do
        Begin
            Ang := A1 + (A2 - A1) * i / VoidSteps;
            C.AddPoint(MMsToCoord(-LaunchX + VoidR * Cos(Ang)),
                       MMsToCoord(Side * VoidR * Sin(Ang)));
        End;
        C.AddPoint(MMsToCoord(-LaunchX + LandHalf), MMsToCoord(Side * SlotHalf));
        C.AddPoint(MMsToCoord(-LaunchX + LandHalf), MMsToCoord(Side * GndInner));
    End;

    { the die's opening, out and back along the inner edge }
    If HasCavity Then
    Begin
        C.AddPoint(MMsToCoord(-CavHalfL), MMsToCoord(Side * GndInner));
        C.AddPoint(MMsToCoord(-CavHalfL), MMsToCoord(Side * CavHalfS));
        C.AddPoint(MMsToCoord( CavHalfL), MMsToCoord(Side * CavHalfS));
        C.AddPoint(MMsToCoord( CavHalfL), MMsToCoord(Side * GndInner));
    End;

    If HasLaunch Then
    Begin
        { and the launch nearer +x, the same shape mirrored }
        C.AddPoint(MMsToCoord(LaunchX - LandHalf), MMsToCoord(Side * GndInner));
        C.AddPoint(MMsToCoord(LaunchX - LandHalf), MMsToCoord(Side * SlotHalf));
        For i := 0 To VoidSteps Do
        Begin
            Ang := (PiVal - A2) + (A2 - A1) * i / VoidSteps;
            C.AddPoint(MMsToCoord(LaunchX + VoidR * Cos(Ang)),
                       MMsToCoord(Side * VoidR * Sin(Ang)));
        End;
    End;

    C.AddPoint(MMsToCoord( CopperHalfL), MMsToCoord(Side * GndInner));
    AddPlaneCorner(C,  CopperHalfL, Side * CopperHalfS, False);
    AddPlaneCorner(C, -CopperHalfL, Side * CopperHalfS, True);

    Rgn.SetOutlineContour(C);
    Rgn.Layer := eTopLayer;
    Board.AddPCBObject(Rgn);
    Announce(Board, Rgn);
End;

{ The plane on the far face of the board.

  A single rectangle where the die's opening is blind -- the pocket
  takes metal off the TOP of the board and the floor underneath is still
  there -- and four rectangles around the opening where it goes right
  through, because there the far face is missing too.

  Four rectangles rather than one with a hole in it, for the same reason
  the coplanar planes bite the opening out of their own outline: a
  region's interior holes do not survive ExportCopper, which reads
  GetMainContour. Four simply-connected pieces do. They meet along their
  shared edges and Altium unites them into one plane, so this is a
  drafting split, not an electrical one. }
Procedure AddBackPlane(Board : IPCB_Board;
                       CopperHalfL, CopperHalfS, CavHalfL, CavHalfS : Double);
Var
    StripHalfL : Double;
Begin
    If CavHalfL <= 0.0 Then
    Begin
        AddRect(Board, -CopperHalfL, -CopperHalfS, CopperHalfL, CopperHalfS, eBottomLayer);
        Exit;
    End;

    { Each piece is drawn only if it has any area, so an opening that
      reaches an edge simply loses the pieces beyond it instead of
      producing a rectangle drawn back to front -- which AddRect would
      have built quite happily, as copper, over the hole. }
    If CavHalfL < CopperHalfL Then
    Begin
        AddRect(Board, -CopperHalfL, -CopperHalfS, -CavHalfL, CopperHalfS, eBottomLayer);
        AddRect(Board,  CavHalfL,    -CopperHalfS,  CopperHalfL, CopperHalfS, eBottomLayer);
    End;

    If CavHalfS < CopperHalfS Then
    Begin
        StripHalfL := CavHalfL;
        If StripHalfL > CopperHalfL Then StripHalfL := CopperHalfL;
        AddRect(Board, -StripHalfL, -CopperHalfS, StripHalfL, -CavHalfS,    eBottomLayer);
        AddRect(Board, -StripHalfL,  CavHalfS,    StripHalfL,  CopperHalfS, eBottomLayer);
    End;
End;

{ The die's opening in the board, as an Altium feature rather than a
  note for somebody to act on later.

  Two shapes, one decision, made in Python and arriving already made:

  * THROUGH -- a Board Cutout region on the keep-out layer. Altium
    treats it as a hole in the board shape, so the 3D view, the DRC and
    the fabrication outputs all agree that there is no board there.

  * BLIND -- a Cavity region on a mechanical layer, carrying the milled
    depth in CavityHeight. This is the mechanism Altium provides for a
    component that sits inside the board, and it is what a fabricator
    reads a depth-milled pocket off.

  Every call that could be missing from this build of the API is wrapped
  and reported separately, because the difference between "the region is
  there but is still plain copper" and "the region was never made" is
  invisible in the saved file and total in what it means. What is NOT
  wrapped is the identifiers: eRegionKind_BoardCutout, eRegionKind_Cavity
  and eKeepOutLayer are named directly, because an undeclared identifier
  in DelphiScript is a parse error -- a modal dialog, not an exception --
  and no Try around it would ever run. They were confirmed present in
  AD26 before being written here; see the header. }
Procedure AddCavity(Board : IPCB_Board; HalfL, HalfS, Depth : Double;
                    Through : Boolean);
Var
    Rgn : IPCB_Region;
    C   : IPCB_Contour;
Begin
    Rgn := PCBServer.PCBObjectFactory(eRegionObject, eNoDimension, eCreate_Default);
    C := PCBServer.PCBContourFactory;
    C.AddPoint(MMsToCoord(-HalfL), MMsToCoord(-HalfS));
    C.AddPoint(MMsToCoord( HalfL), MMsToCoord(-HalfS));
    C.AddPoint(MMsToCoord( HalfL), MMsToCoord( HalfS));
    C.AddPoint(MMsToCoord(-HalfL), MMsToCoord( HalfS));
    Rgn.SetOutlineContour(C);

    If Through Then
    Begin
        Rgn.Layer := eKeepOutLayer;
        Try
            Rgn.Kind := eRegionKind_BoardCutout;
            Say('cavity_kind_set=ok');
        Except
            Say('cavity_kind_set=FAIL');
        End;
    End
    Else
    Begin
        { Mechanical 1 is where a cavity has to live -- the Kind is only
          offered on a mechanical layer. Enabling and naming it is best
          effort: a cavity on a disabled layer is still in the file, it
          just is not drawn, and losing the whole feature over a
          cosmetic call would be the wrong trade.

          Written as chained member access with no intermediate
          variable, deliberately. A Var needs a type name, and a type
          name that this build does not know is a PARSE error -- the
          modal-dialog kind, which no Try around it would ever catch.
          Member access is resolved at run time and does land in the
          Except. LayerStack_V7 rather than LayerStack: spikes/s2_probe
          found the latter raises. }
        Rgn.Layer := eMechanical1;
        Try
            Board.LayerStack_V7.LayerObject[eMechanical1].MechanicalLayerEnabled := True;
            Say('cavity_layer=ok');
        Except
            Say('cavity_layer=not_enabled');
        End;
        Try
            Board.LayerStack_V7.LayerObject[eMechanical1].Name := 'Chip Cavity';
        Except
        End;
        Try
            Rgn.Kind := eRegionKind_Cavity;
            Say('cavity_kind_set=ok');
        Except
            Say('cavity_kind_set=FAIL');
        End;
        Try
            Rgn.CavityHeight := MMsToCoord(Depth);
            Say('cavity_height_set=ok');
        Except
            Say('cavity_height_set=FAIL');
        End;
    End;

    Board.AddPCBObject(Rgn);
    Announce(Board, Rgn);

    { Read back what the region actually is, not what it was told to be.
      A Kind that did not take leaves a plain copper region sitting on
      the keep-out layer, which looks like a cutout in the tree and is
      not one. }
    Try
        If Rgn.Kind = eRegionKind_BoardCutout Then
            Say('cavity_kind_readback=board_cutout')
        Else If Rgn.Kind = eRegionKind_Cavity Then
            Say('cavity_kind_readback=cavity')
        Else
            Say('cavity_kind_readback=WRONG');
    Except
        Say('cavity_kind_readback=unreadable');
    End;
End;

{ The copper that stands round one screw hole, on one layer.

  The hole is a void in the ground plane, and a void inside a region is
  not something this script can draw: a region has one contour, and
  ExportCopper reads only that. So it is built the way the die's opening
  and the connector's anti-pad are -- by never drawing the metal that
  would be in it. The plane gives up a square round the screw, out to the
  board's corner (AddPlaneCorner); this puts the square back as four
  pieces, one per quadrant about the screw centre, each with the quarter
  circle of the keep-out bitten out of the corner at the centre. Each
  piece is a single contour, the four meet along the lines through the
  centre, and the plane and the pieces are united on export, so the
  copper is whole and the keep-out is empty.

  (CX, CY) is the screw centre, R the keep-out's radius, S the square's
  half-size on the side facing the board's middle, and EA, EB how far
  the square runs out toward the edge in x and y. SX, SY (+-1) say which
  corner of the board it is. }
Procedure AddScrewBlock(Board : IPCB_Board; Lay : TLayer;
                        CX, CY, SX, SY, R, S, EA, EB : Double);
Var
    Rgn : IPCB_Region;
    C   : IPCB_Contour;
    QA, QB, LA, LB, Phi, A, B : Double;
    Q, K : Integer;
Begin
    For Q := 0 To 3 Do
    Begin
        If (Q = 0) Or (Q = 2) Then QA := -1.0 Else QA := 1.0;
        If Q < 2 Then QB := -1.0 Else QB := 1.0;
        If QA < 0.0 Then LA := S Else LA := EA;
        If QB < 0.0 Then LB := S Else LB := EB;

        Rgn := PCBServer.PCBObjectFactory(eRegionObject, eNoDimension, eCreate_Default);
        C := PCBServer.PCBContourFactory;

        { the arc, from the point on the b axis round to the point on
          the a axis }
        For K := 0 To 8 Do
        Begin
            Phi := (PiVal / 2.0) * K / 8.0;
            A := QA * R * Sin(Phi);
            B := QB * R * Cos(Phi);
            C.AddPoint(MMsToCoord(CX + SX * A), MMsToCoord(CY + SY * B));
        End;
        C.AddPoint(MMsToCoord(CX + SX * QA * LA), MMsToCoord(CY));
        C.AddPoint(MMsToCoord(CX + SX * QA * LA), MMsToCoord(CY + SY * QB * LB));
        C.AddPoint(MMsToCoord(CX),                MMsToCoord(CY + SY * QB * LB));

        Rgn.SetOutlineContour(C);
        Rgn.Layer := Lay;
        Board.AddPCBObject(Rgn);
        Announce(Board, Rgn);
    End;
End;

{ All four screws, on both copper layers. }
Procedure AddScrewBlocks(Board : IPCB_Board);
Var
    SX, SY : Double;
    I, J   : Integer;
Begin
    For I := 0 To 1 Do
        For J := 0 To 1 Do
        Begin
            If I = 0 Then SX := -1.0 Else SX := 1.0;
            If J = 0 Then SY := -1.0 Else SY := 1.0;
            AddScrewBlock(Board, eTopLayer, SX * ScrewU, SY * ScrewV, SX, SY,
                          ScrewKeepR, ScrewBlockS, PkgHalfL - ScrewU, PkgHalfS - ScrewV);
            AddScrewBlock(Board, eBottomLayer, SX * ScrewU, SY * ScrewV, SX, SY,
                          ScrewKeepR, ScrewBlockS, PkgHalfL - ScrewU, PkgHalfS - ScrewV);
        End;
End;

{ One corner screw's clearance hole: a round Board Cutout, drawn the same
  way the die's through-opening is. It is the hole the router leaves in
  the board, so the fabrication outputs and the 3D view agree there is
  nothing there. It is a polygon, thirty-two sided -- a Region has no arc
  -- which is round enough for a drilled hole and stays inside the
  nominal diameter at its flats.

  The copper keep-out round it is NOT drawn here. The ground planes are
  single-contour regions (see AddGroundPlane for why they cannot carry an
  interior hole), so the wider void is cut by the HFSS builder from the
  same screw_keepout_diameter, and ExportCopper ignores this region
  because it is not on a copper layer. }
Procedure AddScrewHole(Board : IPCB_Board; X, Y, Dia : Double);
Var
    Rgn : IPCB_Region;
    C   : IPCB_Contour;
    i   : Integer;
    Ang : Double;
Begin
    Rgn := PCBServer.PCBObjectFactory(eRegionObject, eNoDimension, eCreate_Default);
    C := PCBServer.PCBContourFactory;
    For i := 0 To 31 Do
    Begin
        Ang := 2.0 * PiVal * i / 32.0;
        C.AddPoint(MMsToCoord(X + Dia / 2.0 * Cos(Ang)),
                   MMsToCoord(Y + Dia / 2.0 * Sin(Ang)));
    End;
    Rgn.SetOutlineContour(C);
    Rgn.Layer := eKeepOutLayer;
    Rgn.Kind := eRegionKind_BoardCutout;
    Board.AddPCBObject(Rgn);
    Announce(Board, Rgn);
End;

{ The drill that goes with a screw clearance hole: a NON-PLATED pad, copper
  no bigger than its hole, so the NC drill output lists the hole (plated
  and non-plated come out as separate files) and a fabricator drills it
  rather than having to read the cutout. The cutout above stays: it is
  what the 3D view and the board outline use.

  On the multilayer layer, so ExportCopper -- which keeps top and bottom
  copper only -- never sees it and the copper the field solve is built
  from is unchanged. }
Procedure AddMountHole(Board : IPCB_Board; X, Y, Dia : Double);
Var
    Pd : IPCB_Pad;
Begin
    Pd := PCBServer.PCBObjectFactory(ePadObject, eNoDimension, eCreate_Default);
    Pd.X := MMsToCoord(X);
    Pd.Y := MMsToCoord(Y);
    Pd.Layer := eMultiLayer;
    Pd.TopShape := eRounded;
    Pd.TopXSize := MMsToCoord(Dia);
    Pd.TopYSize := MMsToCoord(Dia);
    Pd.HoleSize := MMsToCoord(Dia);
    Pd.Plated := False;
    Board.AddPCBObject(Pd);
    Announce(Board, Pd);
End;

{ A rectangle of thin tracks on Mechanical 2, which the Gerber output
  plots as the board outline. A Gerber set has no board shape of its own:
  the outline has to be drawn on a layer, and this is that layer. }
Procedure AddOutlineBox(Board : IPCB_Board; HalfL, HalfS : Double);
Begin
    AddTrack(Board, -HalfL, -HalfS,  HalfL, -HalfS, 0.1, eMechanical2);
    AddTrack(Board,  HalfL, -HalfS,  HalfL,  HalfS, 0.1, eMechanical2);
    AddTrack(Board,  HalfL,  HalfS, -HalfL,  HalfS, 0.1, eMechanical2);
    AddTrack(Board, -HalfL,  HalfS, -HalfL, -HalfS, 0.1, eMechanical2);
End;

Procedure AddVia(Board : IPCB_Board; X, Y, Dia, Hole : Double);
Var
    V : IPCB_Via;
Begin
    V := PCBServer.PCBObjectFactory(eViaObject, eNoDimension, eCreate_Default);
    V.X := MMsToCoord(X);
    V.Y := MMsToCoord(Y);
    V.Size := MMsToCoord(Dia);
    V.HoleSize := MMsToCoord(Hole);
    V.LowLayer := eTopLayer;
    V.HighLayer := eBottomLayer;
    Board.AddPCBObject(V);
    Announce(Board, V);
End;

{ Whether a via of diameter Dia at (X, Y) would land closer than MinGap
  to a track, pad, via or component-owned region already on the board
  -- checked against the BOARD ITSELF rather than a list this script
  keeps, so a bond finger, a connector pad or an earlier stitching/
  shielding via all count the moment they are really there. Nothing
  here can drift out of step with what actually got placed, which a
  hand-derived clearance can.

  A region belonging to a placed component is an obstacle; a bare
  board-level one is not. The connector's library footprint brings its
  own ground connection in as a region -- real copper, several bars of
  it, not a simple pad -- and "regions are not an obstacle" is right
  for AddGroundPlane's and AddRect's regions (that IS the plane a via
  is meant to land in) and wrong for the connector's own, which is
  copper belonging to the PART rather than the board's own plane. The
  first version of this check treated the connector as a whole
  Comp.BoundingRectangle instead, which is not this: a component's
  bounding rectangle is one rectangle over every child it has, so it
  also claimed the empty space between, say, its ground land and its
  outermost pads -- which is most of a connector's footprint by area --
  and a shielding fence run out to meet one found almost nothing free
  near it. InComponent on the region itself is the tighter question:
  only the shape that is actually there is the obstacle.

  Tracks and pads are only obstacles on eTopLayer or eBottomLayer --
  the only two copper layers a two-layer board has. Placing the
  connector copies its library component's silkscreen, courtyard and
  assembly-drawing outlines in along with its pads (measured at 56
  primitives for 5 real pads, in the report this comment was written
  against), and those outlines are tracks too. Checked without this,
  a shielding fence run wide enough to sweep past a connector found
  every candidate near it obstructed and reported zero vias with
  nothing on the board to show why -- the outline was real, but it was
  never copper. A via itself has no such ambiguity: this script only
  ever places one spanning eTopLayer to eBottomLayer (see AddVia), so
  every via already on the board is a real obstacle regardless of
  layer, and needs no check here.

  A bare board-level region -- every plane, fill and cutout this file
  draws itself -- is still not checked at all: that copper is exactly
  where a stitching or shielding via is meant to land, not an obstacle.
  What keeps a via out of the die's opening, the CPW's own gap, a
  connector's void or off the board edge is the caller's own bounds --
  see TryVia.

  The test is a bounding-box overlap, not exact outline geometry: it
  can refuse a via that would technically have squeezed past an
  irregular shape's corner, never the reverse. For a via, erring
  toward more clearance than strictly required is the safe direction. }
Function ViaObstructed(Board : IPCB_Board; X, Y, Dia, MinGap : Double) : Boolean;
Var
    It       : IPCB_SpatialIterator;
    Prim     : IPCB_Primitive;
    Cx, Cy, Half : TCoord;
    Relevant : Boolean;
Begin
    Result := False;
    Cx   := MMsToCoord(X);
    Cy   := MMsToCoord(Y);
    Half := MMsToCoord(Dia / 2.0 + MinGap - GapTolerance);

    { A SPATIAL iterator, asked only for what lies within reach of this
      one candidate, rather than a board iterator walking every object
      on the board. The fill calls this once per grid point and the
      board gains a via at most of them, so walking the whole board
      each time costs candidates x objects -- both of which grow with
      the board's area. On the default 26 x 10 mm board that was about
      half a minute; on a 70 x 30 mm one it ran for well over ten
      minutes without finishing, long past the point the pipeline gave
      up on Altium and moved on. The answer is the same either way: the
      bounding-box test below still decides, and the area handed to the
      iterator is exactly the box that test uses, so nothing that could
      pass it is left out.

      AllObjects, filtered by ObjectId inside the loop, rather than a
      narrower AddFilter_ObjectSet built from just these three kinds:
      AllObjects and AllLayers are the two filter constants this file
      has already confirmed exist (ReportCounts uses the same pair). A
      set built from named object-id constants would very likely work
      too, but it is untested here, and an untested identifier in this
      API is a parse error -- the modal-dialog kind, not a catchable
      one -- so it is not worth the saving. }
    It := Board.SpatialIterator_Create;
    It.AddFilter_ObjectSet(AllObjects);
    It.AddFilter_LayerSet(AllLayers);
    It.AddFilter_Area(Cx - Half, Cy - Half, Cx + Half, Cy + Half);
    Prim := It.FirstPCBObject;
    While (Prim <> Nil) And (Not Result) Do
    Begin
        Relevant := False;
        If Prim.ObjectId = eViaObject Then
            Relevant := True
        Else If Prim.ObjectId = eRegionObject Then
        Begin
            { False by default here, the opposite default from the
              pad/track case below: board-level regions outnumber
              component-owned ones on this board by roughly nine to
              one (every AddGroundPlane, AddRect and AddCavity call
              against, at most, the handful the connector brings in),
              so if InComponent cannot be read at all the safer
              default is to keep treating regions as not-an-obstacle,
              same as before this check existed, rather than block
              stitching out of every plane on the board over the one
              region that needed the opposite answer. }
            Relevant := False;
            Try
                Relevant := Prim.InComponent;
            Except
            End;
        End
        Else If (Prim.ObjectId = eTrackObject) Or (Prim.ObjectId = ePadObject) Then
        Begin
            { eMultiLayer too, not just Top and Bottom: a through-hole
              pad reports its Layer as eMultiLayer, not either copper
              layer by name, and this same vendor's library ships a
              through-hole variant of this connector family alongside
              the SMD one this board places today (see README.md on
              60124002122502) -- the SMP part this generator places is
              a provisional stand-in, expected to change. Missing this
              is exactly the trap MeasureFootprint.pas's IsCopper
              already guards against for the same reason: a through-
              hole pad excluded here would stop being an obstacle at
              all, not fail loudly.

              Read defensively: Layer is read straight off
              IPCB_Primitive with no cast, which every other read of it
              in this file does only after casting to the concrete type
              first (see ExportCopper.pas). If that turns out to matter
              and this raises, the Except leaves Relevant at its safe
              default -- treated as an obstacle, which is what every
              candidate got before this check existed at all. }
            Relevant := True;
            Try
                Relevant := (Prim.Layer = eTopLayer) Or (Prim.Layer = eBottomLayer)
                            Or (Prim.Layer = eMultiLayer);
            Except
            End;
        End;

        If Relevant Then
        Begin
            If (Cx + Half >= Prim.BoundingRectangle.Left)   And
               (Cx - Half <= Prim.BoundingRectangle.Right)  And
               (Cy + Half >= Prim.BoundingRectangle.Bottom) And
               (Cy - Half <= Prim.BoundingRectangle.Top) Then
                Result := True;
        End;
        Prim := It.NextPCBObject;
    End;
    Board.SpatialIterator_Destroy(It);
End;

{ Whether a via of diameter Dia centred at Pos along one axis keeps
  MinGap from the board's edge on that axis, Half being the board's
  half-extent along it. TryVia refuses a site that fails this, and
  AddViaLattice ends its rows and columns at the first that does, so the
  two cannot disagree about where the board stops --
  constraints.check_layout asks the same question of the fence, in
  Python. }
Function ClearOfEdge(Pos, Dia, MinGap, Half : Double) : Boolean;
Begin
    Result := Abs(Pos) + Dia / 2.0 + MinGap - GapTolerance <= Half;
End;

{ Places a via at (X, Y) unless it would land closer than MinGap to the
  board's edge, in the die's opening, in the CPW's own gap, in a
  connector's anti-pad void, or too close to something already there --
  this is the one place that decides whether a candidate site is safe,
  so AddViaLattice routes every via through it rather than trusting its
  own loops to stay clear.

  Every one of those distances is allowed to fall GapTolerance short,
  for the reason GapTolerance gives. A lattice anchored at the board's
  centre lands on round numbers, and a site whose via sits exactly
  MinGap from the die's opening is as likely as not to come out a hair
  inside it in floating point. Refused at random, that would leave
  holes in the pattern that were never meant to be there -- and
  holes that are not the same on both sides of the board.

  The first three keepouts (board edge, die opening, CPW gap) are
  places no copper of ANY kind ever exists on this board, by
  construction of BuildBoard and AddGroundPlane -- ViaObstructed cannot
  see them, because there is no primitive there to be obstructed by. A
  region merely stops short; nothing marks the empty strip itself. The
  fourth, the connector's void, is the same kind of absence: it is a
  bite taken out of the ground plane's own outline (see
  AddGroundPlane), not a separate object, so it is invisible to
  ViaObstructed too. All four have to be told to TryVia directly
  because none of them can be discovered by looking at the board.

  Corridor is GndInner (half the CPW's trace-plus-gap) with the
  fence's own standoff (via_gnd_clearance) already folded in by the
  caller, so a via sitting exactly on the fence's own Y offset clears
  this check with nothing to spare -- see FenceY's construction in
  GenerateFromParams. LaunchX <= 0 disables the void check: the die has
  no connector, and nothing calls this with a real void needing one.

  Corridor only applies out to CorridorHalfL, which the caller passes
  as the signal's own real extent (TraceHalfL): the CPW's gap does not
  exist beyond where the trace itself stops, and past each connector
  the ground plane runs solid across the centreline (see BuildBoard's
  FillHalfL). Without this bound, a whole-board sweep would refuse
  every candidate along Y=0 for the board's full length, including the
  solid ground strip behind each launch -- exactly the copper a
  ground-plane fill exists to stitch.

  MinGap is enforced twice over: once here against the die's opening
  and the connector's void, and once inside ViaObstructed against every
  pad, track and via already on the board -- including ones this run
  placed a moment ago, so two accepted vias are never closer than
  MinGap even where the nominal spacing alone would have allowed it (a
  pattern's own corner, or the via right after one skipped for an
  obstacle). }
Function TryVia(Board : IPCB_Board; X, Y, Dia, MinGap,
                HalfL, HalfS, CavHalfL, CavHalfS,
                Corridor, CorridorHalfL, LaunchX, VoidR : Double) : Boolean;
Var
    Reach : Double;
Begin
    Result := False;
    If (Not ClearOfEdge(X, Dia, MinGap, HalfL)) Or
       (Not ClearOfEdge(Y, Dia, MinGap, HalfS)) Then Exit;
    If ((CavHalfL > 0.0) Or (CavHalfS > 0.0))
       And (Abs(X) < CavHalfL + Dia / 2.0 + MinGap - GapTolerance)
       And (Abs(Y) < CavHalfS + Dia / 2.0 + MinGap - GapTolerance) Then Exit;
    If (Abs(X) < CorridorHalfL)
       And (Abs(Y) < Corridor + Dia / 2.0 - GapTolerance) Then Exit;
    If LaunchX > 0.0 Then
    Begin
        Reach := VoidR + Dia / 2.0 + MinGap - GapTolerance;
        If ((X - LaunchX) * (X - LaunchX) + Y * Y < Reach * Reach) Or
           ((X + LaunchX) * (X + LaunchX) + Y * Y < Reach * Reach) Then Exit;
    End;
    If ScrewKeepR > 0.0 Then
    Begin
        { Edge to edge: the via's own radius and MinGap on top of the
          keep-out circle, the same way every other obstacle is treated. }
        Reach := ScrewKeepR + Dia / 2.0 + MinGap - GapTolerance;
        If ((Abs(X) - ScrewU) * (Abs(X) - ScrewU) + (Abs(Y) - ScrewV) * (Abs(Y) - ScrewV)
            < Reach * Reach) Then Exit;
    End;
    If ViaObstructed(Board, X, Y, Dia, MinGap) Then Exit;
    AddVia(Board, X, Y, Dia, Dia / 2.0);
    Result := True;
End;

{ Counts one TryVia outcome into whichever total it belongs to. Only
  exists to keep AddViaLattice from repeating the same four-line
  If/Else at every candidate site -- it has two. }
Procedure Tally(Ok : Boolean; Var Placed, Skipped : Integer);
Begin
    If Ok Then
        Placed := Placed + 1
    Else
        Skipped := Skipped + 1;
End;

Procedure AddPad(Board : IPCB_Board; X, Y, W, H : Double; Lay : TLayer);
Var
    Pd : IPCB_Pad;
Begin
    Pd := PCBServer.PCBObjectFactory(ePadObject, eNoDimension, eCreate_Default);
    Pd.X := MMsToCoord(X);
    Pd.Y := MMsToCoord(Y);
    Pd.TopXSize := MMsToCoord(W);
    Pd.TopYSize := MMsToCoord(H);
    Pd.TopShape := eRectangular;
    Pd.HoleSize := 0;
    Pd.Layer := Lay;
    Board.AddPCBObject(Pd);
    Announce(Board, Pd);
End;

{ The vendor's own footprint, out of the vendor's own library.

  Wuerth WR-SMP 60124102122403, placed as a real component rather than
  drawn as four pads standing where the lands are. That distinction is
  the difference between a board that describes the fields correctly and
  a board somebody can have made: a component brings its designator, its
  paste and mask layers, its silkscreen and courtyard, its 3D body, and
  a link back to the library for DRC and the bill of materials. Loose
  pads bring none of it, and no output job can invent them.

  The library is the PcbLib extracted from the shipped IntLib -- an
  IntLib is compiled and cannot be opened as a library. Its path and the
  footprint name arrive through the parameter file, so this script never
  has to know which vendor part it is placing.

  Rotation. The library draws the part with its centre pin exiting
  toward -y; the board wants it exiting INBOARD at each end. For the
  launch at -x that is +90 degrees, and at +x it is 270. Getting this
  wrong does not look wrong -- the footprint is symmetric across the
  signal axis, so a half-turn error leaves a launch that looks perfect
  and faces the board edge.

  Returns the number of primitives copied in, which is the only way to
  tell a placed footprint from an empty component that reports success. }
Function OpenSmpLibrary(LibPath, Pattern : String) : Boolean;
Var
    LibComp : IPCB_LibComponent;
    nSeen   : Integer;
Begin
    Result := False;
    SmpDoc := Nil; SmpLib := Nil; SmpIter := Nil; SmpComp := Nil;

    If (LibPath = '') Or (Pattern = '') Then
    Begin
        Say('smp_library=NOT_CONFIGURED');
        Exit;
    End;

    SmpDoc := Client.OpenDocument('PCBLIB', LibPath);
    If SmpDoc = Nil Then
    Begin
        Say('smp_library=COULD_NOT_OPEN');
        Exit;
    End;
    Client.ShowDocument(SmpDoc);

    Try
        SmpLib := PCBServer.GetCurrentPCBLibrary;
    Except
    End;
    If SmpLib = Nil Then
    Begin
        Say('smp_library=NO_LIBRARY');
        Exit;
    End;

    Try
        SmpIter := SmpLib.LibraryIterator_Create;
    Except
    End;
    If SmpIter = Nil Then
    Begin
        Say('smp_library=NO_ITERATOR');
        Exit;
    End;
    Try
        SmpIter.SetState_FilterAll;
    Except
    End;

    nSeen := 0;
    LibComp := SmpIter.FirstPCBObject;
    While LibComp <> Nil Do
    Begin
        nSeen := nSeen + 1;
        If LibComp.Name = Pattern Then SmpComp := LibComp;
        LibComp := SmpIter.NextPCBObject;
    End;
    Say('smp_library_footprints=' + IntToStr(nSeen));

    If SmpComp = Nil Then
    Begin
        Say('smp_footprint=NOT_IN_LIBRARY');
        Exit;
    End;
    Say('smp_footprint=' + Pattern);
    Result := True;
End;

Procedure CloseSmpLibrary;
Begin
    If SmpIter <> Nil Then
    Begin
        Try
            SmpLib.LibraryIterator_Destroy(SmpIter);
        Except
        End;
        SmpIter := Nil;
    End;
    If SmpDoc <> Nil Then
    Begin
        Try
            Client.CloseDocument(SmpDoc);
        Except
        End;
        SmpDoc := Nil;
    End;
    SmpLib := Nil;
    SmpComp := Nil;
End;

{ One launch: the vendor's own footprint, placed as a real component.

  Placed rather than drawn as four pads standing where the lands are,
  and that distinction is the difference between a board that describes
  the fields correctly and a board somebody can have made. A component
  brings its designator, its paste and mask layers, its silkscreen and
  courtyard, its 3D body and a link back to the library, so DRC, the
  bill of materials, the assembly drawing and the 3D export all have
  something to work from. Loose pads bring none of it, and no output job
  can invent them.

  Nothing here knows which vendor part it is placing: the library and
  the footprint name arrive through the parameter file, and the board
  around the launch is drawn from the envelope and anti-pad numbers that
  come with them. Swapping to another manufacturer's SMP is meant to be
  a new library and a new name, not an edit here.

  Rotation. The library draws the part with its centre pin exiting
  toward -y; the board wants it exiting INBOARD at each end -- +90
  degrees at the -x end, 270 at the +x end. Getting this wrong does not
  look wrong: the footprint is symmetric across the signal axis, so a
  half-turn error leaves a launch that looks perfect and faces the board
  edge. That is why the caller checks where the signal pad landed rather
  than trusting the angle.

  BoardDoc is taken rather than looked up. The board has no file name
  yet -- it is saved at the end of the run -- so there is nothing to
  look it up BY, and a primitive created while the library is the front
  document is a primitive added to the library.

  Returns how many primitives were copied in, which is the only way to
  tell a placed footprint from an empty component reporting success. }
Function AddSmpLaunch(Board : IPCB_Board; BoardDoc : IServerDocument;
                      Designator : String; Sign, Origin : Double) : Integer;
Var
    GIter   : IPCB_GroupIterator;
    Comp    : IPCB_Component;
    Prim    : IPCB_Primitive;
    NewPrim : IPCB_Primitive;
    dx, dy  : TCoord;
Begin
    Result := 0;
    If SmpComp = Nil Then Exit;

    Client.ShowDocument(BoardDoc);

    Comp := PCBServer.PCBObjectFactory(eComponentObject, eNoDimension, eCreate_Default);
    Comp.Layer := eTopLayer;
    Comp.X := MMsToCoord(Sign * Origin);
    Comp.Y := MMsToCoord(0.0);
    Try
        Comp.Pattern := SmpComp.Name;
        Comp.Name.Text := Designator;
        Comp.Comment.Text := SmpComp.Name;
        Comp.NameOn := True;
        Comp.CommentOn := False;
    Except
        Say('smp_component_fields=partial');
    End;
    Board.AddPCBObject(Comp);
    Announce(Board, Comp);

    { Copy the library's primitives in, shifted from the library origin
      to where the component sits. The shift is applied per primitive
      rather than left to the component: a replicated primitive carries
      its LIBRARY coordinates, and adding it to a component does not
      move it. }
    dx := Comp.X - SmpComp.X;
    dy := Comp.Y - SmpComp.Y;

    GIter := Nil;
    Try
        GIter := SmpComp.GroupIterator_Create;
    Except
    End;
    If GIter <> Nil Then
    Begin
        Prim := GIter.FirstPCBObject;
        While Prim <> Nil Do
        Begin
            NewPrim := Nil;
            Try
                NewPrim := Prim.Replicate;
            Except
            End;
            If NewPrim <> Nil Then
            Begin
                Try
                    NewPrim.MoveByXY(dx, dy);
                    { BOTH. AddPCBObject on the component puts the
                      primitive in the component's group; on the board
                      it registers it with the board itself. With only
                      the first, the footprint is placed, looks right,
                      and is invisible to every board iterator -- which
                      means it never reaches copper.txt and so never
                      reaches the field solve. The in-memory pad count
                      is what caught it: 54, when 54 bond pads plus two
                      five-pad launches is 64. }
                    Comp.AddPCBObject(NewPrim);
                    Board.AddPCBObject(NewPrim);
                    Announce(Board, NewPrim);
                    Result := Result + 1;
                Except
                End;
            End;
            Prim := GIter.NextPCBObject;
        End;
        Try
            SmpComp.GroupIterator_Destroy(GIter);
        Except
        End;
    End;

    { Rotated only once its children are in, so they turn with it. }
    Try
        If Sign < 0.0 Then
            Comp.Rotation := 90.0
        Else
            Comp.Rotation := 270.0;
    Except
        Say('smp_rotation=FAIL');
    End;
End;

{ Bond pads, placed from the shared layout file.

  The positions are computed in Python (eigen_mode_analyze/bonds.py) and
  written to bonds.txt, because HFSS builds the wires from the same
  file. Working them out separately in both places would let the pads
  and the wires drift apart the first time a parameter changed.

  There is no bond count: the number follows from the die perimeter and
  the pad pitch, the way it does on the bench.

  OnChip selects which end of each bond to place -- the die's pad or the
  board's finger. Both come from the same record, so they cannot end up
  describing different bonds.

  The wires themselves are NOT drawn here. AD26 has wirebond rules,
  violation types and JEDEC profiles but no wirebond entry in its
  scriptable object-id enum, so PCBObjectFactory cannot make one.

  Signal pads are capped to fit inside the trace they sit on, rather
  than using the same width as a ground pad. bonds.py puts two signal
  wires side by side (SIGNAL_WIRES = 2, to roughly halve the series
  inductance a single wire would add), each centred a quarter-trace-
  width off axis -- at the general ground pad size those two pads
  together span 0.25 mm across a 0.20 mm trace, sticking out 0.025 mm on
  each side. A pad wider than the line it lands on locally widens the
  conductor, which is a real, if small, characteristic-impedance
  discontinuity right at the bond -- not just a drafting nicety. Sizing
  each signal pad to Trace / 2 makes the two exactly tile the trace
  width and touch at the centre line, neither exceeding it. }
Function AddBondPads(Board : IPCB_Board; BondPath : String;
                     PadW, PadH, Trace : Double; OnChip : Boolean) : Integer;
Var
    Lines : TStringList;
    n     : Integer;
    Line  : String;
    Kind  : String;
    X, Y  : Double;
    UseH  : Double;
    Ok    : Boolean;
Begin
    Result := 0;
    Lines := TStringList.Create;

    Ok := True;
    Try
        Lines.LoadFromFile(BondPath);
    Except
        Ok := False;
    End;

    If Ok Then
    Begin
        For n := 0 To Lines.Count - 1 Do
        Begin
            Line := Trim(Lines[n]);
            If (Line <> '') And (Copy(Line, 1, 1) <> '#') Then
            Begin
                Kind := Field(Line, 1);
                If OnChip Then
                Begin
                    X := StrToFloat(Field(Line, 4));
                    Y := StrToFloat(Field(Line, 5));
                End
                Else
                Begin
                    X := StrToFloat(Field(Line, 2));
                    Y := StrToFloat(Field(Line, 3));
                End;
                If Kind = 'signal' Then
                    UseH := Trace / 2.0
                Else
                    UseH := PadH;
                AddPad(Board, X, Y, PadW, UseH, eTopLayer);
                Result := Result + 1;
            End;
        End;
    End;

    Lines.Free;
End;

Procedure SetOutline(Board : IPCB_Board; HalfL, HalfS : Double);
Var
    i   : Integer;
    Seg : TPolySegment;
Begin
    Board.BoardOutline.BeginModify;
    Board.BoardOutline.PointCount := 4;
    For i := 0 To 3 Do
    Begin
        { Segments[i] returns the record BY VALUE: editing it in place
          is silently discarded, and the board stays at its default
          6 x 4 inches while reporting success. }
        Seg := Board.BoardOutline.Segments[i];
        Seg.Kind := ePolySegmentLine;
        Case i Of
            0 : Begin Seg.vx := MMsToCoord(-HalfL); Seg.vy := MMsToCoord(-HalfS); End;
            1 : Begin Seg.vx := MMsToCoord(HalfL);  Seg.vy := MMsToCoord(-HalfS); End;
            2 : Begin Seg.vx := MMsToCoord(HalfL);  Seg.vy := MMsToCoord(HalfS);  End;
            3 : Begin Seg.vx := MMsToCoord(-HalfL); Seg.vy := MMsToCoord(HalfS);  End;
        End;
        Board.BoardOutline.Segments[i] := Seg;
    End;
    Board.BoardOutline.EndModify;
    Board.BoardOutline.Rebuild;
End;

{ Every via on the board -- the fence down both sides of the CPW and the
  fill across the ground planes -- laid out as ONE lattice, from the
  route outward.

  The lattice is a set of rows running along the feedline. Row 0 is the
  fence, FenceY either side of the centre line: right beside the CPW's
  gap, where a via matters most. Row K is K steps further out on each
  side. So the pattern is anchored to the route and runs out to the
  board's edge, which is where whatever does not fit is least
  consequential.

  It is anchored at the board's centre along each row too, and
  alternate rows start half a step over -- the "Stagger alternate rows"
  option Altium's own Add Stitching to Net dialog offers. That makes it
  mirror-symmetric left to right and top to bottom, the way the board
  is: each site is worked out for one quadrant and tried in all four.

  The first version stepped a grid across the board from its corner
  instead, with the fence on a grid of its own. Nothing tied the two
  together, and nothing tied either to the route: the first fill row
  came out 1.2 mm from the fence on one side of the line and 0.6 mm on
  the other, and the rows stood a different distance from one end of the
  board than from the other.

  That is more than a matter of looks: the board is symmetric, and a
  structure that is can only pass the line's symmetric mode on to
  symmetric modes between the planes, which a lopsided one does not
  promise. (A reason to expect a cleaner S21, not a measured one.)

  MaxSpacing is the step along a row and between rows, so the practical
  ceiling on how far apart any two neighbouring vias end up in open
  copper -- the guideline for suppressing a given frequency is a spacing
  no more than a fraction of its wavelength, which is what this is for.
  MinGap is the floor TryVia enforces between any two vias and around
  everything else. A site TryVia refuses is simply left out, so the
  lattice has a hole there and nowhere else: next to a bond finger, the
  connector or the die's opening, the pattern is the same one with the
  pieces that do not fit taken out, not a different one shifted to
  dodge them.

  What counts as the fence is row 0 between the two connectors' signal
  lands, the span TraceHalfL bounds; the rest is the fill. They are
  counted apart because the report has always counted them apart, and
  because a fence that comes out short is a different problem from a
  fill that does.

  One row is not a row of the lattice: the centre line itself, behind
  each launch. Between the signal lands the CPW's gap keeps vias off it,
  but behind them the ground runs solid across it, and with the rows
  starting FenceY either side the vias nearest the centre line there
  would stand 2 * FenceY apart -- further than MaxSpacing, in the one
  stretch of ground with nothing across its middle. So it gets a row of
  its own, staggered against the fence's, placed last: a lattice site is
  never the one refused to make room for it.

  Skipped is a count, not a flag: most of the board is open copper, so
  most sites are placed; a run where skips dominate placements is worth
  a human noticing in the report, which is what it is there for. }
Procedure AddViaLattice(Board : IPCB_Board;
                        FenceY, TraceHalfL, ViaDia, MaxSpacing, MinGap,
                        HalfL, HalfS, CavHalfL, CavHalfS,
                        Corridor, LaunchX, VoidR : Double;
                        Var Shielding, ShieldingSkipped,
                            Stitching, StitchingSkipped : Integer);
Var
    K, SX, SY : Integer;
    RowY, RowShift, ColX, X, Y : Double;
    Stagger, OnFence, Ok : Boolean;
Begin
    Shielding := 0;
    ShieldingSkipped := 0;
    Stitching := 0;
    StitchingSkipped := 0;

    { A step of nothing would never leave its row. }
    If MaxSpacing <= 0.0 Then Exit;

    Stagger := False;
    K := 0;
    RowY := FenceY;

    { Both loops stop where TryVia would start refusing for the board's
      edge, so what is counted as skipped is a site that something was
      in the way of, never one that was off the board. }
    While ClearOfEdge(RowY, ViaDia, MinGap, HalfS) Do
    Begin
        If Stagger Then
            RowShift := MaxSpacing / 2.0
        Else
            RowShift := 0.0;

        ColX := RowShift;
        While ClearOfEdge(ColX, ViaDia, MinGap, HalfL) Do
        Begin
            OnFence := (K = 0) And (ColX <= TraceHalfL);

            { SX and SY pick the quadrant. A site on the centre line has
              no mirror image to place, or it would be placed twice. }
            For SX := 0 To 1 Do
            Begin
                For SY := 0 To 1 Do
                Begin
                    If (SX = 0) Or (ColX > 0.0) Then
                    Begin
                        X := ColX;
                        If SX = 1 Then X := -ColX;
                        Y := RowY;
                        If SY = 1 Then Y := -RowY;

                        Ok := TryVia(Board, X, Y, ViaDia, MinGap, HalfL, HalfS,
                                     CavHalfL, CavHalfS, Corridor, TraceHalfL,
                                     LaunchX, VoidR);
                        If OnFence Then
                            Tally(Ok, Shielding, ShieldingSkipped)
                        Else
                            Tally(Ok, Stitching, StitchingSkipped);
                    End;
                End;
            End;

            ColX := ColX + MaxSpacing;
        End;

        K := K + 1;
        RowY := FenceY + K * MaxSpacing;
        Stagger := Not Stagger;
    End;

    { The centre line, behind the launches only -- past the connector's
      anti-pad void. TryVia would refuse every site before that anyway,
      but as a skipped one, and each of those would be counted. Half a
      step over from the fence's own row. }
    ColX := MaxSpacing / 2.0;
    While ClearOfEdge(ColX, ViaDia, MinGap, HalfL) Do
    Begin
        If ColX >= LaunchX + VoidR Then
        Begin
            For SX := 0 To 1 Do
            Begin
                X := ColX;
                If SX = 1 Then X := -ColX;
                Ok := TryVia(Board, X, 0.0, ViaDia, MinGap, HalfL, HalfS,
                             CavHalfL, CavHalfS, Corridor, TraceHalfL,
                             LaunchX, VoidR);
                Tally(Ok, Stitching, StitchingSkipped);
            End;
        End;
        ColX := ColX + MaxSpacing;
    End;
End;

{ Outline, centre conductor, coplanar grounds either side of the gap, a
  ground plane on the far side of the substrate, and -- where the board
  has two layers to stitch -- a via fence down both sides of the line.

  CopperHalfL/CopperHalfS are the extent of the GROUND copper, separate
  from HalfL/HalfS, which stay the substrate's true physical outline.
  On the package they are the same (the ground plane runs to the real
  board edge, which is correct); on the die they are pulled in by
  ChipCopperInset, so the die's own copper does not reach the edge --
  see ChipCopperInset's comment for why.

  CavHalfL/CavHalfS are the die's opening, and CavThrough says whether
  it goes right through -- which decides only whether the plane on the
  far face has to go round it. Both zero on the die itself. }
Procedure BuildBoard(Board : IPCB_Board; HalfL, HalfS, CopperHalfL, CopperHalfS,
                     Trace, Gap, TraceHalfL, FillHalfL,
                     LaunchX, VoidR, SlotHalf, LandHalf,
                     CavHalfL, CavHalfS : Double; CavThrough : Boolean;
                     WantBackPlane : Boolean);
Var
    GndInner : Double;
Begin
    SetOutline(Board, HalfL, HalfS);

    { The centre conductor stops at the launch (or, on the die, at the
      copper inset) rather than running to the physical edge. Running it
      to the edge is what let the connector land bridge the signal into
      both coplanar grounds, and separately let the die's trace meet the
      PCB's copper with no gap between them.

      It also stops at the die's opening and picks up again on the far
      side: the line does not cross the pocket, the bond wires do. A
      track drawn straight through would be a strip of copper spanning a
      hole on the real board, and in the model it would short the two
      ends together in parallel with the wires it is there to represent
      -- which reads as a perfectly good line right up until you notice
      the bonds make no difference to the result. }
    If (CavHalfL > 0.0) And (CavHalfL < TraceHalfL) Then
    Begin
        AddTrack(Board, -TraceHalfL, 0.0, -CavHalfL, 0.0, Trace, eTopLayer);
        AddTrack(Board,  CavHalfL,   0.0,  TraceHalfL, 0.0, Trace, eTopLayer);
    End
    Else
        AddTrack(Board, -TraceHalfL, 0.0, TraceHalfL, 0.0, Trace, eTopLayer);

    { The coplanar grounds. Not plain rectangles on the package: each
      carries the connector's circular anti-pad cut out of its inner
      edge, and the die's opening out of its middle. See AddGroundPlane. }
    GndInner := Trace / 2.0 + Gap;
    AddGroundPlane(Board,  1.0, CopperHalfL, CopperHalfS, GndInner,
                   LaunchX, VoidR, SlotHalf, LandHalf, CavHalfL, CavHalfS);
    AddGroundPlane(Board, -1.0, CopperHalfL, CopperHalfS, GndInner,
                   LaunchX, VoidR, SlotHalf, LandHalf, CavHalfL, CavHalfS);

    { Ground BEHIND each launch -- between the connector's outermost
      ground land and the board edge, and nowhere else.

      The connector's footprint is a U of ground lands open toward the
      die, with the signal land sitting inside the opening. That inside
      is deliberately clear: it is the launch, and its shape is the
      vendor's, not ours to fill in. The two arms already sit within the
      coplanar ground planes and the strap already bridges them across
      the corridor, so the footprint is connected to ground all round
      without any help. What was genuinely missing was metal in the
      corridor BEHIND the strap, where the ground otherwise just stops
      short of the board edge.

      FillHalfL therefore comes from the strap, not from the trace and
      not from the via fence. Both earlier attempts reached inboard of
      it and into live signal: from the fence's outer bound the fill
      landed 0.7 mm inside the trace, and from the trace's end it still
      overlapped the connector's own signal land by 0.45 mm. Everything
      on this layer is united into one body, so either is a dead short
      from signal to ground -- and the union leaves no seam, so nothing
      in the tree shows it. }
    If FillHalfL < CopperHalfL Then
    Begin
        AddRect(Board, -CopperHalfL, -GndInner, -FillHalfL, GndInner, eTopLayer);
        AddRect(Board,  FillHalfL, -GndInner, CopperHalfL,  GndInner, eTopLayer);
    End;

    { The die carries metal on ONE face. Giving it a back plane would
      put a conductor between the die and the board that the real part
      does not have, right where the fields are strongest.

      A through cutout takes the far face with it, so the plane there
      has to go round the opening. A blind pocket does not: it is milled
      from the top and the floor underneath is still board, so the plane
      stays whole -- which is also what makes that floor worth leaving. }
    If WantBackPlane Then
    Begin
        If CavThrough Then
            AddBackPlane(Board, CopperHalfL, CopperHalfS, CavHalfL, CavHalfS)
        Else
            AddBackPlane(Board, CopperHalfL, CopperHalfS, 0.0, 0.0);
    End;
End;

{ Count what is actually on the board, in memory, before it is saved.
  This is what separates "the objects were never added" from "they were
  added and the save dropped them" -- two failures that look identical
  from the outside. }
Procedure ReportCounts(Board : IPCB_Board; Tag : String);
Var
    It   : IPCB_BoardIterator;
    Prim : IPCB_Primitive;
    nT, nV, nP, nR, nO : Integer;
Begin
    nT := 0; nV := 0; nP := 0; nR := 0; nO := 0;
    It := Board.BoardIterator_Create;
    It.AddFilter_ObjectSet(AllObjects);
    It.AddFilter_LayerSet(AllLayers);
    It.AddFilter_Method(eProcessAll);
    Prim := It.FirstPCBObject;
    While Prim <> Nil Do
    Begin
        Case Prim.ObjectId Of
            eTrackObject  : nT := nT + 1;
            eViaObject    : nV := nV + 1;
            ePadObject    : nP := nP + 1;
            eRegionObject : nR := nR + 1;
        Else
            nO := nO + 1;
        End;
        Prim := It.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(It);
    Say(Tag + '_inmem_tracks=' + IntToStr(nT));
    Say(Tag + '_inmem_vias=' + IntToStr(nV));
    Say(Tag + '_inmem_pads=' + IntToStr(nP));
    Say(Tag + '_inmem_regions=' + IntToStr(nR));
End;

Procedure GenerateFromParams;
Var
    P            : TStringList;
    Doc          : IServerDocument;
    ChipDoc      : IServerDocument;
    Board        : IPCB_Board;
    OutDir       : String;
    BondPath     : String;
    HalfL, HalfS, Trace, Gap : Double;
    ChipHalfL, ChipHalfS, ChipGap, ChipTrace : Double;
    ViaDia, ViaMaxSpacing, ViaClear, ViaMinSpacing, Corridor, FenceY, TraceHalfL : Double;
    VoidR : Double;
    PadW, PadH : Double;
    PortCount, PortSpace : Double;
    SmpOrigin, BondSpan : Double;
    CavHalfL, CavHalfS, CavDepth : Double;
    CavThrough : Boolean;
    NoChip : Boolean;
    nStitch, nStitchSkip, nShield, nShieldSkip, nBonds, nLaunch, i : Integer;
Begin
    Rpt := TStringList.Create;
    Rpt.Add('stage=start');
    Rpt.SaveToFile(OutPath);

    P := TStringList.Create;
    Try
        P.LoadFromFile(ParamPath);
    Except
        Say('ok=0');
        Say('error=cannot read the parameter file');
        Say('stage=end');
        Exit;
    End;

    HalfL     := ParamOf(P, 'pcb_length', 26.0) / 2.0;
    HalfS     := ParamOf(P, 'pcb_side', 10.0) / 2.0;
    Trace     := ParamOf(P, 'cpw_trace_width', 0.2);
    Gap       := ParamOf(P, 'pcb_cpw_gap', 0.27);
    ChipHalfL := ParamOf(P, 'chip_length', 6.3) / 2.0;
    ChipHalfS := ParamOf(P, 'chip_side', 6.3) / 2.0;
    ChipGap   := ParamOf(P, 'chip_cpw_gap', 0.17);
    { The die's own line: a width that suits the board is not necessarily a
      CPW on the silicon (Python solves ChipGap for this width). Without the
      parameter, the board's, as before. }
    ChipTrace := ParamOf(P, 'chip_trace_width', Trace);
    ViaDia    := ParamOf(P, 'via_diameter', 0.3);
    ViaMaxSpacing := ParamOf(P, 'via_max_spacing', 0.8);
    ViaClear  := ParamOf(P, 'via_gnd_clearance', 0.15);
    ViaMinSpacing := ParamOf(P, 'via_min_spacing', 0.2);
    PadW      := ParamOf(P, 'bond_pad_length', 0.3);
    PadH      := ParamOf(P, 'bond_pad_width', 0.15);
    PortCount := ParamOf(P, 'port_count', 2);
    PortSpace := ParamOf(P, 'port_space', 1.5);
    BondSpan    := ParamOf(P, 'bonding_clearence', 0.4);
    VoidR     := ParamOf(P, 'smp_void_r', 2.25);
    OutDir    := P.Values['out_dir'];
    BondPath  := OutDir + '\bonds.txt';

    { The die's opening. Already decided in Python -- size, depth and
      through-or-blind -- so nothing here re-derives it from the chip
      and board thicknesses. The defaults are the die's own size with no
      clearance at all and a through cut, which is the safe reading of a
      parameter file too old to carry these: a cut that is certainly big
      enough for the die and certainly manufacturable. }
    CavHalfL   := ParamOf(P, 'cavity_length', ChipHalfL * 2.0) / 2.0;
    CavHalfS   := ParamOf(P, 'cavity_side', ChipHalfS * 2.0) / 2.0;
    CavDepth   := ParamOf(P, 'cavity_depth', ParamOf(P, 'pcb_width', 0.381));
    CavThrough := ParamOf(P, 'cavity_through', 1.0) > 0.5;

    { Bare-board mode (no_chip = 1): no die, so no opening, no bond
      fingers and no chip document. A zero-size opening is already how
      every routine here reads "no opening" -- the centre conductor runs
      unbroken and the ground and back planes stay whole. }
    NoChip := ParamOf(P, 'no_chip', 0.0) > 0.5;
    If NoChip Then
    Begin
        CavHalfL := 0.0;
        CavHalfS := 0.0;
        CavThrough := False;
        Say('no_chip=1');
    End;

    { The corner screws' keep-out. The centre is screw_inset in from the
      board edge on both axes -- the same arithmetic screws.plan does in
      Python, so the two cannot place a screw differently. }
    ScrewKeepR := 0.0;
    ScrewBlockS := 0.0;
    If ParamOf(P, 'screws_on', 0.0) > 0.5 Then
    Begin
        ScrewU := HalfL - ParamOf(P, 'screw_inset', 3.5);
        ScrewV := HalfS - ParamOf(P, 'screw_inset', 3.5);
        ScrewKeepR := ParamOf(P, 'screw_keepout_diameter', 3.0) / 2.0;
        { The square the plane gives up round each screw: the keep-out plus
          half a millimetre each way (screws.KEEPOUT_BLOCK_MARGIN). }
        ScrewBlockS := ScrewKeepR + 0.5;
        PkgHalfL := HalfL;
        PkgHalfS := HalfS;
        Say('screws_on=1');
        Say('screw_keepout_r_mm=' + FloatToStr(ScrewKeepR));
    End;

    { Placed so the outermost land sits port_space from the board edge. }
    SmpOrigin := HalfL - PortSpace + ParamOf(P, 'smp_outer_v', -2.6);

    Say('pcb_gap_um=' + FloatToStr(Gap * 1000));
    Say('chip_gap_um=' + FloatToStr(ChipGap * 1000));
    If CavThrough Then
        Say('cavity_kind=through')
    Else
        Say('cavity_kind=pocket');
    Say('cavity_length_mm=' + FloatToStr(CavHalfL * 2.0));
    Say('cavity_side_mm=' + FloatToStr(CavHalfS * 2.0));
    Say('cavity_depth_mm=' + FloatToStr(CavDepth));
    Say('cavity_clearance_um=' + FloatToStr((CavHalfL - ChipHalfL) * 1000));

    Doc := Client.OpenNewDocument('PCB', 'package', 'package', False);
    If Doc = Nil Then
    Begin
        Say('ok=0');
        Say('error=OpenNewDocument returned nil for the package');
        Say('stage=end');
        Exit;
    End;
    Client.ShowDocument(Doc);
    Board := PCBServer.GetCurrentPCBBoard;
    If Board = Nil Then
    Begin
        Say('ok=0');
        Say('error=no board on the package document');
        Say('stage=end');
        Exit;
    End;

    { The signal's own extent -- from one connector's land to the
      other's -- computed once here because BuildBoard draws the centre
      conductor out to exactly this, and AddViaLattice later bounds
      the fence to the same span rather than a separately hand-tuned
      offset. Two callers reading two different derivations of what is
      supposed to be the same edge is exactly how it drifted before. }
    TraceHalfL := SmpOrigin - ParamOf(P, 'smp_sig_v', 2.9);

    PCBServer.PreProcess;
    Try
        { The ground fill stops where the launch begins -- its innermost
          land. The board's own copper runs to the real board edge
          (CopperHalfL/S = HalfL/S), unlike the die's. Vias are not
          placed here at all any more -- see AddViaLattice, called
          once everything it needs to check itself against (the
          launch, the bond fingers) is actually on the board. }
        BuildBoard(Board, HalfL, HalfS, HalfL, HalfS, Trace, Gap,
                   TraceHalfL,
                   { ground fill starts at the OUTER edge of the
                     connector's own ground land, so it butts against
                     that land -- both are ground -- and leaves the
                     whole footprint interior alone. It used to be taken
                     from the ground strap's inner edge, which the KiCad
                     land pattern had and the vendor's Altium one does
                     not; the land's outer edge is the same place and
                     exists on both. }
                   SmpOrigin - ParamOf(P, 'smp_outer_v', -2.6),
                   { the launch axis, and the vendor's anti-pad around it }
                   SmpOrigin,
                   VoidR,
                   ParamOf(P, 'smp_slot_half', 0.95),
                   ParamOf(P, 'smp_land_half', 2.65),
                   { the die's opening, which the trace and both planes
                     have to keep out of }
                   CavHalfL, CavHalfS, CavThrough,
                   True);
        Say('package_body=ok');
    Except
        Say('package_body=FAIL');
    End;
    PCBServer.PostProcess;

    { The copper that stands round the screw holes, to go with the notches
      the planes above gave up for it. }
    If ScrewKeepR > 0.0 Then
    Begin
        PCBServer.PreProcess;
        Try
            AddScrewBlocks(Board);
            Say('package_screw_copper=ok');
        Except
            Say('package_screw_copper=FAIL');
        End;
        PCBServer.PostProcess;
    End;

    { The opening itself, after the copper: the copper is what has to
      make room for it, so a failure here leaves a board whose planes
      already stand clear rather than one with copper across a hole. }
    If NoChip Then
        Say('package_cavity=skipped')
    Else
    Begin
        PCBServer.PreProcess;
        Try
            AddCavity(Board, CavHalfL, CavHalfS, CavDepth, CavThrough);
            Say('package_cavity=ok');
        Except
            Say('package_cavity=FAIL');
        End;
        PCBServer.PostProcess;
    End;

    { The four screw holes, once the copper they are cut through exists. }
    If ScrewKeepR > 0.0 Then
    Begin
        PCBServer.PreProcess;
        Try
            AddScrewHole(Board, -ScrewU, -ScrewV, ParamOf(P, 'screw_clearance_diameter', 2.3));
            AddScrewHole(Board, -ScrewU,  ScrewV, ParamOf(P, 'screw_clearance_diameter', 2.3));
            AddScrewHole(Board,  ScrewU, -ScrewV, ParamOf(P, 'screw_clearance_diameter', 2.3));
            AddScrewHole(Board,  ScrewU,  ScrewV, ParamOf(P, 'screw_clearance_diameter', 2.3));
            Say('package_screw_holes=4');
        Except
            Say('package_screw_holes=FAIL');
        End;
        PCBServer.PostProcess;

        PCBServer.PreProcess;
        Try
            AddMountHole(Board, -ScrewU, -ScrewV, ParamOf(P, 'screw_clearance_diameter', 2.3));
            AddMountHole(Board, -ScrewU,  ScrewV, ParamOf(P, 'screw_clearance_diameter', 2.3));
            AddMountHole(Board,  ScrewU, -ScrewV, ParamOf(P, 'screw_clearance_diameter', 2.3));
            AddMountHole(Board,  ScrewU,  ScrewV, ParamOf(P, 'screw_clearance_diameter', 2.3));
            Say('package_mount_pads=4');
        Except
            Say('package_mount_pads=FAIL');
        End;
        PCBServer.PostProcess;
    End;

    { The board outline, as tracks on Mechanical 2 for the Gerber output
      (and the die's through-opening, where it goes through). }
    PCBServer.PreProcess;
    Try
        Board.LayerStack_V7.LayerObject[eMechanical2].MechanicalLayerEnabled := True;
    Except
    End;
    Try
        Board.LayerStack_V7.LayerObject[eMechanical2].Name := 'Board Outline';
    Except
    End;
    Try
        AddOutlineBox(Board, HalfL, HalfS);
        If CavThrough And (CavHalfL > 0.0) Then
            AddOutlineBox(Board, CavHalfL, CavHalfS);
        Say('package_outline_layer=ok');
    Except
        Say('package_outline_layer=FAIL');
    End;
    PCBServer.PostProcess;

    { The library is opened OUTSIDE the edit batch. PreProcess and
      PostProcess bracket edits to one board, and opening a second
      document in the middle of that -- which is what reading the
      library is -- is not what the pair is there to describe. }
    Say('trying=open the connector library');
    If OpenSmpLibrary(P.Values['smp_library'], P.Values['smp_footprint']) Then
    Begin
        PCBServer.PreProcess;
        Try
            nLaunch := 0;
            For i := 0 To Round(PortCount) - 1 Do
            Begin
                If i = 0 Then
                    nLaunch := nLaunch + AddSmpLaunch(Board, Doc, 'J1', -1.0, SmpOrigin)
                Else
                    nLaunch := nLaunch + AddSmpLaunch(Board, Doc, 'J2',  1.0, SmpOrigin);
            End;
            Say('package_ports=' + IntToStr(Round(PortCount)));
            Say('package_launch_primitives=' + IntToStr(nLaunch));
        Except
            Say('package_ports=FAIL');
        End;
        PCBServer.PostProcess;
    End
    Else
        Say('package_ports=NO_LAUNCH_PLACED');

    CloseSmpLibrary;
    Client.ShowDocument(Doc);
    Say('signal_pad_mm=' + FloatToStr(TraceHalfL));

    If NoChip Then
        Say('package_bond_pads=skipped')
    Else
    Begin
        PCBServer.PreProcess;
        Try
            nBonds := AddBondPads(Board, BondPath, PadW, PadH, Trace, False);
            Say('package_bond_pads=' + IntToStr(nBonds));
        Except
            Say('package_bond_pads=FAIL');
        End;
        PCBServer.PostProcess;
    End;

    { The vias run LAST, now that the launch and the bond fingers are
      really on the board: TryVia checks every candidate against the
      board as it stands, so placing vias any earlier would have it
      checking against a board that was not finished yet -- which is
      how the fence used to land on pads neither it nor the shield ring
      it used to run alongside knew existed.

      The fence and the fill are one lattice now, so there is no longer
      a question of which goes first: the two used to be separate grids
      that had to be run in a particular order to keep one from
      claiming the other's sites, and the lattice has no sites that
      belong to two. See AddViaLattice. }
    Corridor := Trace / 2.0 + Gap + ViaClear;
    FenceY   := Corridor + ViaDia / 2.0;

    PCBServer.PreProcess;
    Try
        nShield := 0;
        nShieldSkip := 0;
        nStitch := 0;
        nStitchSkip := 0;
        AddViaLattice(Board, FenceY, TraceHalfL,
                      ViaDia, ViaMaxSpacing, ViaMinSpacing,
                      HalfL, HalfS, CavHalfL, CavHalfS,
                      Corridor, SmpOrigin, VoidR,
                      nShield, nShieldSkip, nStitch, nStitchSkip);
        Say('package_shielding_vias=' + IntToStr(nShield));
        Say('package_shielding_skipped=' + IntToStr(nShieldSkip));
        Say('package_stitching_vias=' + IntToStr(nStitch));
        Say('package_stitching_skipped=' + IntToStr(nStitchSkip));
    Except
        Say('package_shielding_vias=FAIL');
        Say('package_stitching_vias=FAIL');
    End;
    PCBServer.PostProcess;

    Try
        Board.ViewManager_FullUpdate;
    Except
    End;

    ReportCounts(Board, 'package');

    If OutDir <> '' Then
    Begin
        Try
            Doc.SetFileName(OutDir + '\package.PcbDoc');
            Doc.DoFileSave('');
            Say('saved_package=ok');
            { Close it once saved. Documents left open accumulate across
              runs, all claiming the same path, and a later reader can
              be handed a previous run's copy. }
            Client.CloseDocument(Doc);
        Except
            Say('saved_package=FAIL');
        End;
    End;

    { The die's board has no screws, and its planes must stay plain. }
    ScrewBlockS := 0.0;

    If NoChip Then
    Begin
        Say('chip_body=skipped');
        Say('chip_bond_pads=skipped');
        Say('ok=1');
        Say('stage=end');
        Exit;
    End;

    ChipDoc := Client.OpenNewDocument('PCB', 'chip', 'chip', False);
    If ChipDoc = Nil Then
        Say('chip=FAIL_no_document')
    Else
    Begin
        Client.ShowDocument(ChipDoc);
        Board := PCBServer.GetCurrentPCBBoard;

        PCBServer.PreProcess;
        Try
            { No via fence and no back plane on the die: it carries a
              single metal layer. Its copper (trace and grounds) is
              pulled in from the die edge by ChipCopperInset -- see that
              constant's comment -- while the SUBSTRATE outline stays
              the die's real size (ChipHalfL, ChipHalfS, unchanged). }
            { The last argument suppresses the ground fill: the die has
              no connector to put metal behind, and any value not less
              than its copper extent leaves the fill unbuilt. }
            BuildBoard(Board, ChipHalfL, ChipHalfS,
                       ChipHalfL - ChipCopperInset, ChipHalfS - ChipCopperInset,
                       ChipTrace, ChipGap,
                       ChipHalfL - ChipCopperInset, ChipHalfL,
                       { no connector on the die, so no anti-pad either --
                         its ground planes stay plain rectangles }
                       0.0, 0.0, 0.0, 0.0,
                       { and no opening: the die is what goes IN one }
                       0.0, 0.0, False,
                       False);
            Say('chip_body=ok');
        Except
            Say('chip_body=FAIL');
        End;
        PCBServer.PostProcess;

        PCBServer.PreProcess;
        Try
            nBonds := AddBondPads(Board, BondPath, PadW, PadH, ChipTrace, True);
            Say('chip_bond_pads=' + IntToStr(nBonds));
        Except
            Say('chip_bond_pads=FAIL');
        End;
        PCBServer.PostProcess;

        Try
            Board.ViewManager_FullUpdate;
        Except
        End;

        ReportCounts(Board, 'chip');

        If OutDir <> '' Then
        Begin
            Try
                ChipDoc.SetFileName(OutDir + '\chip.PcbDoc');
                ChipDoc.DoFileSave('');
                Say('saved_chip=ok');
                Client.CloseDocument(ChipDoc);
            Except
                Say('saved_chip=FAIL');
            End;
        End;
    End;

    Say('ok=1');
    Say('stage=end');
End;

End.
