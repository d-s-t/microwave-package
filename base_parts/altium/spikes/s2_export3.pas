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
    OutPath = 'C:\Users\david\Documents\uni\lab\microwave-package\base_parts\altium\spikes\s2_export3.txt';
    XtPath  = 'C:\Users\david\Documents\uni\lab\microwave-package\base_parts\altium\spikes\s2_out.x_t';
    PcbPath = 'C:\Users\david\AppData\Local\Temp\claude\C--Users-david-Documents-uni-lab-microwave-package\e18edbbb-5947-4821-80d7-c1ce1b4831de\scratchpad\altium_fixture\PCB1.PcbDoc';


Procedure S2Export3;
Var
    B : IPCB_Board;
    D : IServerDocument;
    E : IPCB_Board3DModelExporter;
    CL : IPCB_CopperBodyList;
    L : TStringList;
Begin
    L := TStringList.Create;
    L.Add('stage=start');
    L.SaveToFile(OutPath);

    D := Client.OpenDocument('PCB', PcbPath);
    If D = Nil Then Begin L.Add('open=FAIL'); L.SaveToFile(OutPath); L.Free; Exit; End;
    Client.ShowDocument(D);
    B := PCBServer.GetCurrentPCBBoard;
    If B = Nil Then Begin L.Add('board=nil'); L.SaveToFile(OutPath); L.Free; Exit; End;
    E := PCBServer.Board3DModelExporter;
    If E = Nil Then Begin L.Add('exporter=nil'); L.SaveToFile(OutPath); L.Free; Exit; End;

    Try E.SetState_ExportCopperOption(eBoard3DModelExportCopperOptions_CopperAll); L.Add('CopperOption=ok'); Except L.Add('CopperOption=FAIL'); End;
    Try E.SetState_ExportHolesOption(eBoard3DModelExportHolesOptions_AllHoles); L.Add('HolesOption=ok'); Except L.Add('HolesOption=FAIL'); End;
    Try E.SetState_ExportComponentOptions(eBoard3DModelExportComponentOptions_AllComponents); L.Add('ComponentOptions=ok'); Except L.Add('ComponentOptions=FAIL'); End;
    L.SaveToFile(OutPath);

    L.Add('trying=D1_B_File_Fmt');
    L.SaveToFile(OutPath);
    Try
        E.ExportBoard3DModel(B, XtPath, eBoard3DModelExportFormat_Parasolid);
        L.Add('D1_B_File_Fmt=OK');
    Except
        L.Add('D1_B_File_Fmt=fail');
    End;
    L.Add('trying=D2_B_File');
    L.SaveToFile(OutPath);
    Try
        E.ExportBoard3DModel(B, XtPath);
        L.Add('D2_B_File=OK');
    Except
        L.Add('D2_B_File=fail');
    End;
    L.Add('trying=D3_noargs');
    L.SaveToFile(OutPath);
    Try
        E.ExportBoard3DModel;
        L.Add('D3_noargs=OK');
    Except
        L.Add('D3_noargs=fail');
    End;
    L.Add('trying=D4_Fmt_File_B');
    L.SaveToFile(OutPath);
    Try
        E.ExportBoard3DModel(eBoard3DModelExportFormat_Parasolid, XtPath, B);
        L.Add('D4_Fmt_File_B=OK');
    Except
        L.Add('D4_Fmt_File_B=fail');
    End;
    L.Add('trying=D5_File_Fmt_T');
    L.SaveToFile(OutPath);
    Try
        E.ExportBoard3DModel(XtPath, eBoard3DModelExportFormat_Parasolid, True);
        L.Add('D5_File_Fmt_T=OK');
    Except
        L.Add('D5_File_Fmt_T=fail');
    End;
    L.Add('trying=D6_B_Fmt_File');
    L.SaveToFile(OutPath);
    Try
        E.ExportBoard3DModel(B, eBoard3DModelExportFormat_Parasolid, XtPath);
        L.Add('D6_B_Fmt_File=OK');
    Except
        L.Add('D6_B_Fmt_File=fail');
    End;
    L.Add('trying=BOARD_file_fmt');
    L.SaveToFile(OutPath);
    Try
        B.ExportBoard3DModel(XtPath, eBoard3DModelExportFormat_Parasolid);
        L.Add('BOARD_file_fmt=OK');
    Except
        L.Add('BOARD_file_fmt=fail');
    End;
    L.Add('trying=BOARD_file');
    L.SaveToFile(OutPath);
    Try
        B.ExportBoard3DModel(XtPath);
        L.Add('BOARD_file=OK');
    Except
        L.Add('BOARD_file=fail');
    End;

    Try
        CL := E.GetCopperBodyList;
        If CL = Nil Then L.Add('CopperBodyList=nil') Else L.Add('CopperBodyList=GOT');
    Except
        L.Add('CopperBodyList=fail');
    End;

    L.Add('stage=end');
    L.SaveToFile(OutPath);
    L.Free;
End;

End.

{ ---------------------------------------------------------------------
  FINDINGS (2026-09-02, Altium AD26)

  PARTIAL. The exporter binds and configures, but the call that performs
  the export could not be reached by probing.

  What works:
      E := PCBServer.Board3DModelExporter        returns a live interface
      E.SetState_ExportCopperOption(...)         ok
      E.SetState_ExportHolesOption(...)          ok
      E.SetState_ExportComponentOptions(...)     ok
      E.SetState_ExportModelsOption(...)         ok
  So copper export IS configurable from a script, including
  eBoard3DModelExportCopperOptions_CopperLayer, which is the per-layer
  split the body-naming contract depends on.

  What does not:
      ExportBoard3DModel  -- eight arities tried (board/filename/format
                             in every plausible order, plus no-arg, plus
                             the same spellings on IPCB_Board). All raise.
      GetCopperBodyList   -- raises too.

  The pattern is suggestive: the SetState_ procedures, which take a
  single enum and return nothing, bind fine, while every member that
  returns an interface or performs the export does not. That looks like
  the scripting engine exposing only part of the interface rather than
  eight wrong guesses in a row, so more guessing is poor value.

  Two routes remain, and they differ enough to be worth a decision
  rather than a coin toss:

    * OutJob. Run a preconfigured output batch through
      WorkSpaceManager:GenerateReport with ObjectKind='OutputBatch' --
      the batch mechanism Altium's own CI guidance uses. Needs an OutJob
      configured once, then it is just a process call.

    * Skip the CAD round trip for copper entirely. Enumerating every
      primitive already works (the probe counted 262 vias), so the
      geometry can be written out as data and rebuilt directly in HFSS
      by PyAEDT, which already builds the model. That gives exact body
      names and materials for free and removes the imported-face
      fragility, at the cost of rendering pours ourselves.

  No dialogs in any of this: wrong arities raise catchable exceptions.
  The dialogs only came from parse errors -- a nested block comment, and
  calling PCBServer before any document was open.
  --------------------------------------------------------------------- }
