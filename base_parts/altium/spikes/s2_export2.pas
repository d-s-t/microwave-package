{ Spike S2 part 4: drive IPCB_Board3DModelExporter with its real members.

  Part 3 got a live exporter but every property assignment failed. The
  reason: this interface has no properties. Its members, read out of
  EditScript.dll's registry, follow Altium's GetState_/SetState_ idiom:

      Clear, AddBoard, RemoveBoard, GetBoard
      SetState_ExportCopperOption      <- CopperNone/Only/Selected/Layer/All
      SetState_ExportCopperLayer       <- which layer, for _CopperLayer
      SetState_ExportHolesOption
      SetState_ExportComponentOptions
      SetState_ExportModelsOption
      SetState_IncludeElectricalPadHoles / MechanicalPadHoles / FreePadHoles
      SetState_ExportPadAndViaBarrelsOnly
      SetState_ComponentSuffixType / ComponentSuffix
      ExportBoard3DModel, ExportBoard3DModelToOCCDoc
      GetCopperBodyList, GetCopperBodyList2

  There is no SetState_Format and no SetState_FileName, so both must be
  arguments of ExportBoard3DModel. Its arity is the last unknown, so the
  three plausible spellings are tried in order.

  The progress line is written and FLUSHED before each attempt, not
  after. A wrong argument count may be fatal rather than catchable, and
  if it is, the file still names the spelling that killed the script --
  which is the only diagnostic Altium leaves behind.

  Remember: Pascal block comments do not nest.
}

Const
    OutPath = 'C:\Users\david\Documents\uni\lab\microwave-package\base_parts\altium\spikes\s2_export2.txt';
    XtPath  = 'C:\Users\david\Documents\uni\lab\microwave-package\base_parts\altium\spikes\s2_out.x_t';
    PcbPath = 'C:\Users\david\AppData\Local\Temp\claude\C--Users-david-Documents-uni-lab-microwave-package\e18edbbb-5947-4821-80d7-c1ce1b4831de\scratchpad\altium_fixture\PCB1.PcbDoc';

Procedure S2Export2;
Var
    B : IPCB_Board;
    D : IServerDocument;
    E : IPCB_Board3DModelExporter;
    L : TStringList;
Begin
    L := TStringList.Create;
    L.Add('stage=start');
    L.SaveToFile(OutPath);

    D := Client.OpenDocument('PCB', PcbPath);
    If D = Nil Then
    Begin
        L.Add('open=FAIL'); L.SaveToFile(OutPath); L.Free; Exit;
    End;
    Client.ShowDocument(D);

    B := PCBServer.GetCurrentPCBBoard;
    If B = Nil Then
    Begin
        L.Add('board=nil'); L.SaveToFile(OutPath); L.Free; Exit;
    End;
    E := PCBServer.Board3DModelExporter;
    If E = Nil Then
    Begin
        L.Add('exporter=nil'); L.SaveToFile(OutPath); L.Free; Exit;
    End;
    L.Add('exporter=ok');
    L.SaveToFile(OutPath);

    Try E.Clear; L.Add('Clear=ok'); Except L.Add('Clear=FAIL'); End;
    Try E.AddBoard(B); L.Add('AddBoard=ok'); Except L.Add('AddBoard=FAIL'); End;
    Try E.SetState_ExportCopperOption(eBoard3DModelExportCopperOptions_CopperAll); L.Add('CopperOption=ok'); Except L.Add('CopperOption=FAIL'); End;
    Try E.SetState_ExportHolesOption(eBoard3DModelExportHolesOptions_AllHoles); L.Add('HolesOption=ok'); Except L.Add('HolesOption=FAIL'); End;
    Try E.SetState_ExportComponentOptions(eBoard3DModelExportComponentOptions_AllComponents); L.Add('ComponentOptions=ok'); Except L.Add('ComponentOptions=FAIL'); End;
    Try E.SetState_ExportModelsOption(eBoard3DModelExportModelsOptions_AsBoth); L.Add('ModelsOption=ok'); Except L.Add('ModelsOption=FAIL'); End;
    L.SaveToFile(OutPath);

    L.Add('trying=A_FileName_Format');
    L.SaveToFile(OutPath);
    Try
        E.ExportBoard3DModel(XtPath, eBoard3DModelExportFormat_Parasolid);
        L.Add('A=ok');
    Except
        L.Add('A=FAIL');
    End;
    L.SaveToFile(OutPath);

    L.Add('trying=B_Format_FileName');
    L.SaveToFile(OutPath);
    Try
        E.ExportBoard3DModel(eBoard3DModelExportFormat_Parasolid, XtPath);
        L.Add('B=ok');
    Except
        L.Add('B=FAIL');
    End;
    L.SaveToFile(OutPath);

    L.Add('trying=C_FileNameOnly');
    L.SaveToFile(OutPath);
    Try
        E.ExportBoard3DModel(XtPath);
        L.Add('C=ok');
    Except
        L.Add('C=FAIL');
    End;

    L.Add('stage=end');
    L.SaveToFile(OutPath);
    L.Free;
End;

End.
