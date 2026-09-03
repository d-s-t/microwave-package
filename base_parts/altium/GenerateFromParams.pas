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

  * Every feature is guarded and counted separately, because Altium
    leaves no stack trace, no exit code and no dialog for a runtime
    error. The report file is the only diagnostic there is.

  API spellings established empirically, none of them guessable:

      Client.OpenNewDocument -- four arguments; three returns nil
      BoardOutline.Segments[i] returns the record BY VALUE, so it must
          be read out, edited and assigned back
      PCBServer.PCBObjectFactory(kind, eNoDimension, eCreate_Default)
      PCBServer.PCBContourFactory for region outlines
      Doc.SetFileName then Doc.DoFileSave

  Note: Pascal block comments do not nest.
  Altium internal units are 1/10000 mil; MMsToCoord converts.
}

Const
    ParamPath = 'C:\Users\david\Documents\uni\lab\microwave-package\base_parts\altium\params.txt';
    OutPath   = 'C:\Users\david\Documents\uni\lab\microwave-package\base_parts\altium\generate_report.txt';

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

Procedure SetOutline(Board : IPCB_Board; HalfL, HalfS : Double);
Var
    i   : Integer;
    Seg : TPolySegment;
Begin
    Board.BoardOutline.BeginModify;
    Board.BoardOutline.PointCount := 4;
    For i := 0 To 3 Do
    Begin
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

{ Outline, centre conductor, coplanar grounds either side of the gap, a
  ground plane on the far side of the substrate, and -- where the board
  has two layers to stitch -- a via fence down both sides of the line. }
Procedure BuildBoard(Board : IPCB_Board; HalfL, HalfS, Trace, Gap,
                     ViaDia, ViaPitch, ViaClear, TraceHalfL : Double;
                     WantVias : Boolean; WantBackPlane : Boolean;
                     Var nVias : Integer);
Var
    GndInner, FenceY, X : Double;
Begin
    SetOutline(Board, HalfL, HalfS);

    { The centre conductor stops at the launch rather than running to the
      board edge. Running it out to the edge is what let the connector
      land bridge the signal into both coplanar grounds. }
    AddTrack(Board, -TraceHalfL, 0.0, TraceHalfL, 0.0, Trace, eTopLayer);

    GndInner := Trace / 2.0 + Gap;
    AddRect(Board, -HalfL,  GndInner, HalfL,  HalfS,    eTopLayer);
    AddRect(Board, -HalfL, -HalfS,    HalfL, -GndInner, eTopLayer);

    { The die carries metal on ONE face. Giving it a back plane would
      put a conductor between the die and the board that the real part
      does not have, right where the fields are strongest. }
    If WantBackPlane Then
        AddRect(Board, -HalfL, -HalfS, HalfL, HalfS, eBottomLayer);

    nVias := 0;
    If WantVias Then
    Begin
        FenceY := GndInner + ViaClear + ViaDia / 2.0;
        If FenceY + ViaDia / 2.0 < HalfS Then
        Begin
            X := -HalfL + ViaPitch;
            While X < HalfL - ViaPitch / 2.0 Do
            Begin
                AddVia(Board, X,  FenceY, ViaDia, ViaDia / 2.0);
                AddVia(Board, X, -FenceY, ViaDia, ViaDia / 2.0);
                nVias := nVias + 2;
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

{ A ring of stitching vias around the chip pocket.

  The ring deliberately BREAKS where the CPW crosses it. A via dropped
  into the signal corridor would short the line to ground, and the
  generator would cheerfully report success while producing a dead
  board. `Corridor` is the half-height of the keep-out: trace plus gap
  plus a via radius of margin. }
Function AddShieldRing(Board : IPCB_Board;
                       HalfX, HalfY, ViaDia, ViaPitch, Corridor : Double) : Integer;
Var
    X, Y : Double;
    n    : Integer;
Begin
    n := 0;

    { Top and bottom runs: full width, no corridor to worry about. }
    X := -HalfX;
    While X <= HalfX Do
    Begin
        AddVia(Board, X,  HalfY, ViaDia, ViaDia / 2.0);
        AddVia(Board, X, -HalfY, ViaDia, ViaDia / 2.0);
        n := n + 2;
        X := X + ViaPitch;
    End;

    { Left and right runs: skip anything inside the signal corridor. }
    Y := -HalfY + ViaPitch;
    While Y < HalfY Do
    Begin
        If Abs(Y) > Corridor Then
        Begin
            AddVia(Board, -HalfX, Y, ViaDia, ViaDia / 2.0);
            AddVia(Board,  HalfX, Y, ViaDia, ViaDia / 2.0);
            n := n + 2;
        End;
        Y := Y + ViaPitch;
    End;

    Result := n;
End;

{ Bond fingers: the landing pads the wire bonds will run to.

  The bonds themselves are NOT drawn here. AD26 clearly models bond
  wires -- it has wirebond rules, violation types, JEDEC profile
  constants and export support -- but there is no wirebond entry in the
  scriptable object-id enum at all. Of its 52 object ids, none is a
  wirebond, so PCBObjectFactory cannot make one. That is the same
  partial exposure that blocked the 3D exporter.

  It costs nothing, because a bond is EM geometry rather than layout:
  copper_geometry builds the loops directly in HFSS from bond_count,
  bond_height and bond_wire_diameter, where the arc profile can be made
  exactly what the model needs. What the layout owes the bonds is the
  pads at each end, which is what this places. }
{ The SMP launch, from the vendor land pattern.

  Wuerth WR-SMP 60124102122403, read out of the KiCad footprint that
  ships with the part and passed in through the parameter file, so the
  vendor's numbers are not retyped here.

  The connector works in its own frame: v runs along the signal axis
  with the centre pin exiting toward +v, u runs across it. Three ground
  pads form a U open toward +v, and its closed end -- the strap -- joins
  the two sides of the ground. On a coplanar board those two sides are
  the CPW's own grounds, so the strap deliberately shorts them behind
  the launch. That is why the centre conductor has to stop at the signal
  pad and never reach the strap.

  Placement maps the connector frame onto the board with

      board x = Sign * (Origin - v)      board y = u

  and Origin is set so the outermost copper sits port_space from the
  board edge. Sign mirrors the whole thing for the other end. }
{ One land of the connector, mapped from the connector frame onto the
  board. A pad's extent along board x is its size along v, and along y
  its size along u -- the two frames are rotated a quarter turn.

  Top level on purpose: DelphiScript has no closures, and a nested
  procedure reading Board or Origin from the enclosing scope fails at
  parse time with "Can't access top level variable". }
Procedure AddLand(Board : IPCB_Board; Sign, Origin, U, V, SizeU, SizeV : Double);
Begin
    AddPad(Board, Sign * (Origin - V), U, SizeV, SizeU, eTopLayer);
End;

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

Function AddBondFingers(Board : IPCB_Board;
                        X, PadPitch, Count, PadW, PadH, GndInner : Double) : Integer;
Var
    i, nGround : Integer;
    Y, FirstY  : Double;
Begin
    { One finger on the centre conductor for the signal bond; the rest
      are ground bonds and belong ON the ground, not in the gap. Spacing
      them evenly about the centre line put them straddling the gap edge,
      which is metal the real board does not have in the one place the
      field is most sensitive to it. }
    AddPad(Board, X, 0.0, PadW, PadH, eTopLayer);
    Result := 1;

    FirstY := GndInner + PadH / 2.0;
    nGround := Round(Count) - 1;
    For i := 0 To nGround - 1 Do
    Begin
        Y := FirstY + (i div 2) * PadPitch;
        If (i mod 2) = 0 Then
            AddPad(Board, X, Y, PadW, PadH, eTopLayer)
        Else
            AddPad(Board, X, -Y, PadW, PadH, eTopLayer);
        Result := Result + 1;
    End;
End;

Procedure GenerateFromParams;
Var
    P            : TStringList;
    Doc          : IServerDocument;
    ChipDoc      : IServerDocument;
    Board        : IPCB_Board;
    OutDir       : String;
    HalfL, HalfS, Trace, Gap : Double;
    ChipHalfL, ChipHalfS, ChipGap : Double;
    ViaDia, ViaPitch, ViaClear : Double;
    PadW, PadH, PadPitch, BondCount : Double;
    PortCount, PortSpace, PortLen, PortWid : Double;
    PortX : Double;
    ShieldInset, BondWire, BondHeight, BondSpan : Double;
    SmpOrigin : Double;
    nVias : Integer;
    nRing : Integer;
    nBonds : Integer;
    i : Integer;
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
    PadPitch  := ParamOf(P, 'bond_pad_pitch', 0.4);
    BondCount := ParamOf(P, 'bond_count', 3);
    PortCount := ParamOf(P, 'port_count', 2);
    PortSpace := ParamOf(P, 'port_space', 1.5);
    PortLen   := ParamOf(P, 'port_length', 5.1);
    PortWid   := ParamOf(P, 'port_width', 5.1);
    { Placed so the outermost land sits port_space from the edge. }
    SmpOrigin := HalfL - PortSpace + ParamOf(P, 'smp_outer_v', -2.6);
    ShieldInset := ParamOf(P, 'shield_ring_inset', 0.5);
    BondWire    := ParamOf(P, 'bond_wire_diameter', 0.025);
    BondHeight  := ParamOf(P, 'bond_height', 0.2);
    BondSpan    := ParamOf(P, 'bonding_clearence', 1.0);
    OutDir    := P.Values['out_dir'];
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
        BuildBoard(Board, HalfL, HalfS, Trace, Gap, ViaDia, ViaPitch, ViaClear,
                   SmpOrigin - ParamOf(P, 'smp_sig_v', 2.9), True, True, nVias);
        Say('package_vias=' + IntToStr(nVias));
        Say('package_body=ok');
    Except
        Say('package_body=FAIL');
    End;
    PCBServer.PostProcess;

    { The SMP launch.

      The previous version dropped a port_length/4 square on the centre
      line. At 1.275 mm that is far wider than the gap edge at about
      0.37 mm, so it shorted the signal into both grounds -- the bridge
      between the two ground sides visible in the 3D view.

      The land is therefore no wider than the trace, which makes a short
      impossible by construction: it stays inside the corridor the two
      coplanar grounds already leave. The connector body is
      port_length x port_width and its OUTER edge sits port_space from
      the board edge, so the launch centre is one half-body further in.

      Exact SMP land dimensions belong to the connector datasheet; what
      is fixed here is the placement and the clearance. }
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
        Say('smp_origin_mm=' + FloatToStr(SmpOrigin));
        Say('signal_pad_mm=' + FloatToStr(SmpOrigin - ParamOf(P, 'smp_sig_v', 2.9)));
    Except
        Say('package_ports=FAIL');
    End;
    PCBServer.PostProcess;

    Try
        Board.ViewManager_FullUpdate;
    Except
    End;

    { Shield ring around the chip pocket, broken where the CPW crosses. }
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

    { Bond fingers facing the die, one row each side. The wires
      themselves are built in HFSS -- see AddBondFingers. }
    PCBServer.PreProcess;
    Try
        nBonds := AddBondFingers(Board, -ChipHalfL - BondSpan, PadPitch, BondCount,
                                 PadW, PadH, Trace / 2.0 + Gap)
                + AddBondFingers(Board,  ChipHalfL + BondSpan, PadPitch, BondCount,
                                 PadW, PadH, Trace / 2.0 + Gap);
        Say('package_bond_fingers=' + IntToStr(nBonds));
    Except
        Say('package_bond_fingers=FAIL');
    End;
    PCBServer.PostProcess;

    ReportCounts(Board, 'package');

    If OutDir <> '' Then
    Begin
        Try
            Doc.SetFileName(OutDir + '\package.PcbDoc');
            Doc.DoFileSave('');
            Say('saved_package=ok');
            { Close it once saved. Documents left open accumulate across
              runs, all claiming the same path, and a later reader can
              be handed a previous run's copy -- which is how an export
              once reported the board before its bond fingers existed. }
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
            { No via fence on the die: it carries a single metal layer,
              so there is nothing to stitch to. }
            BuildBoard(Board, ChipHalfL, ChipHalfS, Trace, ChipGap,
                       ViaDia, ViaPitch, ViaClear, ChipHalfL, False, False, nVias);
            Say('chip_body=ok');
        Except
            Say('chip_body=FAIL');
        End;
        PCBServer.PostProcess;

        PCBServer.PreProcess;
        Try
            { Same rule as the board: signal pad on the line, ground
              pads clear of the die's own gap. }
            nBonds := AddBondFingers(Board, -ChipHalfL + PadW, PadPitch, BondCount,
                                     PadW, PadH, Trace / 2.0 + ChipGap)
                    + AddBondFingers(Board,  ChipHalfL - PadW, PadPitch, BondCount,
                                     PadW, PadH, Trace / 2.0 + ChipGap);
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
