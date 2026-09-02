{ Generate the package PCB from default.txt.

  Stage 1: read the parameter file, create a NEW PCB document, set its
  outline from pcb_length x pcb_side, draw the CPW centre trace, and
  save it. Later stages add the ground pours, the via fence, the
  connectors, the shield ring and the bonds.

  Conventions, and why they matter:

  * The board is CENTRED ON THE ORIGIN. This is the only datum shared
    between Altium and SolidWorks. Copper is positioned in HFSS by
    arithmetic rather than by mates, so if the centre ever drifts, the
    copper silently lands in the wrong place with nothing to catch it.

  * Idempotent: the document is created fresh every run. The .PcbDoc is
    OUTPUT and is never hand-edited, so there is no state to drift.

  * Every uncertain call reports itself, and progress is flushed BEFORE
    each risky call. Altium leaves no stack trace, no exit code and no
    dialog for a runtime error -- the sentinel file is the only
    diagnostic there is.

  API spellings, each established the hard way and none of them
  guessable from the documentation:

      Client.OpenNewDocument('PCB', name, caption, False)   -- FOUR args
      Board.BoardOutline.PointCount / .Segments[i].vx/.vy/.Kind
      PCBServer.PCBObjectFactory(eTrackObject, eNoDimension, eCreate_Default)
      Board.AddPCBObject(prim)

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

{ A straight copper track between two points, in millimetres. }
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
End;

Procedure GenerateFromParams;
Var
    P            : TStringList;
    Doc          : IServerDocument;
    Board        : IPCB_Board;
    PcbLength    : Double;
    PcbSide      : Double;
    TraceW       : Double;
    HalfL, HalfS : Double;
    OutDir       : String;
    i            : Integer;
    Seg          : TPolySegment;
Begin
    Rpt := TStringList.Create;
    Rpt.Add('stage=start');
    Rpt.SaveToFile(OutPath);

    P := TStringList.Create;
    Try
        P.LoadFromFile(ParamPath);
    Except
        Say('ok=0'); Say('error=cannot read params'); Say('stage=end'); Exit;
    End;

    PcbLength := ParamOf(P, 'pcb_length', 26.0);
    PcbSide   := ParamOf(P, 'pcb_side', 10.0);
    TraceW    := ParamOf(P, 'cpw_trace_width', 0.2);
    OutDir    := P.Values['out_dir'];
    HalfL := PcbLength / 2.0;
    HalfS := PcbSide / 2.0;
    Say('pcb_length=' + FloatToStr(PcbLength));
    Say('pcb_side=' + FloatToStr(PcbSide));
    Say('cpw_trace_width=' + FloatToStr(TraceW));

    Doc := Client.OpenNewDocument('PCB', 'package', 'package', False);
    If Doc = Nil Then
    Begin
        Say('ok=0'); Say('error=OpenNewDocument returned nil'); Say('stage=end'); Exit;
    End;
    Client.ShowDocument(Doc);
    Board := PCBServer.GetCurrentPCBBoard;
    If Board = Nil Then
    Begin
        Say('ok=0'); Say('error=no board on the new document'); Say('stage=end'); Exit;
    End;
    Say('board=' + Board.FileName);

    { Four corners, not five. Altium closes the outline itself, and the
      duplicated closing point is what left the default 6x4 inch shape
      in place on the previous attempt. }
    { Segments[i] returns the record BY VALUE. Writing
      Segments[i].vx := ... therefore mutates a temporary and is
      discarded -- which is why the previous attempt reported success
      while the board stayed at its default 6 x 4 inches. The whole
      record has to be read out, edited, and assigned back. }
    PCBServer.PreProcess;
    Try
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
        Say('outline=ok');
    Except
        Say('outline=FAIL');
    End;
    PCBServer.PostProcess;

    { The CPW centre conductor, along the feedline axis through centre. }
    Try
        AddTrack(Board, -HalfL, 0.0, HalfL, 0.0, TraceW, eTopLayer);
        Say('cpw_trace=ok');
    Except
        Say('cpw_trace=FAIL');
    End;

    Try Board.ViewManager_FullUpdate; Except End;

    { Report the outline back in millimetres, so the caller can check the
      board is the size it asked for rather than trusting 'ok'. }
    Try
        Say('outline_width_mm='  + FloatToStr(CoordToMMs(Board.BoardOutline.BoundingRectangle.Right - Board.BoardOutline.BoundingRectangle.Left)));
        Say('outline_height_mm=' + FloatToStr(CoordToMMs(Board.BoardOutline.BoundingRectangle.Top - Board.BoardOutline.BoundingRectangle.Bottom)));
    Except
        Say('outline_measure=FAIL');
    End;

    If OutDir <> '' Then
    Begin
        Try
            Doc.DoFileSaveAs(OutDir + '\package.PcbDoc', True);
            Say('save_A=ok');
        Except
            Say('save_A=fail');
        End;
        Try
            Doc.DoFileSaveAs(OutDir + '\package.PcbDoc');
            Say('save_B=ok');
        Except
            Say('save_B=fail');
        End;
        Try
            Doc.SetFileName(OutDir + '\package.PcbDoc');
            Doc.DoFileSave('');
            Say('save_C=ok');
        Except
            Say('save_C=fail');
        End;
    End;

    Say('ok=1');
    Say('stage=end');
End;

End.
