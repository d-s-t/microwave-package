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

  * Every feature is guarded and counted separately, because Altium
    leaves no stack trace, no exit code and no dialog for a runtime
    error. The report file is the only diagnostic there is.

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

  Altium internal units are 1/10000 mil; MMsToCoord converts.
}

Const
    ParamPath = 'C:\Users\david\Documents\uni\lab\microwave-package\base_parts\altium\params.txt';
    OutPath   = 'C:\Users\david\Documents\uni\lab\microwave-package\base_parts\altium\generate_report.txt';

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

Var
    Rpt : TStringList;

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

{ A filled rectangle of copper. Ground planes are drawn as plain regions
  rather than polygon pours: a pour needs a net and a repour pass, and
  for a shape this simple the region is both exact and cheaper. }
Procedure AddRect(Board : IPCB_Board; X1, Y1, X2, Y2 : Double; Lay : TLayer);
Var
    R : IPCB_Region;
    C : IPCB_Contour;
Begin
    R := PCBServer.PCBObjectFactory(eRegionObject, eNoDimension, eCreate_Default);
    C := PCBServer.PCBContourFactory;
    C.AddPoint(MMsToCoord(X1), MMsToCoord(Y1));
    C.AddPoint(MMsToCoord(X2), MMsToCoord(Y1));
    C.AddPoint(MMsToCoord(X2), MMsToCoord(Y2));
    C.AddPoint(MMsToCoord(X1), MMsToCoord(Y2));
    R.SetOutlineContour(C);
    R.Layer := Lay;
    Board.AddPCBObject(R);
    Announce(Board, R);
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

{ One land of the SMP connector, mapped from the connector frame onto
  the board. A pad's extent along board x is its size along v, and along
  y its size along u -- the two frames are rotated a quarter turn.

  Top level on purpose: DelphiScript has no closures. }
Procedure AddLand(Board : IPCB_Board; Sign, Origin, U, V, SizeU, SizeV : Double);
Begin
    AddPad(Board, Sign * (Origin - V), U, SizeV, SizeU, eTopLayer);
End;

{ The SMP launch, from the vendor land pattern.

  Wuerth WR-SMP 60124102122403, read out of the KiCad footprint that
  ships with the part and passed in through the parameter file, so the
  vendor's numbers are not retyped here.

  The three ground pads form a U open toward +v, and its closed end --
  the strap -- joins the two sides of the ground. On a coplanar board
  those two sides are the CPW's own grounds, so the strap deliberately
  shorts them behind the launch. That is why the centre conductor stops
  at the signal pad and never reaches the strap. }
Procedure AddSmpLaunch(Board : IPCB_Board; P : TStringList; Sign, Origin : Double);
Begin
    AddLand(Board, Sign, Origin,
            ParamOf(P, 'smp_sig_u', 0.0),   ParamOf(P, 'smp_sig_v', 2.9),
            ParamOf(P, 'smp_sig_su', 0.48), ParamOf(P, 'smp_sig_sv', 1.4));
    AddLand(Board, Sign, Origin,
            ParamOf(P, 'smp_arml_u', -2.45), ParamOf(P, 'smp_arml_v', -0.1),
            ParamOf(P, 'smp_arml_su', 0.3),  ParamOf(P, 'smp_arml_sv', 4.0));
    AddLand(Board, Sign, Origin,
            ParamOf(P, 'smp_armr_u', 2.45),  ParamOf(P, 'smp_armr_v', -0.2),
            ParamOf(P, 'smp_armr_su', 0.3),  ParamOf(P, 'smp_armr_sv', 4.0));
    AddLand(Board, Sign, Origin,
            ParamOf(P, 'smp_strap_u', 0.0),  ParamOf(P, 'smp_strap_v', -2.45),
            ParamOf(P, 'smp_strap_su', 4.0), ParamOf(P, 'smp_strap_sv', 0.3));
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

{ A ring of stitching vias around the chip pocket.

  The ring deliberately BREAKS where the CPW crosses it. A via dropped
  into the signal corridor would short the line to ground while the
  generator cheerfully reported success. Corridor is the half-height of
  the keep-out: trace plus gap plus a via radius of margin. }
Function AddShieldRing(Board : IPCB_Board;
                       HalfX, HalfY, ViaDia, ViaPitch, Corridor : Double) : Integer;
Var
    X, Y : Double;
Begin
    Result := 0;

    X := -HalfX;
    While X <= HalfX Do
    Begin
        AddVia(Board, X,  HalfY, ViaDia, ViaDia / 2.0);
        AddVia(Board, X, -HalfY, ViaDia, ViaDia / 2.0);
        Result := Result + 2;
        X := X + ViaPitch;
    End;

    Y := -HalfY + ViaPitch;
    While Y < HalfY Do
    Begin
        If Abs(Y) > Corridor Then
        Begin
            AddVia(Board, -HalfX, Y, ViaDia, ViaDia / 2.0);
            AddVia(Board,  HalfX, Y, ViaDia, ViaDia / 2.0);
            Result := Result + 2;
        End;
        Y := Y + ViaPitch;
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
  see ChipCopperInset's comment for why. }
Procedure BuildBoard(Board : IPCB_Board; HalfL, HalfS, CopperHalfL, CopperHalfS,
                     Trace, Gap, ViaDia, ViaPitch, ViaClear, TraceHalfL,
                     FenceHalfL, FenceInnerL, FillHalfL : Double;
                     WantVias : Boolean; WantBackPlane : Boolean;
                     Var nVias : Integer);
Var
    GndInner, FenceY, X : Double;
Begin
    SetOutline(Board, HalfL, HalfS);

    { The centre conductor stops at the launch (or, on the die, at the
      copper inset) rather than running to the physical edge. Running it
      to the edge is what let the connector land bridge the signal into
      both coplanar grounds, and separately let the die's trace meet the
      PCB's copper with no gap between them. }
    AddTrack(Board, -TraceHalfL, 0.0, TraceHalfL, 0.0, Trace, eTopLayer);

    GndInner := Trace / 2.0 + Gap;
    AddRect(Board, -CopperHalfL,  GndInner, CopperHalfL,  CopperHalfS,    eTopLayer);
    AddRect(Board, -CopperHalfL, -CopperHalfS,    CopperHalfL, -GndInner, eTopLayer);

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
      does not have, right where the fields are strongest. }
    If WantBackPlane Then
        AddRect(Board, -CopperHalfL, -CopperHalfS, CopperHalfL, CopperHalfS, eBottomLayer);

    nVias := 0;
    If WantVias Then
    Begin
        FenceY := GndInner + ViaClear + ViaDia / 2.0;
        If FenceY + ViaDia / 2.0 < HalfS Then
        Begin
            { The fence runs between the die and each launch, and
              nowhere else.

              It stops at FenceHalfL because beyond that is the
              connector, and a via under its land is a hole under a
              solder joint. It stops again at FenceInnerL because inside
              that is the die pocket -- there is no board there to
              stitch, and the bond fingers are, which is where the fence
              was landing vias straight on top of them. }
            X := -FenceHalfL;
            While X <= FenceHalfL Do
            Begin
                If Abs(X) >= FenceInnerL Then
                Begin
                    AddVia(Board, X,  FenceY, ViaDia, ViaDia / 2.0);
                    AddVia(Board, X, -FenceY, ViaDia, ViaDia / 2.0);
                    nVias := nVias + 2;
                End;
                X := X + ViaPitch;
            End;
        End;
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
    ChipHalfL, ChipHalfS, ChipGap : Double;
    ViaDia, ViaPitch, ViaClear : Double;
    PadW, PadH : Double;
    PortCount, PortSpace : Double;
    ShieldInset, SmpOrigin, BondSpan : Double;
    nVias, nRing, nBonds, i : Integer;
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
    ViaDia    := ParamOf(P, 'via_diameter', 0.3);
    ViaPitch  := ParamOf(P, 'via_pitch', 0.8);
    ViaClear  := ParamOf(P, 'via_gnd_clearance', 0.15);
    PadW      := ParamOf(P, 'bond_pad_length', 0.3);
    PadH      := ParamOf(P, 'bond_pad_width', 0.15);
    PortCount := ParamOf(P, 'port_count', 2);
    PortSpace := ParamOf(P, 'port_space', 1.5);
    ShieldInset := ParamOf(P, 'shield_ring_inset', 0.9);
    BondSpan    := ParamOf(P, 'bonding_clearence', 0.4);
    OutDir    := P.Values['out_dir'];
    BondPath  := OutDir + '\bonds.txt';

    { Placed so the outermost land sits port_space from the board edge. }
    SmpOrigin := HalfL - PortSpace + ParamOf(P, 'smp_outer_v', -2.6);

    Say('pcb_gap_um=' + FloatToStr(Gap * 1000));
    Say('chip_gap_um=' + FloatToStr(ChipGap * 1000));

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

    PCBServer.PreProcess;
    Try
        { The fence and the ground fill both stop where the launch
          begins -- its innermost land. The board's own copper runs to
          the real board edge (CopperHalfL/S = HalfL/S), unlike the
          die's. }
        BuildBoard(Board, HalfL, HalfS, HalfL, HalfS, Trace, Gap,
                   ViaDia, ViaPitch, ViaClear,
                   SmpOrigin - ParamOf(P, 'smp_sig_v', 2.9),
                   SmpOrigin - ParamOf(P, 'smp_inner_v', 3.6),
                   { clear of the bond fingers at the die }
                   ChipHalfL + BondSpan + PadW / 2.0 + ViaDia,
                   { ground fill starts at the strap's inner edge, so it
                     merges with the strap -- both are ground -- and
                     leaves the whole footprint interior alone }
                   SmpOrigin - ParamOf(P, 'smp_strap_v', -2.45)
                             - ParamOf(P, 'smp_strap_sv', 0.3) / 2.0,
                   True, True, nVias);
        Say('package_vias=' + IntToStr(nVias));
        Say('package_body=ok');
    Except
        Say('package_body=FAIL');
    End;
    PCBServer.PostProcess;

    PCBServer.PreProcess;
    Try
        For i := 0 To Round(PortCount) - 1 Do
        Begin
            If i = 0 Then
                AddSmpLaunch(Board, P, -1.0, SmpOrigin)
            Else
                AddSmpLaunch(Board, P,  1.0, SmpOrigin);
        End;
        Say('package_ports=' + IntToStr(Round(PortCount)));
        Say('signal_pad_mm=' + FloatToStr(SmpOrigin - ParamOf(P, 'smp_sig_v', 2.9)));
    Except
        Say('package_ports=FAIL');
    End;
    PCBServer.PostProcess;

    PCBServer.PreProcess;
    Try
        nRing := AddShieldRing(Board,
                               ChipHalfL + ShieldInset, ChipHalfS + ShieldInset,
                               ViaDia, ViaPitch,
                               Trace / 2.0 + Gap + ViaDia / 2.0);
        Say('package_shield_vias=' + IntToStr(nRing));
    Except
        Say('package_shield_vias=FAIL');
    End;
    PCBServer.PostProcess;

    PCBServer.PreProcess;
    Try
        nBonds := AddBondPads(Board, BondPath, PadW, PadH, Trace, False);
        Say('package_bond_pads=' + IntToStr(nBonds));
    Except
        Say('package_bond_pads=FAIL');
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
                       Trace, ChipGap, ViaDia, ViaPitch, ViaClear,
                       ChipHalfL - ChipCopperInset, ChipHalfL, 0.0, ChipHalfL,
                       False, False, nVias);
            Say('chip_body=ok');
        Except
            Say('chip_body=FAIL');
        End;
        PCBServer.PostProcess;

        PCBServer.PreProcess;
        Try
            nBonds := AddBondPads(Board, BondPath, PadW, PadH, Trace, True);
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
