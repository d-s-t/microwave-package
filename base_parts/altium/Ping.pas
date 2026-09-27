{ Does nothing, on purpose.

  altium_generation.run_script launches this ahead of every real script
  to find out whether Altium will take one. Its launcher (X2.EXE) exits
  once Altium has taken it, and Altium takes nothing while it is showing
  a dialog -- a script that failed to parse, or one that has stopped on
  an error -- so a launcher that will not exit says Altium is waiting
  for a human.

  It does NOT say that no script is running. Altium takes a new script
  whenever the running one lets Windows messages through, and runs it
  right there, nested inside the first. Whether an earlier script of
  ours has finished is read from its report instead.

  It has no body because nothing it could do would add to that answer,
  and anything it did do could fail as a modal dialog. It names no file
  either, unlike every other script here, so it is the same from any
  checkout.
}

Procedure Ping;
Begin
End;

End.
