# Long sweeps: runbook

How to run a sweep that takes hours or days without anyone watching it, what
the runner does on the way, and what to do when it asks for help. The
mechanics are in the README section [Long sweeps](README.md#long-sweeps); this
file is the procedure.

## The short version

1. Check the list below, with the PC and the worker machine as they will be all weekend.
2. Write or pick a plan in `sweeps/examples/` (or your own file).
3. Run it once with `--dry-run`. It starts nothing and writes nothing.
4. Start it detached (command below). Check `sweeps\<name>\status.json` and `alerts.txt` after a few minutes.
5. Answer any alert. The runner does everything else.

## Before the weekend (checklist)

Do these on the analysis PC, and on the worker machine where noted.

- **The screen must not lock.** A locked screen stops AEDT's copy and paste, which
  is what builds the models here. Turn off the lock: Settings, Accounts, Sign-in
  options, "If you've been away, when should Windows require you to sign in" set to
  **Never**; Settings, Personalisation, Lock screen, screen saver "On resume, display
  logon screen" off; and no screen saver that locks. The screen may turn off, the
  session must not lock.
- **The PC must not sleep or hibernate** while plugged in (Power settings: sleep
  "Never", hibernate off). Set Windows Update's active hours to cover the weekend, and
  do not restart the PC during the sweep.
- **Altium is open** (your own instance: the runner never starts or closes it), with
  its project loaded and **no dialog open**.
- **SolidWorks: save anything you want to keep.** Before each job the runner closes,
  *without saving*, any SolidWorks document that belongs to another worktree (this
  repository's other checkouts, including `D:\microwave-package\base_parts`). Documents
  outside this repository are never touched.
- **The worker machine** is awake, its worker program is running, and it can see the
  queue share. Its heartbeat file `A:\_logs\worker_status.json` should change every
  few seconds.
- **The queue share `A:\` is mapped** and reachable from this PC.
- **Nothing else is queued** from this PC or another session (the web UI, another
  Claude session). Two pipelines on one PC share Altium, SolidWorks and AEDT, and the
  worker queue.
- **Disk space:** each run keeps its AEDT projects (gigabytes). Keep at least 20 GB free
  on the drive holding `runs\` (or `MICROWAVE_RUNS_PATH`).
- **The environment variable** `ANSYS_ANALYZE_QUEUE_PATH` is set for your user (it is
  set with `setx`; a new PowerShell must be opened after setting it).

## Expected time

| work | time |
|---|---|
| one converged solve (`--preset converged`) | 4.5 to 10 h on the worker |
| one quick solve (`--preset quick`) | about 20 min |
| the model build for each combination (Altium, SolidWorks, AEDT) | not measured here; the first job's log shows the steps and their times |

So the converged depth example (three jobs, one solve each) is roughly 14 to 30 h,
plus the builds. A job that runs past `max_job_hours` (16 h by default) is
terminated and resumed later.

## Writing a plan

A plan is a JSON file. The examples are the best guide:

- `sweeps/examples/depth_converged.json`: pedestal depth 0.1, 0.25 and 1.0 mm on the TMM 10i chip board, converged, one job per depth.
- `sweeps/examples/substrate_converged.json`: the same package on TMM 10i, RO3003 and RO4003.
- `sweeps/examples/smoke_quick_default.json`: one quick solve on the default board (about 25 min of worker time). Use it to check the setup before a long run.

```json
{
  "name": "depth_converged",
  "preset": "converged",
  "max_job_hours": 16,
  "description": "free text, optional",
  "jobs": [
    {
      "name": "depth_0.1mm",
      "note": "free text, optional",
      "args": ["--sweep", "pedestal_depth=0.1", "--sweep", "no_chip=0", "..."]
    }
  ]
}
```

- **`name`** is the folder under `sweeps\`. Letters, digits, `_`, `.` and `-` only.
- **`preset`** is `quick` or `converged`. It is passed to every job as `--preset`, and each run records it.
- **`max_job_hours`** is the wall-time limit for one attempt of one job. Default 16.
- **`jobs`** run in order. Each **`name`** is unique within the plan.
- **`args`** are the pipeline's own command-line arguments, one token per list item, exactly as you would type them after `python -m microwave_package.pipeline`. Each job needs at least one `--sweep`; a single value pins a parameter. Do not put `--preset` or `--resume` in `args`.

Each job is checked when the plan is loaded, by the pipeline's own parser, so a
misspelt flag or a bad sweep value is refused before anything runs. `--dry-run`
prints each job's five solver values, and warns when a job overrides the preset
(for example with `--max-passes`).

Keep one job to one run that finishes inside `max_job_hours`. A converged solve
takes 4.5 to 10 h, so a converged job should contain one combination. Split a
longer sweep over several jobs, as the depth example does.

### Checking the settings against a reference

When a plan is meant to reproduce an earlier run, compare the effective settings:
`run_config.json` of the new run against the old one differs only in the five solver
settings and `solver_preset`/`solver_overrides`. The depth example was checked that
way against `run_20261021_141838`.

## Starting it

Run from a checkout of this repository, in your own (not elevated) PowerShell. The
runner needs your desktop session for Altium, SolidWorks and AEDT, and the mapped
drive `A:`. A Task Scheduler job running as SYSTEM, or an elevated shell, sees
neither.

Check the plan first. This starts nothing and writes nothing:

```powershell
cd D:\microwave-package
python -m microwave_package.sweep_runner sweeps\examples\depth_converged.json --dry-run
```

Start it detached, so it keeps running after the window is closed:

```powershell
cd D:\microwave-package
New-Item -ItemType Directory -Force sweeps\depth_converged | Out-Null
Start-Process -FilePath python -WorkingDirectory D:\microwave-package -WindowStyle Hidden `
  -ArgumentList '-m','microwave_package.sweep_runner','sweeps\examples\depth_converged.json' `
  -RedirectStandardOutput sweeps\depth_converged\console.txt `
  -RedirectStandardError sweeps\depth_converged\console.err -PassThru
```

Note the PID that `-PassThru` prints. The runner also writes its own log, so the
console files are only for a crash at the very start.

Until the sweep runner is merged to `main`, use the checkout where the change
lives: `D:\microwave-package\.claude\worktrees\sweep-runner`.

Starting the same plan again while it is running is refused (`runner.lock`).
Starting it again after it stopped resumes it.

## Watching it

Everything is in `sweeps\<plan name>\`:

| file | what it tells you |
|---|---|
| `status.json` | rewritten every minute: `state` (running, waiting, paused, stopped, finished, finished-with-failures), the current job and its attempt, how long it has run, `worker.heartbeat_age_min`, `queue` (pending and in-progress task counts), job counts, `last_alert`, and whether `STOP` or `PAUSE` is present |
| `alerts.txt` | every alert, timestamped. Read this first |
| `runner.log` | everything the runner did, including each command it started |
| `results.md` / `results.csv` | one row per job and combination, rebuilt after each job |
| `state.json` | the record of each job: status, run id, attempts, interruptions, reason |
| `logs\<job>.attemptN.log` | the pipeline's own output for each attempt |

Statuses. Jobs: `pending`, `running`, `done`, `invalid`, `failed`, `timeout`. A
combination in `results`: `valid`, `invalid`, `failed`, `pending` (the job did not
finish), or `skipped` (its geometry does not fit).

## Pausing and stopping

- **Pause:** create an empty file `sweeps\<plan name>\PAUSE`. The next job does not
  start until you delete it. A running job is not affected.
- **Stop:** create `sweeps\<plan name>\STOP`. The current job finishes, and the runner
  exits. To start again, delete `STOP` first (the runner refuses to start while it is
  there, and says so).
- **Stopping the runner process itself** (closing it, or ending it in Task Manager)
  does not stop the pipeline it started: that child keeps running until its job ends.
  Start the runner again and it waits for that child, then resumes the run. Prefer
  `STOP` for a planned stop.

## What the alerts mean, and what to do

Each alert appears once for as long as its problem lasts, and is followed by a "Resolved" line in `runner.log` when it clears.

| alert | what it means | what you do |
|---|---|---|
| `Windows is locked (LogonUI.exe is running)` | The screen is locked. A job will not start, and a job already running fails its local build and is resumed. | Unlock the PC. The runner re-checks every minute. |
| `Altium has a dialog open (...)` | Altium is refusing scripts while a message box is open. | Dismiss it in Altium. Do not close Altium. |
| `the worker heartbeat is N min old` | The worker has not written its heartbeat for N minutes. Before a job starts, the limit is 15 min; during a job the alert is at 30. | Check the worker PC: asleep, program stopped, network. The runner waits; it does nothing to the worker. |
| `the worker looks hung: no new worker log line for N min` | Both the heartbeat and the worker log have been quiet for 90 min. The worker has hung before and recovered by itself. | Check the worker PC. The runner keeps waiting; if the worker is not coming back, stop the sweep. |
| `the queue folder ... is not reachable` | This PC cannot see `A:\`. | Reconnect the share (VPN, network). The job waits, then starts. |
| `closing N SolidWorks document(s) ... WITHOUT saving` | The runner is closing documents left by another worktree. This is information, not a problem. | Nothing. If your own work was in one of them, you have lost its unsaved edits: save before the sweep next time. |
| `could not close the SolidWorks document ...` | SolidWorks refused to close it. The next SolidWorks build can fail with OpenDoc6 error 65536. | Close it by hand in SolidWorks, without saving. |
| `a pipeline process from an earlier runner (pid N) is still running` | A run from an earlier start is still going (the runner was stopped, or the PC restarted without it). | Nothing. The runner waits for it and does not touch it. |
| `Job X interrupted (...); it resumes once the preflight passes` | A locked screen, an Altium dialog or a refused SolidWorks document broke the job. | Clear the cause if it is still there. The job resumes its run. After three interruptions the job is marked failed. |
| `Job X is failed: ...` | The job did not produce a complete result. The reason is in the line and in `state.json`. | Read the reason. A failed job is retried the next time the runner starts. |
| `Job X is invalid: ...` | The results exist, but a combination's solve is not to be trusted: a flat S21, or the worker logged `Error in Solving`. | See "Redoing a job" below. |
| `Job X ... wall-time limit ... reached` | The job ran longer than `max_job_hours` on this attempt. Its pipeline child was terminated. | Nothing. Start the runner again to resume it. Raise `max_job_hours` if the job is expected to take longer. |
| `Altium (X2.EXE) is not running: open it yourself` | Altium is not open on this PC. The runner waits, and never starts Altium. A job with `--no-altium` does not wait for it. | Open your Altium project yourself. Do not expect the runner to start it. |
| `SolidWorks is running but its COM object could not be attached` | The runner could not attach to the running SolidWorks session, so no document check was made for that job. | Check that SolidWorks is running in your session with no dialog open. If documents from another checkout are open, close them by hand without saving; otherwise a build can fail with OpenDoc6 error 65536. |
| `SolidWorks: the document list stopped early (...)` or `a document's path could not be read (...)` | Some open SolidWorks documents could not be read, so a document from another checkout may have been missed. | Close documents from other checkouts by hand, without saving, before the next build. |
| `STOP file found` | The sweep is stopping after the current job. | Nothing, unless you did not mean to stop. Delete `STOP` to keep it going. |

## Redoing a job

- **A failed or timed-out job** is retried the next time the runner starts; it resumes
  its run, so nothing is solved twice.
- **An invalid job** is not retried by itself, because its results exist. To solve it
  again, stop the runner, open `sweeps\<plan name>\state.json`, delete the job's entry
  under `"jobs"`, and start the runner. The job then starts a new run with the same
  arguments. The old run stays in `runs\`, and its rows are gone from `results.*`
  after the next job. Keep a copy of `state.json` first.
- **A changed plan:** if you change a job's `args`, the runner drops that job's earlier
  attempt (it says so in the alerts) and starts it again. A finished job is kept.

## Dry run and checks

`--dry-run` is safe to run at any time. It prints the command for each job, the five
solver values and whether they were overridden, and whether each job would start,
resume or be skipped. It also runs every preflight check (the heartbeat, the queue,
the screen, Altium's dialogs) and lists the SolidWorks documents from other checkouts
that a real run would close, without closing anything.

## Limits and what is not checked

- The runner does not restart the worker, Altium, SolidWorks or AEDT, and it never
  starts or closes Altium.
- The check on results is for flat S21 and for the worker's `Error in Solving` lines.
  A converged result still needs a look: compare the band numbers with the reference
  runs and the neighbouring points before quoting one.
- A restart of the PC or a power cut ends the runner and its child. Start the runner
  again with the same command: the job resumes its run.
