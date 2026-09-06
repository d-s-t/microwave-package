{ Spike S2, part 1: is the PCB API reachable from an externally-launched
  script, and what does the existing board actually contain?

  Read-only by construction. It opens the document if it is not already
  open and reports what it finds; it never modifies and never saves,
  because it runs inside whatever Altium session the operator has open
  (see the findings in s1_launch.pas).

  Run:
    X2.EXE '-RScriptingSystem:RunScriptFile(FileName="<this file>"|ProcName="S2Probe")'
}

Const
    PcbPath = 'C:\Users\david\Documents\uni\lab\tmp\altium\PCB1.PcbDoc';
    OutPath = 'C:\Users\david\Documents\uni\lab\microwave-package\base_parts\altium\spikes\s2_probe.txt';

Procedure S2Probe;
Var
    Board      : IPCB_Board;
    Rpt        : TStringList;
    Iterator   : IPCB_BoardIterator;
    Prim       : IPCB_Primitive;
    Doc        : IServerDocument;
    LayerStack : IPCB_LayerStack;
    LayerObj   : IPCB_LayerObject;
    nTrack, nVia, nPad, nArc, nRegion, nComp, nOther : Integer;
    i          : Integer;
Begin
    Rpt := TStringList.Create;
    Try
        Try
            Board := PCBServer.GetCurrentPCBBoard;
            If Board = Nil Then
            Begin
                Rpt.Add('note=no current board, opening ' + PcbPath);
                Doc := Client.OpenDocument('PCB', PcbPath);
                If Doc <> Nil Then Client.ShowDocument(Doc);
                Board := PCBServer.GetCurrentPCBBoard;
            End;

            If Board = Nil Then
            Begin
                Rpt.Add('ok=0');
                Rpt.Add('error=could not obtain an IPCB_Board');
            End
            Else
            Begin
                Rpt.Add('ok=1');
                Rpt.Add('board=' + Board.FileName);

                { Board outline extent, in Altium internal units. }
                Rpt.Add('bound_left='   + IntToStr(Board.BoundingRectangle.Left));
                Rpt.Add('bound_right='  + IntToStr(Board.BoundingRectangle.Right));
                Rpt.Add('bound_bottom=' + IntToStr(Board.BoundingRectangle.Bottom));
                Rpt.Add('bound_top='    + IntToStr(Board.BoundingRectangle.Top));

                { What the layer stack looks like -- the CPW needs to know
                  which signal layers exist and how thick the dielectric is.
                  Guarded on its own: AD26 has more than one layer-stack
                  interface generation, and a miss here must not cost us the
                  primitive census below. }
                Try
                    LayerStack := Board.LayerStack_V7;
                    If LayerStack = Nil Then LayerStack := Board.LayerStack;
                    If LayerStack = Nil Then
                        Rpt.Add('layerstack=nil')
                    Else
                    Begin
                        i := 0;
                        LayerObj := LayerStack.FirstLayer;
                        While (LayerObj <> Nil) And (i < 40) Do
                        Begin
                            Rpt.Add('layer[' + IntToStr(i) + ']=' + LayerObj.Name);
                            LayerObj := LayerStack.NextLayer(LayerObj);
                            i := i + 1;
                        End;
                        Rpt.Add('layer_count=' + IntToStr(i));
                    End;
                Except
                    Rpt.Add('layerstack=EXCEPTION');
                End;

                { Primitive census: tells us whether there is real copper
                  here to export, or an empty board. }
                nTrack := 0; nVia := 0; nPad := 0; nArc := 0;
                nRegion := 0; nComp := 0; nOther := 0;
                Try
                Iterator := Board.BoardIterator_Create;
                Iterator.AddFilter_ObjectSet(AllObjects);
                Iterator.AddFilter_LayerSet(AllLayers);
                Iterator.AddFilter_Method(eProcessAll);
                Prim := Iterator.FirstPCBObject;
                While Prim <> Nil Do
                Begin
                    Case Prim.ObjectId Of
                        eTrackObject     : nTrack  := nTrack + 1;
                        eViaObject       : nVia    := nVia + 1;
                        ePadObject       : nPad    := nPad + 1;
                        eArcObject       : nArc    := nArc + 1;
                        eRegionObject    : nRegion := nRegion + 1;
                        eComponentObject : nComp   := nComp + 1;
                    Else
                        nOther := nOther + 1;
                    End;
                    Prim := Iterator.NextPCBObject;
                End;
                Board.BoardIterator_Destroy(Iterator);

                Rpt.Add('tracks='     + IntToStr(nTrack));
                Rpt.Add('vias='       + IntToStr(nVia));
                Rpt.Add('pads='       + IntToStr(nPad));
                Rpt.Add('arcs='       + IntToStr(nArc));
                Rpt.Add('regions='    + IntToStr(nRegion));
                Rpt.Add('components=' + IntToStr(nComp));
                Rpt.Add('other='      + IntToStr(nOther));
                Except
                    Rpt.Add('census=EXCEPTION');
                End;
            End;
        Except
            Rpt.Add('ok=0');
            Rpt.Add('error=exception raised in probe');
        End;
        Rpt.SaveToFile(OutPath);
    Finally
        Rpt.Free;
    End;
End;

End.

{ ---------------------------------------------------------------------
  FINDINGS (run 2026-09-02, Altium AD26)

  PASSED. The PCB API is fully reachable from an externally-launched
  script -- PCBServer.GetCurrentPCBBoard returns a live board and every
  read below worked, in 3.1 s.

  The existing board (PCB1.PcbDoc) reports:
      36.0 x 14.0 mm, 2 layers (Top, Bottom)
      17 tracks, 262 vias, 4 pads, 4 arcs, 10 regions, 2 components
  So it already carries a CPW, a substantial via fence and both
  connectors -- a real reference for what the generator must reproduce.

  Two API notes for the generator:

  1. Board.LayerStack raised where LayerStack_V7 succeeded. AD26 has more
     than one layer-stack interface generation and the older one is not
     safe to assume; use _V7.

  2. Guarding each section separately was what turned an opaque
     'exception raised in probe' into a located failure. The generator
     should report progress the same way, because a runtime error inside
     Altium produces no dialog, no exit code and no stack -- only
     whatever the script itself managed to write down.
  --------------------------------------------------------------------- }
