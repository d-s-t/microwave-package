{ Spike S3: can a vendor footprint be read out of a PcbLib from a
  script, and does it agree with the KiCad numbers already in
  eigen_mode_analyze/smp.py?

  Two questions, because either answer alone is not enough to act on.
  If the library cannot be read from a script there is nothing to place.
  If it can, but the vendor's Altium land pattern differs from the
  KiCad one, then placing it MOVES THE LAUNCH -- and everything
  measured from the launch moves with it: the ground-plane anti-pad,
  the ground fill behind the strap, the via fence, the bond fingers,
  and the connector's 3D placement over in HFSS.

  Read-only by construction. It opens the library, reports what is in
  it, and closes it again. It never modifies and never saves, because
  it runs inside whatever Altium session the operator has open.

  Method notes, which are the whole reason this is a spike and not a
  first draft of the real thing:

  * Every risky call is announced to the report BEFORE it is made, so a
    script that dies takes its last line with it as the diagnostic.

  * Only identifiers already proved to exist are named. IPCB_Library,
    IPCB_LibraryIterator, IPCB_LibComponent, IPCB_GroupIterator,
    eComponentBodyObject, eTopPaste, eTopSolder and the rest were
    confirmed present in AD26's own DLLs first. An identifier that does
    not exist is a parse error, which is a modal dialog no Try can
    catch. Method names resolve at run time and DO land in the Except,
    so those are probed freely.

  Run:
    X2.EXE -RScriptingSystem:RunScriptFile(FileName="<this file>"|ProcName="S3LibComp")

  Note: Pascal block comments do not nest.
}

Const
    LibPath = 'C:\Users\david\Documents\uni\lab\microwave-package\base_parts\altium\vendor\Altium_WR-SMP (rev22a).PcbLib';
    Pattern = '60124102122403';
    OutPath = 'C:\Users\david\Documents\uni\lab\microwave-package\base_parts\altium\spikes\s3_libcomp.txt';

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

{ Only layers whose identifiers are confirmed. Anything else reports as
  'other' rather than being guessed at. }
Function LayerTag(L : TLayer) : String;
Begin
    If L = eTopLayer Then Result := 'top'
    Else If L = eBottomLayer Then Result := 'bottom'
    Else If L = eMultiLayer Then Result := 'multi'
    Else If L = eTopPaste Then Result := 'toppaste'
    Else If L = eTopSolder Then Result := 'topsolder'
    Else If L = eTopOverlay Then Result := 'topoverlay'
    Else If L = eKeepOutLayer Then Result := 'keepout'
    Else Result := 'other';
End;

Procedure S3LibComp;
Var
    Doc     : IServerDocument;
    Lib     : IPCB_Library;
    LibIter : IPCB_LibraryIterator;
    GIter   : IPCB_GroupIterator;
    LibComp : IPCB_LibComponent;
    Found   : IPCB_LibComponent;
    Prim    : IPCB_Primitive;
    Pad     : IPCB_Pad;
    Line    : String;
    i       : Integer;
    nComp, nPrim, nPad, nBody : Integer;
Begin
    Rpt := TStringList.Create;
    Rpt.Add('stage=start');
    Rpt.SaveToFile(OutPath);

    Say('trying=OpenDocument PCBLIB');
    Doc := Client.OpenDocument('PCBLIB', LibPath);
    If Doc = Nil Then
    Begin
        Say('opened_as_PCBLIB=0');
        Say('trying=OpenDocument PcbLib');
        Doc := Client.OpenDocument('PcbLib', LibPath);
    End;
    If Doc = Nil Then
    Begin
        Say('ok=0');
        Say('error=could not open the library document');
        Say('stage=end');
        Exit;
    End;
    Say('opened=ok');

    Client.ShowDocument(Doc);

    Say('trying=GetCurrentPCBLibrary');
    Lib := Nil;
    Try
        Lib := PCBServer.GetCurrentPCBLibrary;
    Except
        Say('GetCurrentPCBLibrary=RAISED');
    End;
    If Lib = Nil Then
    Begin
        Say('ok=0');
        Say('error=no current PCB library after opening it');
        Say('stage=end');
        Exit;
    End;
    Say('library=ok');

    Say('trying=LibraryIterator_Create');
    LibIter := Nil;
    Try
        LibIter := Lib.LibraryIterator_Create;
    Except
        Say('LibraryIterator_Create=RAISED');
    End;
    If LibIter = Nil Then
    Begin
        Say('ok=0');
        Say('error=no library iterator');
        Say('stage=end');
        Exit;
    End;

    { Some builds want the filter set before iterating and some do not.
      Either way this must not be fatal. }
    Try
        LibIter.SetState_FilterAll;
        Say('SetState_FilterAll=ok');
    Except
        Say('SetState_FilterAll=unavailable');
    End;

    nComp := 0;
    Found := Nil;
    Say('trying=iterate footprints');
    LibComp := LibIter.FirstPCBObject;
    While LibComp <> Nil Do
    Begin
        nComp := nComp + 1;
        Say('footprint_' + IntToStr(nComp) + '=' + LibComp.Name);
        If LibComp.Name = Pattern Then Found := LibComp;
        LibComp := LibIter.NextPCBObject;
    End;
    Say('footprints=' + IntToStr(nComp));

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
    Say('found=' + Pattern);

    { Where the footprint's own origin sits, so the pad coordinates
      below read relative to it -- which is the frame smp.py is in. }
    Try
        Say('origin_x_mm=' + MM(Found.X));
        Say('origin_y_mm=' + MM(Found.Y));
    Except
        Say('origin=unreadable');
    End;

    Say('trying=GroupIterator_Create');
    GIter := Nil;
    Try
        GIter := Found.GroupIterator_Create;
    Except
        Say('GroupIterator_Create=RAISED');
    End;

    If GIter <> Nil Then
    Begin
        Try
            GIter.AddFilter_ObjectSet(AllObjects);
        Except
        End;

        nPrim := 0; nPad := 0; nBody := 0;
        Prim := GIter.FirstPCBObject;
        While Prim <> Nil Do
        Begin
            nPrim := nPrim + 1;
            Case Prim.ObjectId Of
                ePadObject :
                    Begin
                        nPad := nPad + 1;
                        Pad := Prim;
                        Say('pad_' + IntToStr(nPad) + '=' + Pad.Name +
                            '|' + LayerTag(Pad.Layer) +
                            '|' + MM(Pad.X - Found.X) +
                            '|' + MM(Pad.Y - Found.Y) +
                            '|' + MM(Pad.TopXSize) +
                            '|' + MM(Pad.TopYSize) +
                            '|hole=' + MM(Pad.HoleSize));
                    End;
                eComponentBodyObject :
                    Begin
                        nBody := nBody + 1;
                        Say('body_' + IntToStr(nBody) + '_layer=' + LayerTag(Prim.Layer));
                        Try
                            Say('body_' + IntToStr(nBody) + '_identifier=' + Prim.Identifier);
                        Except
                        End;
                        Try
                            Say('body_' + IntToStr(nBody) + '_height_mm=' + MM(Prim.OverallHeight));
                        Except
                        End;
                    End;
                eTrackObject  : Say('track_layer=' + LayerTag(Prim.Layer));
                eArcObject    : Say('arc_layer=' + LayerTag(Prim.Layer));
                eFillObject   : Say('fill_layer=' + LayerTag(Prim.Layer));
                eRegionObject :
                    Begin
                        { The ground land with the anti-pad cut out of
                          it, if this footprint carries one -- the same
                          shape smp.py reads from the KiCad fp_poly.
                          Reported as its extent and vertex count rather
                          than 100 points of arc. }
                        Say('region_layer=' + LayerTag(Prim.Layer));
                        Try
                            Say('region_points=' + IntToStr(Prim.GetMainContour.Count));
                            Say('region_bounds_mm=' +
                                MM(Prim.BoundingRectangle.Left - Found.X) + '|' +
                                MM(Prim.BoundingRectangle.Bottom - Found.Y) + '|' +
                                MM(Prim.BoundingRectangle.Right - Found.X) + '|' +
                                MM(Prim.BoundingRectangle.Top - Found.Y));
                            { Every vertex, so the land size, the
                              anti-pad radius and the escape slot can be
                              measured off the vendor's own shape rather
                              than assumed to match the KiCad one. }
                            Line := '';
                            For i := 0 To Prim.GetMainContour.Count - 1 Do
                            Begin
                                If i > 0 Then Line := Line + ',';
                                Line := Line + MM(Prim.GetMainContour.x(i) - Found.X) +
                                        ',' + MM(Prim.GetMainContour.y(i) - Found.Y);
                            End;
                            Say('region_contour_mm=' + Line);
                        Except
                            Say('region_shape=unreadable');
                        End;
                    End;
                eTextObject   : Say('text_layer=' + LayerTag(Prim.Layer));
            Else
                Say('other_primitive_layer=' + LayerTag(Prim.Layer));
            End;
            Prim := GIter.NextPCBObject;
        End;
        Say('primitives=' + IntToStr(nPrim));
        Say('pads=' + IntToStr(nPad));
        Say('bodies=' + IntToStr(nBody));

        Try
            Found.GroupIterator_Destroy(GIter);
        Except
        End;
    End;

    Try
        Lib.LibraryIterator_Destroy(LibIter);
    Except
    End;

    { Put the session back the way it was found. }
    Try
        Client.CloseDocument(Doc);
        Say('closed=ok');
    Except
        Say('closed=FAIL');
    End;

    Say('ok=1');
    Say('stage=end');
End;

End.
