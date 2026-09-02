{ Write the copper geometry of a generated board out as plain text.

  This exists because Altium's own 3D export cannot deliver copper to
  us. Its STEP export drops copper by design, and while the Parasolid
  exporter configures happily from a script -- including per-layer
  copper -- the call that performs the export could not be reached
  across eight arities. See spikes/s2_export3.pas.

  Reading the primitives, on the other hand, works perfectly. So the
  copper is described here as data and rebuilt directly in HFSS by
  PyAEDT, which has three advantages over a CAD round trip: every solid
  gets exactly the name its material rule expects, nothing depends on
  imported face ids that change on every rebuild, and the copper
  thickness comes from the same parameter file as everything else.

  Coordinates are millimetres, relative to the board centre, which is
  the datum shared with SolidWorks. Positions are emitted exactly as
  Altium holds them; the Z placement is decided by the builder, since
  only it knows where the substrate sits in the assembly.

  Record formats, one per line:

      track|layer|x1|y1|x2|y2|width
      via|x|y|diameter|holesize
      pad|layer|x|y|xsize|ysize
      region|layer|x1,y1,x2,y2,...

  Note: Pascal block comments do not nest.
}

Const
    Dir     = 'C:\Users\david\Documents\uni\lab\microwave-package\base_parts\altium\generated\';
    OutPath = 'C:\Users\david\Documents\uni\lab\microwave-package\base_parts\altium\generated\copper_report.txt';

Var
    Rpt : TStringList;

Procedure Say(S : String);
Begin
    Rpt.Add(S);
    Rpt.SaveToFile(OutPath);
End;

Function LayerTag(L : TLayer) : String;
Begin
    If L = eTopLayer Then
        Result := 'top'
    Else If L = eBottomLayer Then
        Result := 'bottom'
    Else
        Result := 'other';
End;

Function MM(C : TCoord) : String;
Begin
    Result := FloatToStr(CoordToMMs(C));
End;

{ One board's copper, appended to Data. Returns how many records were
  written so the caller can report a per-board count. }
Function DumpBoard(FileName : String; Data : TStringList) : Integer;
Var
    Doc    : IServerDocument;
    Board  : IPCB_Board;
    It     : IPCB_BoardIterator;
    Prim   : IPCB_Primitive;
    Trk    : IPCB_Track;
    Via    : IPCB_Via;
    Pad    : IPCB_Pad;
    Rgn    : IPCB_Region;
    Contour : IPCB_Contour;
    Line   : String;
    i, n   : Integer;
Begin
    Result := 0;

    { Close EVERY copy already open, not just one. Documents accumulate
      across runs all claiming the same path, and closing a single one
      still leaves Altium free to hand back another -- which is how this
      export once reported a board as it stood before its bond fingers
      were added. Bounded, so a document that refuses to close cannot
      spin here forever. }
    Try
        For i := 1 To 10 Do
        Begin
            Doc := Client.GetDocumentByPath(Dir + FileName);
            If Doc = Nil Then Break;
            Client.CloseDocument(Doc);
        End;
    Except
    End;

    Doc := Client.OpenDocument('PCB', Dir + FileName);
    If Doc = Nil Then Exit;
    Client.ShowDocument(Doc);
    Board := PCBServer.GetCurrentPCBBoard;
    If Board = Nil Then Exit;

    n := 0;
    It := Board.BoardIterator_Create;
    It.AddFilter_ObjectSet(MkSet(eTrackObject, eViaObject, ePadObject, eRegionObject));
    It.AddFilter_LayerSet(AllLayers);
    It.AddFilter_Method(eProcessAll);
    Prim := It.FirstPCBObject;
    While Prim <> Nil Do
    Begin
        Line := '';
        Case Prim.ObjectId Of
            eTrackObject :
                Begin
                    Trk := Prim;
                    Line := 'track|' + LayerTag(Trk.Layer) + '|' +
                            MM(Trk.X1) + '|' + MM(Trk.Y1) + '|' +
                            MM(Trk.X2) + '|' + MM(Trk.Y2) + '|' + MM(Trk.Width);
                End;
            eViaObject :
                Begin
                    Via := Prim;
                    Line := 'via|' + MM(Via.X) + '|' + MM(Via.Y) + '|' +
                            MM(Via.Size) + '|' + MM(Via.HoleSize);
                End;
            ePadObject :
                Begin
                    Pad := Prim;
                    Line := 'pad|' + LayerTag(Pad.Layer) + '|' +
                            MM(Pad.X) + '|' + MM(Pad.Y) + '|' +
                            MM(Pad.TopXSize) + '|' + MM(Pad.TopYSize);
                End;
            eRegionObject :
                Begin
                    Rgn := Prim;
                    Contour := Rgn.GetMainContour;
                    If Contour <> Nil Then
                    Begin
                        Line := 'region|' + LayerTag(Rgn.Layer) + '|';
                        For i := 0 To Contour.Count - 1 Do
                        Begin
                            If i > 0 Then Line := Line + ',';
                            Line := Line + MM(Contour.x(i)) + ',' + MM(Contour.y(i));
                        End;
                    End;
                End;
        End;
        If Line <> '' Then
        Begin
            Data.Add(Line);
            n := n + 1;
        End;
        Prim := It.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(It);
    Result := n;
End;

Procedure ExportCopper;
Var
    Data : TStringList;
    nPkg, nChip : Integer;
Begin
    Rpt := TStringList.Create;
    Rpt.Add('stage=start');
    Rpt.SaveToFile(OutPath);

    Data := TStringList.Create;
    Try
        Data.Add('# copper geometry, millimetres, relative to board centre');

        Data.Add('board=package');
        nPkg := DumpBoard('package.PcbDoc', Data);
        Say('package_records=' + IntToStr(nPkg));

        Data.Add('board=chip');
        nChip := DumpBoard('chip.PcbDoc', Data);
        Say('chip_records=' + IntToStr(nChip));

        Data.SaveToFile(Dir + 'copper.txt');
        Say('written=' + Dir + 'copper.txt');
        Say('ok=1');
    Except
        Say('ok=0');
        Say('error=exception while dumping copper');
    End;
    Say('stage=end');
End;

End.
