{ Write the fabrication files of one board by running an Output Job.

  Reads manufacturing_params.txt (written by
  microwave_package.design.altium_generation.export_manufacturing):

      project=<abs path>.PrjPcb     the board's project, holding the PcbDoc
                                    and the OutJob
      outjob=<abs path>.OutJob      the job to run; its outputs name their
                                    own folder, so nothing else is passed

  and runs the job. Altium has no scriptable call that writes a Gerber,
  ODB++ or IPC-2581 set without a dialog; an Output Job is what it runs
  unattended. The job is plain text written by Python (design/outjob.py).

  Every step is announced to the report BEFORE it is attempted: a runtime
  error in a script parks it in the debugger with no output, so the last
  line written is the whole diagnostic. See base_parts/altium/README.md.

  Block comments do not nest in this language, so nothing in here may
  contain a closing brace. }

Const
    ParamPath = '<ALTIUM_DIR>\manufacturing_params.txt';
    OutPath   = '<ALTIUM_DIR>\generated\manufacturing_report.txt';

Var
    Rpt : TStringList;

Procedure Say(S : String);
Begin
    Rpt.Add(S);
    Rpt.SaveToFile(OutPath);
End;

Procedure ExportManufacturing;
Var
    P          : TStringList;
    ProjectPath, JobPath : String;
    Doc        : IServerDocument;
    Workspace  : IWorkspace;
    Project    : IProject;
    Kinds      : TStringList;
    i          : Integer;
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
    ProjectPath := P.Values['project'];
    JobPath := P.Values['outjob'];
    Say('project=' + ProjectPath);
    Say('outjob=' + JobPath);

    Say('trying=open the project');
    Try
        Workspace := GetWorkspace;
        If Workspace <> Nil Then
        Begin
            Workspace.DM_OpenProject(ProjectPath, False);
            Say('project_open=ok');
        End
        Else
            Say('project_open=NO_WORKSPACE');
    Except
        Say('project_open=FAIL');
    End;

    { The kind name of an Output Job document is not documented, so each
      likely spelling is tried in turn and the one that works is reported. }
    Say('trying=open the output job');
    Doc := Nil;
    Kinds := TStringList.Create;
    Kinds.Add('OUTPUTJOB');
    Kinds.Add('OUTJOB');
    Kinds.Add('OutJob');
    Kinds.Add('Output Job');
    Kinds.Add('OUTPUT JOB');
    Kinds.Add('');
    For i := 0 To Kinds.Count - 1 Do
    Begin
        If Doc <> Nil Then Break;
        Say('trying_kind=' + Kinds[i]);
        Try
            Doc := Client.OpenDocument(Kinds[i], JobPath);
        Except
            Doc := Nil;
        End;
        If Doc <> Nil Then
        Begin
            Say('outjob_kind=' + Kinds[i]);
            Try
                Client.ShowDocument(Doc);
                Say('outjob_open=ok');
            Except
                Say('outjob_show=FAIL');
            End;
        End;
    End;
    If Doc = Nil Then Say('outjob_open=NIL');

    If Doc <> Nil Then
    Begin
        Say('trying=run the output job');
        Try
            ResetParameters;
            AddStringParameter('Action', 'Run');
            AddStringParameter('ObjectKind', 'OutputBatch');
            RunProcess('WorkspaceManager:GenerateReport');
            Say('outjob_run=ok');
        Except
            Say('outjob_run=FAIL');
        End;
    End;

    Say('trying=close the documents');
    Try
        If Doc <> Nil Then Client.CloseDocument(Doc);
        Doc := Client.GetDocumentByPath(JobPath);
        If Doc <> Nil Then Client.CloseDocument(Doc);
        Doc := Client.GetDocumentByPath(P.Values['pcb']);
        If Doc <> Nil Then Client.CloseDocument(Doc);
        Say('closed=ok');
    Except
        Say('closed=FAIL');
    End;

    Say('ok=1');
    Say('stage=end');
End;

End.
