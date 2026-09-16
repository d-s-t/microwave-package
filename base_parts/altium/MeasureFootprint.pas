{ Measure a connector footprint in a PCB library and write it out.

  This exists so that swapping to another manufacturer's SMP is a new
  library and a footprint name, rather than an afternoon reading numbers
  off a vendor drawing and retyping them into Python.

  A PcbLib is a compiled compound document, so Python cannot read one --
  only Altium can. What crosses is this file's output: every pad, every
  copper shape and the 3D body, in the footprint's own frame, which
  eigen_mode_analyze/smp.py then turns into the handful of values the
  board around the launch is drawn from. Measured once when the library
  changes, committed, and read from then on; nothing in a sweep runs it.

  Coordinates are millimetres RELATIVE TO THE FOOTPRINT ORIGIN, and in
  the library's own orientation -- which way the centre pin points is
  not this script's business to decide. smp.py works that out from which
  pad is pin 1, because that is the one thing every coax launch agrees
  on and no vendor frame convention does.

  Record formats, one per line:

      pad|name|layer|x|y|xsize|ysize|rotation|hole
      copper|x1,y1,x2,y2,...
      body|identifier|height

  Sizes are the pad's own, in the PAD's frame -- which is how Altium
  holds them, hence the rotation beside them. See ExportCopper.pas,
  which learned the same lesson the expensive way.

  Reads smp_library, smp_footprint and smp_measured from params.txt.

  Note: Pascal block comments do not nest.
}

Const
    ParamPath = 'C:\Users\david\Documents\uni\lab\microwave-package\base_parts\altium\params.txt';
    OutPath   = 'C:\Users\david\Documents\uni\lab\microwave-package\base_parts\altium\measure_report.txt';

Var
    Rpt : TStringList;

Procedure Say(S : String);
Begin
    Rpt.Add(S);
    Rpt.SaveToFile(OutPath);
End;

Function MM(C : TCoord) : String;
Begin
    Result := FloatToStr(CoordToMMs(C));
End;

{ eMultiLayer matters as much as the two faces: a THROUGH-HOLE pad
  lives there, not on a layer. Accepting only top and bottom measures a
  through-hole connector as having no pads at all -- which is how a
  sibling part in this very library came back with an empty land
  pattern and a clean bill of health. What the board can actually build
  is smp.derive's business; this script's job is to miss nothing. }
Function IsCopper(L : TLayer) : Boolean;
Begin
    Result := (L = eTopLayer) Or (L = eBottomLayer) Or (L = eMultiLayer);
End;

Function LayerTag(L : TLayer) : String;
Begin
    If L = eTopLayer Then Result := 'top'
    Else If L = eBottomLayer Then Result := 'bottom'
    Else If L = eMultiLayer Then Result := 'multi'
    Else Result := 'other';
End;

Procedure MeasureFootprint;
Var
    P       : TStringList;
    Data    : TStringList;
    Doc     : IServerDocument;
    Lib     : IPCB_Library;
    LibIter : IPCB_LibraryIterator;
    GIter   : IPCB_GroupIterator;
    LibComp : IPCB_LibComponent;
    Found   : IPCB_LibComponent;
    Prim    : IPCB_Primitive;
    Pad     : IPCB_Pad;
    LibPath, Pattern, DataPath, Line : String;
    nPad, nCopper, nBody, i : Integer;
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

    LibPath  := P.Values['smp_library'];
    Pattern  := P.Values['smp_footprint'];
    DataPath := P.Values['smp_measured'];
    If (LibPath = '') Or (Pattern = '') Or (DataPath = '') Then
    Begin
        Say('ok=0');
        Say('error=smp_library, smp_footprint and smp_measured must all be set');
        Say('stage=end');
        Exit;
    End;
    Say('library=' + LibPath);
    Say('footprint=' + Pattern);

    Say('trying=open the library');
    Doc := Client.OpenDocument('PCBLIB', LibPath);
    If Doc = Nil Then
    Begin
        Say('ok=0');
        Say('error=could not open the library');
        Say('stage=end');
        Exit;
    End;
    Client.ShowDocument(Doc);

    Lib := Nil;
    Try
        Lib := PCBServer.GetCurrentPCBLibrary;
    Except
    End;
    If Lib = Nil Then
    Begin
        Say('ok=0');
        Say('error=no current PCB library after opening it');
        Say('stage=end');
        Exit;
    End;

    LibIter := Nil;
    Try
        LibIter := Lib.LibraryIterator_Create;
    Except
    End;
    If LibIter = Nil Then
    Begin
        Say('ok=0');
        Say('error=no library iterator');
        Say('stage=end');
        Exit;
    End;
    Try
        LibIter.SetState_FilterAll;
    Except
    End;

    Found := Nil;
    Data := TStringList.Create;
    Data.Add('# ' + Pattern + ', measured from ' + LibPath);
    Data.Add('# by base_parts/altium/MeasureFootprint.pas -- do not edit by hand.');
    Data.Add('# Millimetres, relative to the footprint origin, in the library');
    Data.Add('# orientation. Sizes are in the PAD frame; see the rotation.');

    LibComp := LibIter.FirstPCBObject;
    While LibComp <> Nil Do
    Begin
        If LibComp.Name = Pattern Then Found := LibComp;
        LibComp := LibIter.NextPCBObject;
    End;

    If Found = Nil Then
    Begin
        Try
            Lib.LibraryIterator_Destroy(LibIter);
        Except
        End;
        Say('ok=0');
        Say('error=' + Pattern + ' is not in this library');
        Say('stage=end');
        Exit;
    End;

    Data.Add('footprint=' + Pattern);

    nPad := 0; nCopper := 0; nBody := 0;
    GIter := Nil;
    Try
        GIter := Found.GroupIterator_Create;
    Except
    End;
    If GIter = Nil Then
    Begin
        Say('ok=0');
        Say('error=could not iterate the footprint');
        Say('stage=end');
        Exit;
    End;

    Prim := GIter.FirstPCBObject;
    While Prim <> Nil Do
    Begin
        Case Prim.ObjectId Of
            ePadObject :
                Begin
                    Pad := Prim;
                    If IsCopper(Pad.Layer) Then
                    Begin
                        nPad := nPad + 1;
                        Data.Add('pad|' + Pad.Name + '|' + LayerTag(Pad.Layer) + '|' +
                                 MM(Pad.X - Found.X) + '|' + MM(Pad.Y - Found.Y) + '|' +
                                 MM(Pad.TopXSize) + '|' + MM(Pad.TopYSize) + '|' +
                                 FloatToStr(Pad.Rotation) + '|' + MM(Pad.HoleSize));
                    End;
                End;
            eRegionObject :
                Begin
                    { The ground land, with the anti-pad cut out of it.
                      Every copper region is written, not the first one:
                      a footprint with two is a footprint this board
                      does not understand, and smp.py should say so
                      rather than quietly model half of it. }
                    If IsCopper(Prim.Layer) And (Prim.GetMainContour <> Nil) Then
                    Begin
                        nCopper := nCopper + 1;
                        Line := 'copper|';
                        For i := 0 To Prim.GetMainContour.Count - 1 Do
                        Begin
                            If i > 0 Then Line := Line + ',';
                            Line := Line +
                                MM(Prim.GetMainContour.x(i) - Found.X) + ',' +
                                MM(Prim.GetMainContour.y(i) - Found.Y);
                        End;
                        Data.Add(Line);
                    End;
                End;
            eComponentBodyObject :
                Begin
                    nBody := nBody + 1;
                    Line := 'body|';
                    Try
                        Line := Line + Prim.Identifier;
                    Except
                    End;
                    Line := Line + '|';
                    Try
                        Line := Line + MM(Prim.OverallHeight);
                    Except
                        Line := Line + '0';
                    End;
                    Data.Add(Line);
                End;
        End;
        Prim := GIter.NextPCBObject;
    End;

    Try
        Found.GroupIterator_Destroy(GIter);
    Except
    End;
    Try
        Lib.LibraryIterator_Destroy(LibIter);
    Except
    End;

    Say('pads=' + IntToStr(nPad));
    Say('copper_regions=' + IntToStr(nCopper));
    Say('bodies=' + IntToStr(nBody));

    Try
        Data.SaveToFile(DataPath);
        Say('written=' + DataPath);
    Except
        Say('ok=0');
        Say('error=could not write ' + DataPath);
        Say('stage=end');
        Exit;
    End;

    Try
        Client.CloseDocument(Doc);
    Except
    End;

    Say('ok=1');
    Say('stage=end');
End;

End.
