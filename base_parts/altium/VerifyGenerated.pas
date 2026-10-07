{ Open the generated boards and report what is actually inside them.

  Written because the generator reported success while the saved
  document appeared empty. The generator measures the board it holds in
  memory; this measures the files on disk, which is the only thing that
  matters to anyone opening them afterwards.

  That first "empty" document turned out to be neither empty nor broken:
  the boards are centred on the origin, so they sit at negative
  coordinates and a freshly opened document shows blank space until the
  view is fitted.

  Note: Pascal block comments do not nest.
}

Const
    Dir     = '<ALTIUM_DIR>\generated\';
    OutPath = '<ALTIUM_DIR>\verify_report.txt';

Var
    Rpt : TStringList;

Procedure Say(S : String);
Begin
    Rpt.Add(S);
    Rpt.SaveToFile(OutPath);
End;

Procedure CheckOne(FileName : String; Tag : String);
Var
    Doc   : IServerDocument;
    Board : IPCB_Board;
    It    : IPCB_BoardIterator;
    Prim  : IPCB_Primitive;
    nTrack, nVia, nPad, nRegion, nOther : Integer;
Begin
    { Close any copy already open before reading from disk. Without
      this, Altium hands back the document it already has in memory and
      GetCurrentPCBBoard may return a stale one from an earlier run --
      which is exactly how a correct 62-via board once read back as the
      previous run's single track. }
    Try
        Doc := Client.GetDocumentByPath(Dir + FileName);
        If Doc <> Nil Then Client.CloseDocument(Doc);
    Except
    End;

    Doc := Client.OpenDocument('PCB', Dir + FileName);
    If Doc = Nil Then
    Begin
        Say(Tag + '=COULD_NOT_OPEN');
        Exit;
    End;
    Client.ShowDocument(Doc);
    Board := PCBServer.GetCurrentPCBBoard;
    If Board = Nil Then
    Begin
        Say(Tag + '=NO_BOARD');
        Exit;
    End;

    Try
        Say(Tag + '_width_mm='  + FloatToStr(CoordToMMs(Board.BoardOutline.BoundingRectangle.Right - Board.BoardOutline.BoundingRectangle.Left)));
        Say(Tag + '_height_mm=' + FloatToStr(CoordToMMs(Board.BoardOutline.BoundingRectangle.Top - Board.BoardOutline.BoundingRectangle.Bottom)));
    Except
        Say(Tag + '_outline=unreadable');
    End;

    nTrack := 0; nVia := 0; nPad := 0; nRegion := 0; nOther := 0;
    It := Board.BoardIterator_Create;
    It.AddFilter_ObjectSet(AllObjects);
    It.AddFilter_LayerSet(AllLayers);
    It.AddFilter_Method(eProcessAll);
    Prim := It.FirstPCBObject;
    While Prim <> Nil Do
    Begin
        Case Prim.ObjectId Of
            eTrackObject  : nTrack  := nTrack + 1;
            eViaObject    : nVia    := nVia + 1;
            ePadObject    : nPad    := nPad + 1;
            eRegionObject : nRegion := nRegion + 1;
        Else
            nOther := nOther + 1;
        End;
        Prim := It.NextPCBObject;
    End;
    Board.BoardIterator_Destroy(It);

    Say(Tag + '_tracks='  + IntToStr(nTrack));
    Say(Tag + '_vias='    + IntToStr(nVia));
    Say(Tag + '_pads='    + IntToStr(nPad));
    Say(Tag + '_regions=' + IntToStr(nRegion));
End;

Procedure VerifyGenerated;
Begin
    Rpt := TStringList.Create;
    Rpt.Add('stage=start');
    Rpt.SaveToFile(OutPath);

    CheckOne('package.PcbDoc', 'package');
    CheckOne('chip.PcbDoc', 'chip');

    Say('ok=1');
    Say('stage=end');
End;

End.
