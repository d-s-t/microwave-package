{ Spike S1: can Altium be driven from outside the GUI at all?

  Altium has no COM server, so the only way in is to launch X2.EXE with a
  ScriptingSystem process. This proves three things the whole pipeline
  depends on, and deliberately nothing else -- every avoidable API call is
  left out so that a failure here means the launch mechanism is broken,
  not that some incidental call was misspelled:

    1. the procedure runs when invoked from the command line,
    2. it can write a sentinel file for the caller to poll for,
    3. Altium then exits, so the next combination gets a clean session.

  There is no headless mode: a runtime error in here leaves Altium parked
  in its debugger with no dialog and no exit code, which is exactly why
  the Python side must poll for the sentinel and kill on timeout rather
  than waiting on the process to end.

  Run:
    X2.EXE '-RScriptingSystem:RunScriptFile(FileName="<this file>"|ProcName="SpikeS1")'
}

Const
    SentinelPath = 'C:\Users\david\Documents\uni\lab\microwave-package\base_parts\altium\spikes\s1_done.txt';

Procedure SpikeS1;
Var
    Report : TStringList;
Begin
    Report := TStringList.Create;
    Try
        Report.Add('ok=1');
        Report.Add('when=' + DateTimeToStr(Now));
        Report.SaveToFile(SentinelPath);
    Finally
        Report.Free;
    End;

    { Quit so the next invocation starts from a clean session. }
    Client.SendMessage('Client:Quit', '', 512, Nil);
End;

End.

{ ---------------------------------------------------------------------
  FINDINGS (run 2026-09-02, Altium AD26)

  PASSED, and repeatable: two consecutive invocations both ran, the
  second in 3.1 s.

  Three things turned up that the pipeline design has to account for:

  1. Altium is SINGLE-INSTANCE. Launching X2.EXE does not start a new
     session -- it forwards the process to the one already running and
     the launcher stub exits immediately. So the sweep does NOT pay an
     Altium cold start per combination (3 s, not 60 s). The Python side
     must still allow a much longer timeout for the FIRST call, which is
     the only one that may have to start Altium.

  2. Client:Quit did NOT close the session, and that is a mercy rather
     than a bug: the script ran inside the operator's own already-open
     Altium (running since 15:06), so a working Quit would have killed
     their unsaved work. It is removed from the real generator -- the
     warm session is both faster and safer to leave alone.

  3. Because the generator runs inside whatever session happens to be
     open, it MUST NOT touch documents it does not own, and must refuse
     to run if its own target documents have unsaved changes. Polling a
     sentinel is the only reliable completion signal either way: the
     launcher stub's exit says nothing about whether the script ran.
  --------------------------------------------------------------------- }
