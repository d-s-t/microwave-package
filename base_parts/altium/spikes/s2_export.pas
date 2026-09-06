{ Spike S2 part 3: export the board to Parasolid, with copper.

  Two things were settled the hard way before this script worked.

  1. How the exporter is obtained:

         Exporter := PCBServer.Board3DModelExporter;

     It is a no-argument PROPERTY on PCBServer. Eight spellings were
     tried, one per script, because DelphiScript takes the whole script
     down on an unknown identifier and candidates therefore cannot share
     a file. PCBServer.CreateBoard3DModelExporter, the Board-side
     spellings and the bare global all fail.

  2. ORDER MATTERS. On a cold start, with no PCB document open, the PCB
     server is not loaded and PCBServer is not even a declared
     identifier -- the script dies on "Undeclared identifier:
     GetCurrentPCBBoard" before any Try/Except can catch it. Opening the
     document through Client FIRST is what registers the server. A warm
     session hides this bug completely, because a document is already
     open.

  The option enums come from EditScript.dll's own type registry:
      TBoard3DModelExportFormat           _Step | _Parasolid
      TBoard3DModelExportCopperOptions    _CopperNone | _CopperOnly |
                                          _SelectedCoppers |
                                          _CopperLayer | _CopperAll
      TBoard3DModelExportHolesOptions     _AllHoles | _SelectedHoles
      TBoard3DModelExportComponentOptions _AllComponents | _SelectedComponents
      TBoard3DModelExportModelsOptions    _AsStep | _AsExtruded | _AsBoth

  _CopperLayer is the one the pipeline needs: copper split per layer is
  what lets each body arrive under its own name, which is what lets HFSS
  give it its own material.

  Note for future edits: Pascal block comments do NOT nest. An inner
  closing brace ends this comment early and everything after it is
  parsed as code -- which is exactly how an earlier version of this file
  produced "Error in declaration block at Line:9".

  Run:
    X2.EXE '-RScriptingSystem:RunScriptFile(FileName="<this file>"|ProcName="S2Export")'
}

Const
    OutPath = 'C:\Users\david\Documents\uni\lab\microwave-package\base_parts\altium\spikes\s2_export.txt';
    XtPath  = 'C:\Users\david\Documents\uni\lab\microwave-package\base_parts\altium\spikes\s2_out.x_t';
    PcbPath = 'C:\Users\Public\Documents\Altium\david_coplanar\PCB1.PcbDoc';

Procedure S2Export;
Var
    B : IPCB_Board;
    D : IServerDocument;
    E : IPCB_Board3DModelExporter;
    L : TStringList;
Begin
    L := TStringList.Create;
    L.Add('stage=start');
    L.SaveToFile(OutPath);

    { Open the document BEFORE any mention of PCBServer -- see note 2. }
    D := Client.OpenDocument('PCB', PcbPath);
    If D = Nil Then
    Begin
        L.Add('open=FAIL'); L.Add('stage=end'); L.SaveToFile(OutPath); L.Free; Exit;
    End;
    Client.ShowDocument(D);
    L.Add('open=ok');
    L.SaveToFile(OutPath);

    B := PCBServer.GetCurrentPCBBoard;
    If B = Nil Then
    Begin
        L.Add('board=nil'); L.Add('stage=end'); L.SaveToFile(OutPath); L.Free; Exit;
    End;
    L.Add('board=' + B.FileName);

    E := PCBServer.Board3DModelExporter;
    If E = Nil Then
    Begin
        L.Add('exporter=nil'); L.Add('stage=end'); L.SaveToFile(OutPath); L.Free; Exit;
    End;
    L.Add('exporter=ok');
    L.SaveToFile(OutPath);

    { Each option set on its own, so a wrong spelling names itself
      instead of hiding the ones after it. }
    Try E.Format := eBoard3DModelExportFormat_Parasolid; L.Add('set_Format=ok'); Except L.Add('set_Format=FAIL'); End;
    Try E.CopperOptions := eBoard3DModelExportCopperOptions_CopperLayer; L.Add('set_Copper=ok'); Except L.Add('set_Copper=FAIL'); End;
    Try E.HolesOptions := eBoard3DModelExportHolesOptions_AllHoles; L.Add('set_Holes=ok'); Except L.Add('set_Holes=FAIL'); End;
    Try E.ComponentOptions := eBoard3DModelExportComponentOptions_AllComponents; L.Add('set_Comp=ok'); Except L.Add('set_Comp=FAIL'); End;
    Try E.ModelsOptions := eBoard3DModelExportModelsOptions_AsBoth; L.Add('set_Models=ok'); Except L.Add('set_Models=FAIL'); End;
    Try E.FileName := XtPath; L.Add('set_FileName=ok'); Except L.Add('set_FileName=FAIL'); End;
    L.SaveToFile(OutPath);

    Try
        E.ExportBoard3DModel(B);
        L.Add('export=ok');
    Except
        L.Add('export=FAIL');
    End;

    L.Add('stage=end');
    L.SaveToFile(OutPath);
    L.Free;
End;

End.
