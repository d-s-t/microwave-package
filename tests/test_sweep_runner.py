"""The long-sweep runner, tested with fakes only: no Altium, SolidWorks, AEDT,
no worker, no queue, no process is touched. The clock is simulated, so a job
that runs for 10 hours takes milliseconds.

Run: python -m unittest discover -s tests
"""
import csv
import json
import logging
import os
import shutil
import tempfile
import unittest
from io import StringIO
from pathlib import Path

import numpy as np

from microwave_package import sweep_runner as sr

EXAMPLES = Path(__file__).resolve().parent.parent / "sweeps" / "examples"
ARGS = ["--sweep", "pedestal_depth=0.1", "--solution-type", "DrivenModal", "--no-pair"]
TASK = "driven_20261021_143603_986692"
OTHER_TASK = "driven_20261021_150000_000001"


def logging_quiet():
    logging.disable(logging.CRITICAL)


def make_plan(jobs=None, preset="converged", hours=None, name="test_plan"):
    data = {"name": name, "preset": preset,
            "jobs": jobs if jobs is not None else [{"name": "one", "args": list(ARGS)}]}
    if hours is not None:
        data["max_job_hours"] = hours
    return sr.parse_plan(data)


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.t = start
        self.sleeps = 0
        self.on_sleep = None

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps += 1
        if self.sleeps > 200_000:
            raise RuntimeError("runaway loop in a test")
        self.t += seconds
        if self.on_sleep is not None:
            self.on_sleep(self)


class FakeProc:
    """A pipeline process: exits after `end_after` seconds with `rc`, and runs
    scripted events (log lines, state changes) at the offsets given."""

    def __init__(self, clock, log_path, pid, script):
        self.clock = clock
        self.log_path = log_path
        self.pid = pid
        self.script = script
        self.started = clock.now()
        self.events = sorted(script.get("events", []), key=lambda event: event[0])
        self.returncode = None
        self.terminated = False
        self.killed = False

    def append(self, text):
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        elapsed = self.clock.now() - self.started
        while self.events and elapsed >= self.events[0][0]:
            _, action = self.events.pop(0)
            action(self)
        if self.terminated:
            self.returncode = -15
        elif self.script.get("end_after") is not None and elapsed >= self.script["end_after"]:
            self.returncode = self.script.get("rc", 0)
            if self.script.get("on_end"):
                self.script["on_end"](self)
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.terminated = True


class FakePopen:
    def __init__(self, clock, scripts=()):
        self.clock = clock
        self.scripts = list(scripts)
        self.calls = []
        self.procs = []
        self.pid = 4000

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        script = self.scripts.pop(0) if self.scripts else {"end_after": 60, "rc": 0}
        self.pid += 1
        proc = FakeProc(self.clock, kwargs["stdout"].name, self.pid, script)
        self.procs.append(proc)
        return proc


class FakeDesktop:
    def __init__(self, root, clock):
        self.queue_root = Path(root)
        self.clock = clock
        self.worker_log_path = self.queue_root / "_logs" / "ansys_analyze_worker.log"
        self.reachable = True
        self.age = 1.0
        self.detail = {"worker_state": "running", "current_task_id": None}
        self.log_size = 0
        self.errors = {}
        self.locked = False
        self.modal = []
        self.sw_docs = []
        self.closed = []
        self.alive = {}
        self.queue = {"pending": 0, "in_progress": 0}

    def queue_reachable(self):
        return self.reachable

    def heartbeat_age_minutes(self):
        return self.age

    def heartbeat_detail(self):
        return dict(self.detail)

    def worker_log_size(self):
        return self.log_size

    def worker_errors(self, offset):
        return None if self.errors is None else dict(self.errors)

    def queue_counts(self):
        return dict(self.queue) if self.reachable else None

    def screen_locked(self):
        return self.locked

    def altium_modal_titles(self):
        return list(self.modal)

    def solidworks_documents(self):
        return list(self.sw_docs)

    def close_solidworks_documents(self, paths):
        self.closed.extend(paths)
        return []

    def create_time(self, pid):
        return 1.0

    def process_alive(self, pid, create_time):
        return bool(self.alive.get(pid, False))


def write_results(path, s21_db, s11_db=-8.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    freq = np.linspace(2.0, 20.0, len(s21_db))
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["freq_ghz", "s11_db", "s11_deg", "s21_db", "s21_deg", "sweep"])
        for f, s11, s21 in zip(freq, s11_db if isinstance(s11_db, list) else [s11_db] * len(freq), s21_db):
            writer.writerow([f"{f:.6f}", s11, 0.0, s21, 0.0, "coarse"])


def make_run(runs_root, run_id, *, stage="complete", task=TASK, s21=None, queue_status="done",
             eigen_run_id=None, skip=None):
    """A run folder like the pipeline leaves: state, config and one combination."""
    folder = Path(runs_root) / run_id
    folder.mkdir(parents=True, exist_ok=True)
    combo = {"params": {"pedestal_depth": 0.1}, "task_id": task, "queue_status": queue_status,
             "skip_reason": skip, "step_status": "done", "model_status": "built"}
    (folder / "state.json").write_text(json.dumps({"stage": stage, "combos": {"pedestal_depth-0.1mm": combo}}))
    config = {"analysis_settings": {
        "solver_preset": "converged", "solver_overrides": [], "max_passes": 99, "min_passes": 3,
        "min_converged": 2, "max_delta_s": 0.02, "max_delta_f": 3.0},
        "sweep_params": [], "parameter_defaults": {}}
    if eigen_run_id:
        config["eigen_run_id"] = eigen_run_id
    (folder / "run_config.json").write_text(json.dumps(config))
    if s21 is not None:
        write_results(folder / "models" / "pedestal_depth-0.1mm" / "results.csv", s21)
    return folder


def varying_s21(n=73):
    return [-2.0 - 0.01 * (i % 17) for i in range(n)]


class PlanTests(unittest.TestCase):
    def test_example_plans_load(self):
        for name in ("depth_converged", "substrate_converged", "smoke_quick_default"):
            plan = sr.load_plan(EXAMPLES / f"{name}.json")
            self.assertEqual(plan.name, name)
            self.assertTrue(plan.jobs)

    def test_depth_example_is_three_converged_jobs_inside_the_limit(self):
        plan = sr.load_plan(EXAMPLES / "depth_converged.json")
        self.assertEqual(plan.preset, "converged")
        self.assertEqual([job.name for job in plan.jobs], ["depth_0.1mm", "depth_0.25mm", "depth_1.0mm"])
        self.assertEqual(plan.max_job_hours, 16.0)

    def test_unknown_preset_is_refused(self):
        with self.assertRaises(sr.PlanError):
            sr.parse_plan({"name": "x", "preset": "medium", "jobs": [{"name": "a", "args": ARGS}]})

    def test_job_names_must_be_unique_and_safe(self):
        with self.assertRaises(sr.PlanError):
            sr.parse_plan({"name": "x", "preset": "quick",
                           "jobs": [{"name": "a", "args": ARGS}, {"name": "a", "args": ARGS}]})
        with self.assertRaises(sr.PlanError):
            sr.parse_plan({"name": "x", "preset": "quick", "jobs": [{"name": "a/b", "args": ARGS}]})
        with self.assertRaises(sr.PlanError):
            sr.parse_plan({"name": "../escape", "preset": "quick", "jobs": [{"name": "a", "args": ARGS}]})

    def test_preset_and_resume_are_not_job_arguments(self):
        for extra in (["--preset", "quick"], ["--resume", "run_20261021_141838"]):
            with self.assertRaises(sr.PlanError):
                sr.parse_plan({"name": "x", "preset": "quick",
                               "jobs": [{"name": "a", "args": ARGS + extra}]})

    def test_a_job_needs_a_sweep(self):
        with self.assertRaises(sr.PlanError):
            sr.parse_plan({"name": "x", "preset": "quick",
                           "jobs": [{"name": "a", "args": ["--solution-type", "DrivenModal"]}]})

    def test_arguments_the_pipeline_refuses_are_caught_before_a_run(self):
        with self.assertRaises(sr.PlanError) as ctx:
            sr.parse_plan({"name": "x", "preset": "quick",
                           "jobs": [{"name": "a", "args": ARGS + ["--no-such-flag"]}]})
        self.assertIn("job a", str(ctx.exception))

    def test_bad_sweep_spec_is_refused(self):
        with self.assertRaises(sr.PlanError):
            sr.parse_plan({"name": "x", "preset": "quick",
                           "jobs": [{"name": "a", "args": ["--sweep", "pedestal_depth=0:10:0"]}]})

    def test_unknown_plan_key_is_refused(self):
        with self.assertRaises(sr.PlanError):
            sr.parse_plan({"name": "x", "preset": "quick", "max_job_hour": 4,
                           "jobs": [{"name": "a", "args": ARGS}]})

    def test_wall_limit_must_be_positive(self):
        for bad in (0, -1, "16", True):
            with self.assertRaises(sr.PlanError):
                sr.parse_plan({"name": "x", "preset": "quick", "max_job_hours": bad,
                               "jobs": [{"name": "a", "args": ARGS}]})

    def test_fresh_and_resume_commands(self):
        plan = make_plan()
        job = plan.jobs[0]
        fresh = sr.job_command(plan, job, None, python="py")
        self.assertEqual(fresh[:4], ["py", "-u", "-m", "microwave_package.pipeline"])
        self.assertEqual(fresh[4:6], ["--preset", "converged"])
        self.assertEqual(fresh[6:], ARGS)
        resumed = sr.job_command(plan, job, "run_20261021_141838", python="py")
        self.assertEqual(resumed, ["py", "-u", "-m", "microwave_package.pipeline",
                                   "--resume", "run_20261021_141838"])

    def test_the_job_settings_record_the_preset(self):
        settings = sr.job_settings(ARGS, "quick")
        self.assertEqual(settings["solver_preset"], "quick")
        self.assertEqual(settings["max_passes"], 4)
        self.assertEqual(settings["solver_overrides"], [])
        overridden = sr.job_settings(ARGS + ["--max-passes", "10"], "quick")
        self.assertEqual(overridden["solver_overrides"], ["max_passes"])


class WorkerLogTests(unittest.TestCase):
    def test_error_in_the_last_block_of_a_task_counts(self):
        text = "\n".join([
            f"INFO: Processing task {TASK} (run_setups)",
            "ERROR: Error in Solving Setup Setup_1",
            "INFO: Design setup Setup_1 solved correctly in 0.0h 0.0m 3.0s",
            f"INFO: Processing task {TASK} (run_setups)",
            "INFO: Design setup Setup_1 solved correctly in 0.0h 18.0m 43.0s",
            f"INFO: Task {TASK} completed successfully.",
        ])
        self.assertEqual(sr.count_errors_by_task(text)[TASK], 0)

    def test_error_in_an_earlier_block_only_is_not_forgiven_by_another_task(self):
        text = "\n".join([
            f"INFO: Processing task {TASK} (run_setups)",
            "ERROR: Error in Solving Setup Setup_1",
            f"INFO: Processing task {OTHER_TASK} (run_setups)",
            "INFO: solved correctly",
        ])
        counts = sr.count_errors_by_task(text)
        self.assertEqual(counts[TASK], 1)
        self.assertEqual(counts[OTHER_TASK], 0)

    def test_an_error_before_any_task_is_ignored(self):
        text = "ERROR: Error in Solving Setup Setup_1\n" + f"INFO: Processing task {TASK} (x)\n"
        self.assertEqual(sr.count_errors_by_task(text), {TASK: 0})

    def test_a_task_named_in_a_path_line_takes_the_error(self):
        text = "\n".join([
            f"INFO: Parsing C:\\Ansys\\projects\\{TASK}\\run_1.aedt.",
            "ERROR: Error in Solving Setup Setup_1",
        ])
        self.assertEqual(sr.count_errors_by_task(text)[TASK], 1)

    def test_unreadable_log_gives_none(self):
        self.assertIsNone(sr.worker_errors_since(Path(tempfile.gettempdir()) / "no_such_worker.log", 0))


class ArithmeticTests(unittest.TestCase):
    def test_band_means_and_clean_share(self):
        freq = np.linspace(2.0, 20.0, 73)
        s11 = np.full(73, 10 ** (-10 / 20))
        s21 = np.full(73, 10 ** (-2 / 20))
        stats = sr.band_stats(freq, s11, s21)
        expected = [int(s11_in.sum()) for s11_in in
                    (sr.s11_analysis.in_band(freq, band) for band in sr.BANDS)]
        self.assertEqual(stats["points"], expected)
        self.assertEqual(stats["points"][0], 33)          # 2 to 10 GHz, both ends
        self.assertEqual(stats["points"][2], 12)          # 17 to 20 GHz, 20 GHz itself left out
        self.assertAlmostEqual(stats["s11"][0], -10.0, places=6)
        self.assertAlmostEqual(stats["s21"][1], -2.0, places=6)
        self.assertEqual(stats["clean"], [100.0, 100.0, 100.0])

    def test_clean_share_counts_points_above_minus_three(self):
        freq = np.linspace(2.0, 20.0, 73)
        s21_db = np.where(freq <= 10.0, -2.0, -5.0)
        stats = sr.band_stats(freq, np.ones(73), 10 ** (s21_db / 20))
        self.assertEqual(stats["clean"][0], 100.0)
        self.assertEqual(stats["clean"][2], 0.0)

    def test_heartbeat_age_is_not_fooled_by_clock_skew(self):
        heartbeat = sr.Heartbeat()
        heartbeat.observe(1000.0, now=5000.0)            # first look: age from the timestamp
        self.assertAlmostEqual(heartbeat.age_minutes(5000.0), (5000.0 - 1000.0) / 60)
        heartbeat.observe(1100.0, now=5010.0)            # the timestamp moved: it is fresh
        self.assertAlmostEqual(heartbeat.age_minutes(5020.0), 10.0 / 60)

    def test_heartbeat_timestamp_in_the_future_is_age_zero(self):
        heartbeat = sr.Heartbeat()
        heartbeat.observe(9000.0, now=5000.0)
        self.assertEqual(heartbeat.age_minutes(5000.0), 0.0)

    def test_missing_heartbeat_has_no_age(self):
        heartbeat = sr.Heartbeat()
        heartbeat.observe(None, now=5000.0)
        self.assertIsNone(heartbeat.age_minutes(5000.0))

    def test_log_growth_minutes(self):
        growth = sr.LogGrowth()
        growth.start(100, now=0.0)
        self.assertAlmostEqual(growth.update(100, now=600.0), 10.0)
        self.assertAlmostEqual(growth.update(150, now=660.0), 0.0)

    def test_foreign_documents_are_other_checkouts_only(self):
        main = Path(tempfile.mkdtemp(prefix="main_"))
        own = main / ".claude" / "worktrees" / "sweep-runner"
        other = main / ".claude" / "worktrees" / "youthful-cray"
        outside = Path(tempfile.mkdtemp(prefix="elsewhere_"))
        documents = [
            (str(own / "base_parts" / "box.SLDPRT"), False),
            (str(other / "base_parts" / "box.SLDPRT"), True),
            (str(main / "base_parts" / "lid_inside.SLDPRT"), True),
            (str(outside / "mine.SLDPRT"), True),
            ("", False),
        ]
        found = sr.foreign_documents(documents, own, main)
        self.assertEqual(found, [str(other / "base_parts" / "box.SLDPRT"),
                                 str(main / "base_parts" / "lid_inside.SLDPRT")])
        self.assertEqual(sr.main_checkout_of(own), main)


class Verdicts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="verdict_"))
        self.runs = self.tmp / "runs"

    def test_valid_flat_missing_and_logged_errors(self):
        make_run(self.runs, "run_ok", s21=varying_s21())
        rows = sr.evaluate_run(self.runs, "run_ok", {TASK: 0}, True, "p", "j", "2026-10-21")
        self.assertEqual(rows[0]["status"], "valid")
        self.assertEqual(rows[0]["valid"], "yes")
        self.assertEqual(rows[0]["points_2_10"], 33)

        make_run(self.runs, "run_flat", s21=[-2.0] * 73)
        flat = sr.evaluate_run(self.runs, "run_flat", {TASK: 0}, True, "p", "j", "2026-10-21")[0]
        self.assertEqual(flat["status"], "invalid")
        self.assertIn("flat", flat["reason"])

        logged = sr.evaluate_run(self.runs, "run_ok", {TASK: 2}, True, "p", "j", "2026-10-21")[0]
        self.assertEqual(logged["status"], "invalid")
        self.assertIn("Error in Solving", logged["reason"])

        make_run(self.runs, "run_missing", s21=None)
        missing = sr.evaluate_run(self.runs, "run_missing", {TASK: 0}, True, "p", "j", "2026-10-21")[0]
        self.assertEqual(missing["status"], "failed")
        pending = sr.evaluate_run(self.runs, "run_missing", {TASK: 0}, False, "p", "j", "2026-10-21")[0]
        self.assertEqual(pending["status"], "pending")

    def test_a_worker_failed_task_is_failed_and_an_unreadable_log_is_failed(self):
        make_run(self.runs, "run_wf", s21=varying_s21(), queue_status="failed")
        self.assertEqual(sr.evaluate_run(self.runs, "run_wf", {}, True, "p", "j", "d")[0]["status"], "failed")
        make_run(self.runs, "run_ul", s21=varying_s21())
        row = sr.evaluate_run(self.runs, "run_ul", None, True, "p", "j", "d")[0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("log could not be read", row["reason"])

    def test_skipped_combination_is_not_judged(self):
        make_run(self.runs, "run_skip", skip="geometry does not fit", s21=None)
        row = sr.evaluate_run(self.runs, "run_skip", {}, True, "p", "j", "d")[0]
        self.assertEqual(row["status"], "skipped")

    def test_job_verdict_rules(self):
        ok = {"status": "valid", "combo": "a", "reason": ""}
        bad = {"status": "invalid", "combo": "b", "reason": "flat"}
        gone = {"status": "failed", "combo": "c", "reason": "no results"}
        skip = {"status": "skipped", "combo": "d", "reason": "fit"}
        self.assertEqual(sr.job_verdict([ok, skip])[0], "done")
        self.assertEqual(sr.job_verdict([ok, bad])[0], "invalid")
        self.assertEqual(sr.job_verdict([ok, bad, gone])[0], "failed")
        self.assertEqual(sr.job_verdict([skip])[0], "failed")
        self.assertEqual(sr.job_verdict([])[0], "failed")

    def test_results_files_carry_the_date_and_band_edges(self):
        make_run(self.runs, "run_ok", s21=varying_s21())
        rows = sr.evaluate_run(self.runs, "run_ok", {}, True, "test_plan", "j", "2026-10-21")
        folder = self.tmp / "sweeps" / "test_plan"
        sr.render_results(folder, make_plan(), rows, now=1_000_000.0)
        with open(folder / "results.csv", newline="", encoding="utf-8") as handle:
            read = list(csv.DictReader(handle))
        self.assertEqual(read[0]["date"], "2026-10-21")
        self.assertEqual(read[0]["valid"], "yes")
        self.assertIn("preset", read[0])
        text = (folder / "results.md").read_text(encoding="utf-8")
        self.assertIn("Sweep results: test_plan", text)
        self.assertIn("17-20 GHz [17,20)", text)
        self.assertIn("2026-10-21", text)


class RunnerFixture(unittest.TestCase):
    def setUp(self):
        logging_quiet()
        self.tmp = Path(tempfile.mkdtemp(prefix="sweep_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runs = self.tmp / "runs"
        self.clock = FakeClock()
        self.desktop = FakeDesktop(self.tmp / "queue", self.clock)
        self.main = self.tmp / "main"
        self.project = self.main / ".claude" / "worktrees" / "sweep-runner"
        self.project.mkdir(parents=True)

    def runner(self, plan, popen, **kwargs):
        folder = self.tmp / "sweeps" / plan.name
        return sr.SweepRunner(plan, folder, desktop=self.desktop, project_root=self.project,
                              runs_root=self.runs, clock=self.clock.now, sleep=self.clock.sleep,
                              popen=popen, echo=False, **kwargs)

    def folder(self, plan):
        return self.tmp / "sweeps" / plan.name

    def alerts(self, plan):
        path = self.folder(plan) / "alerts.txt"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def state(self, plan):
        return json.loads((self.folder(plan) / "state.json").read_text(encoding="utf-8"))

    def created_run(self, run_id, **kwargs):
        """Events for a process that creates a run and finishes it with results."""
        def create(proc):
            proc.append(f"2026-10-21 14:00:00,000 INFO: Created run {run_id} with 1 combination(s)")
        return create

    def finish_run(self, run_id, **kwargs):
        def on_end(proc):
            make_run(self.runs, run_id, **kwargs)
        return on_end


class Lifecycle(RunnerFixture):
    def test_a_good_job_is_done_and_its_rows_are_written(self):
        plan = make_plan()
        popen = FakePopen(self.clock, [{
            "end_after": 600, "rc": 0,
            "events": [(1, self.created_run("run_good"))],
            "on_end": self.finish_run("run_good", s21=varying_s21()),
        }])
        code = self.runner(plan, popen).run()
        self.assertEqual(code, 0)
        job = self.state(plan)["jobs"]["one"]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["run_id"], "run_good")
        self.assertEqual(job["combos"][0]["status"], "valid")
        self.assertIn("--preset", popen.calls[0])
        self.assertTrue((self.folder(plan) / "results.csv").exists())
        status = json.loads((self.folder(plan) / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "finished")
        self.assertEqual(status["jobs"]["done"], 1)
        self.assertIn("heartbeat_age_min", status["worker"])
        self.assertIsNone(status["current_job"])

    def test_a_job_that_is_already_done_is_not_started_again(self):
        plan = make_plan()
        popen = FakePopen(self.clock, [{"end_after": 60, "rc": 0, "events": [(1, self.created_run("run_a"))],
                                        "on_end": self.finish_run("run_a", s21=varying_s21())}])
        self.runner(plan, popen).run()
        second = FakePopen(self.clock, [])
        self.runner(plan, second).run()
        self.assertEqual(second.calls, [])

    def test_an_interrupted_job_resumes_the_same_run(self):
        plan = make_plan()

        def first_attempt_fails(proc):
            proc.append("ERROR: AEDT copy/paste failed: script macro error")
            proc.append("2026-10-21 14:00:00,000 INFO: Created run run_resume with 1 combination(s)")

        popen = FakePopen(self.clock, [
            {"end_after": 60, "rc": 1, "events": [(1, first_attempt_fails)]},
            {"end_after": 60, "rc": 0, "on_end": self.finish_run("run_resume", s21=varying_s21())},
        ])
        self.runner(plan, popen).run()
        self.assertEqual(len(popen.calls), 2)
        self.assertIn("--resume", popen.calls[1])
        self.assertIn("run_resume", popen.calls[1])
        job = self.state(plan)["jobs"]["one"]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["interruptions"], 1)
        self.assertTrue(any("Altium" in line or "AEDT" in line or "interrupted" in line
                            for line in self.alerts(plan)))

    def test_a_run_that_is_only_half_handed_over_is_resumed(self):
        plan = make_plan()
        popen = FakePopen(self.clock, [
            {"end_after": 60, "rc": 0, "events": [(1, self.created_run("run_half"))],
             "on_end": self.finish_run("run_half", stage="geometry_ready", s21=None)},
            {"end_after": 60, "rc": 0, "on_end": self.finish_run("run_half", s21=varying_s21())},
        ])
        self.runner(plan, popen).run()
        self.assertEqual(self.state(plan)["jobs"]["one"]["status"], "done")
        self.assertIn("--resume", popen.calls[1])

    def test_a_generic_failure_is_failed_with_its_error_line(self):
        plan = make_plan()

        def boom(proc):
            proc.append("2026-10-21 14:00:00,000 ERROR: Task failed: Project has no analysis setup.")

        popen = FakePopen(self.clock, [{"end_after": 30, "rc": 2, "events": [(1, boom)]}])
        code = self.runner(plan, popen).run()
        self.assertEqual(code, 1)
        job = self.state(plan)["jobs"]["one"]
        self.assertEqual(job["status"], "failed")
        self.assertIn("exit code 2", job["reason"])
        self.assertIn("no analysis setup", job["reason"])
        self.assertEqual(len(popen.calls), 1)

    def test_a_job_that_overruns_is_terminated_and_the_next_one_runs(self):
        plan = make_plan(jobs=[{"name": "slow", "args": list(ARGS)}, {"name": "next", "args": list(ARGS)}],
                         hours=0.1)
        slow = {"end_after": None}
        popen = FakePopen(self.clock, [slow, {"end_after": 30, "rc": 0,
                                              "events": [(1, self.created_run("run_next"))],
                                              "on_end": self.finish_run("run_next", s21=varying_s21())}])
        self.runner(plan, popen).run()
        self.assertTrue(popen.procs[0].terminated)
        jobs = self.state(plan)["jobs"]
        self.assertEqual(jobs["slow"]["status"], "timeout")
        self.assertEqual(jobs["next"]["status"], "done")
        self.assertTrue(any("wall-time limit" in line for line in self.alerts(plan)))

    def test_stop_on_error_leaves_the_rest_unstarted(self):
        plan = make_plan(jobs=[{"name": "bad", "args": list(ARGS)}, {"name": "later", "args": list(ARGS)}])
        popen = FakePopen(self.clock, [{"end_after": 10, "rc": 3}])
        code = self.runner(plan, popen, stop_on_error=True).run()
        self.assertEqual(code, 1)
        self.assertEqual(len(popen.calls), 1)
        self.assertEqual(self.state(plan)["jobs"]["later"]["status"], "pending")

    def test_an_invalid_job_is_recorded_and_skipped_next_time(self):
        plan = make_plan()
        popen = FakePopen(self.clock, [{"end_after": 60, "rc": 0, "events": [(1, self.created_run("run_inv"))],
                                        "on_end": self.finish_run("run_inv", s21=varying_s21())}])
        self.desktop.errors = {TASK: 1}
        self.runner(plan, popen).run()
        self.assertEqual(self.state(plan)["jobs"]["one"]["status"], "invalid")
        self.assertTrue(any("not valid" in line or "invalid" in line for line in self.alerts(plan)))
        again = FakePopen(self.clock, [])
        self.runner(plan, again).run()
        self.assertEqual(again.calls, [])

    def test_a_restart_resumes_a_job_that_was_running(self):
        plan = make_plan()
        folder = self.folder(plan)
        folder.mkdir(parents=True)
        (folder / "state.json").write_text(json.dumps({
            "plan": plan.name, "order": ["one"], "jobs": {"one": {
                **sr.new_job_record(plan.jobs[0]), "status": "running", "run_id": "run_old",
                "attempts": 1, "log": str(folder / "logs" / "one.attempt1.log")}}}))
        make_run(self.runs, "run_old", stage="eigenmodes", s21=None)
        popen = FakePopen(self.clock, [{"end_after": 60, "rc": 0,
                                        "on_end": self.finish_run("run_old", s21=varying_s21())}])
        self.runner(plan, popen).run()
        self.assertIn("--resume", popen.calls[0])
        self.assertIn("run_old", popen.calls[0])
        self.assertEqual(self.state(plan)["jobs"]["one"]["status"], "done")

    def test_a_job_waits_for_a_pipeline_process_left_by_an_earlier_runner(self):
        plan = make_plan()
        folder = self.folder(plan)
        folder.mkdir(parents=True)
        (folder / "state.json").write_text(json.dumps({
            "plan": plan.name, "order": ["one"], "jobs": {"one": {
                **sr.new_job_record(plan.jobs[0]), "status": "running", "run_id": "run_left",
                "child_pid": 4242, "child_create_time": 1.0}}}))
        self.desktop.alive[4242] = True
        make_run(self.runs, "run_left", s21=varying_s21())

        def child_exits(clock):
            if clock.sleeps == 5:
                self.desktop.alive[4242] = False

        self.clock.on_sleep = child_exits
        popen = FakePopen(self.clock, [{"end_after": 30, "rc": 0}])
        self.runner(plan, popen).run()
        self.assertEqual(len(popen.calls), 1)
        self.assertIn("--resume", popen.calls[0])
        self.assertTrue(any("earlier runner" in line for line in self.alerts(plan)))


class Preflight(RunnerFixture):
    def test_a_stale_heartbeat_holds_the_job_until_it_is_fresh(self):
        plan = make_plan()
        self.desktop.age = 20.0
        popen = FakePopen(self.clock, [{"end_after": 60, "rc": 0, "events": [(1, self.created_run("run_p"))],
                                        "on_end": self.finish_run("run_p", s21=varying_s21())}])

        def heal(clock):
            if clock.sleeps == 13:
                self.desktop.age = 1.0

        self.clock.on_sleep = heal
        self.runner(plan, popen).run()
        alerts = [line for line in self.alerts(plan) if "heartbeat" in line]
        self.assertEqual(len(alerts), 1)          # one alert for the whole wait
        self.assertEqual(self.state(plan)["jobs"]["one"]["status"], "done")

    def test_a_locked_screen_holds_the_job(self):
        plan = make_plan()
        self.desktop.locked = True
        popen = FakePopen(self.clock, [{"end_after": 60, "rc": 0, "events": [(1, self.created_run("run_l"))],
                                        "on_end": self.finish_run("run_l", s21=varying_s21())}])

        def unlock(clock):
            if clock.sleeps == 20:
                self.desktop.locked = False

        self.clock.on_sleep = unlock
        self.runner(plan, popen).run()
        self.assertEqual(sum("locked" in line for line in self.alerts(plan)), 1)
        self.assertEqual(self.state(plan)["jobs"]["one"]["status"], "done")
        self.assertGreater(self.clock.sleeps, 20)

    def test_an_altium_dialog_holds_the_job(self):
        plan = make_plan()
        self.desktop.modal = ["Confirm"]
        popen = FakePopen(self.clock, [{"end_after": 60, "rc": 0, "events": [(1, self.created_run("run_m"))],
                                        "on_end": self.finish_run("run_m", s21=varying_s21())}])

        def dismiss(clock):
            if clock.sleeps == 13:
                self.desktop.modal = []

        self.clock.on_sleep = dismiss
        self.runner(plan, popen).run()
        self.assertTrue(any("Altium has a dialog open" in line for line in self.alerts(plan)))
        self.assertEqual(self.state(plan)["jobs"]["one"]["status"], "done")

    def test_stop_during_a_wait_exits_without_starting(self):
        plan = make_plan()
        self.desktop.reachable = False
        folder = self.folder(plan)

        def stop(clock):
            if clock.sleeps == 3:
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "STOP").write_text("stop")

        self.clock.on_sleep = stop
        popen = FakePopen(self.clock, [])
        code = self.runner(plan, popen).run()
        self.assertEqual(code, 0)
        self.assertEqual(popen.calls, [])
        self.assertEqual(self.state(plan)["jobs"]["one"]["status"], "pending")

    def test_pause_holds_the_next_job_until_removed(self):
        plan = make_plan()
        folder = self.folder(plan)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "PAUSE").write_text("pause")

        def resume(clock):
            if clock.sleeps == 9:
                (folder / "PAUSE").unlink()

        self.clock.on_sleep = resume
        popen = FakePopen(self.clock, [{"end_after": 60, "rc": 0, "events": [(1, self.created_run("run_pz"))],
                                        "on_end": self.finish_run("run_pz", s21=varying_s21())}])
        self.runner(plan, popen).run()
        self.assertGreaterEqual(self.clock.sleeps, 9)
        self.assertEqual(self.state(plan)["jobs"]["one"]["status"], "done")

    def test_solidworks_documents_from_another_checkout_are_closed_before_the_job(self):
        plan = make_plan()
        own = self.project / "base_parts" / "box.SLDPRT"
        other = self.main / ".claude" / "worktrees" / "youthful-cray" / "base_parts" / "box.SLDPRT"
        self.desktop.sw_docs = [(str(own), False), (str(other), True)]
        popen = FakePopen(self.clock, [{"end_after": 60, "rc": 0, "events": [(1, self.created_run("run_sw"))],
                                        "on_end": self.finish_run("run_sw", s21=varying_s21())}])
        self.runner(plan, popen).run()
        self.assertEqual(self.desktop.closed, [str(other)])


class Watchdog(RunnerFixture):
    def test_heartbeat_alert_once_per_episode_and_cleared(self):
        plan = make_plan()
        runner = self.runner(plan, FakePopen(self.clock, []))
        runner.folder.mkdir(parents=True, exist_ok=True)
        runner.log_growth.start(0, self.clock.now())
        record = sr.new_job_record(plan.jobs[0])
        log_path = runner.folder / "logs" / "one.attempt1.log"
        self.desktop.age = 31.0
        runner.watch("one", record, log_path)
        runner.watch("one", record, log_path)
        self.assertEqual(sum("heartbeat" in line for line in self.alerts(plan)), 1)
        self.desktop.age = 10.0
        runner.watch("one", record, log_path)
        self.assertIn("Resolved: watch heartbeat", (runner.folder / "runner.log").read_text(encoding="utf-8"))

    def test_hang_needs_a_stale_heartbeat_and_a_silent_log_for_ninety_minutes(self):
        plan = make_plan()
        runner = self.runner(plan, FakePopen(self.clock, []))
        runner.folder.mkdir(parents=True, exist_ok=True)
        record = sr.new_job_record(plan.jobs[0])
        log_path = runner.folder / "logs" / "one.attempt1.log"
        runner.log_growth.start(0, self.clock.now() - 95 * 60)
        self.desktop.age = 95.0
        runner.watch("one", record, log_path)
        self.assertTrue(any("looks hung" in line for line in self.alerts(plan)))

        plan2 = make_plan(jobs=[{"name": "two", "args": list(ARGS)}], name="test_plan_two")
        runner2 = self.runner(plan2, FakePopen(self.clock, []))
        runner2.folder.mkdir(parents=True, exist_ok=True)
        runner2.log_growth.start(0, self.clock.now() - 95 * 60)
        self.desktop.age = 40.0          # stale, but not for 90 minutes
        runner2.watch("two", record, log_path)
        self.assertFalse(any("looks hung" in line for line in self.alerts(plan2)))

    def test_status_file_is_rewritten_with_the_worker_and_queue(self):
        plan = make_plan()
        runner = self.runner(plan, FakePopen(self.clock, []))
        self.desktop.queue = {"pending": 2, "in_progress": 1}
        runner.load_state()
        runner.check_problems()
        runner.write_status("waiting", waiting=["x"])
        status = json.loads((runner.folder / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "waiting")
        self.assertEqual(status["queue"], {"pending": 2, "in_progress": 1})
        self.assertEqual(status["worker"]["heartbeat_age_min"], 1.0)
        self.assertEqual(status["jobs"]["total"], 1)


class DuringAJob(RunnerFixture):
    def test_stop_during_a_job_lets_it_finish_and_starts_nothing_else(self):
        plan = make_plan(jobs=[{"name": "first", "args": list(ARGS)}, {"name": "second", "args": list(ARGS)}])
        folder = self.tmp / "sweeps" / plan.name

        def ask_to_stop(proc):
            (folder / "STOP").write_text("stop")

        popen = FakePopen(self.clock, [{"end_after": 300, "rc": 0, "events": [(1, self.created_run("run_s")),
                                                                              (200, ask_to_stop)],
                                        "on_end": self.finish_run("run_s", s21=varying_s21())}])
        code = self.runner(plan, popen).run()
        self.assertEqual(code, 0)
        self.assertEqual(len(popen.calls), 1)
        jobs = self.state(plan)["jobs"]
        self.assertEqual(jobs["first"]["status"], "done")
        self.assertEqual(jobs["second"]["status"], "pending")
        status = json.loads((folder / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "stopped")
        self.assertTrue(status["stop_file"])

    def test_a_locked_screen_during_a_job_makes_its_failure_a_resume(self):
        plan = make_plan()
        self.desktop.locked = False

        def lock_then_fail(proc):
            self.desktop.locked = True
            proc.append("2026-10-21 14:00:00,000 INFO: Created run run_lk with 1 combination(s)")

        def unlock(clock):
            if clock.sleeps == 30:
                self.desktop.locked = False

        self.clock.on_sleep = unlock
        popen = FakePopen(self.clock, [
            {"end_after": 200, "rc": 1, "events": [(70, lock_then_fail)]},
            {"end_after": 60, "rc": 0, "on_end": self.finish_run("run_lk", s21=varying_s21())},
        ])
        self.runner(plan, popen).run()
        self.assertEqual(len(popen.calls), 2)
        self.assertIn("--resume", popen.calls[1])
        self.assertEqual(self.state(plan)["jobs"]["one"]["status"], "done")

    def test_repeated_interruptions_give_up_as_a_failure(self):
        plan = make_plan()
        scripts = [{"end_after": 30, "rc": 1,
                    "events": [(1, lambda p: p.append("ERROR: OpenDoc6 reported error code 65536"))]}
                   for _ in range(sr.MAX_INTERRUPTIONS + 1)]
        popen = FakePopen(self.clock, scripts)
        self.runner(plan, popen).run()
        self.assertEqual(len(popen.calls), sr.MAX_INTERRUPTIONS + 1)
        job = self.state(plan)["jobs"]["one"]
        self.assertEqual(job["status"], "failed")
        self.assertIn("gave up", job["reason"])


class Control(RunnerFixture):
    def test_the_single_runner_lock_refuses_a_second_runner(self):
        folder = self.tmp / "lockdir"
        folder.mkdir()
        lock = folder / "runner.lock"
        acquired, holder = sr.acquire_lock(lock, self.desktop)
        self.assertTrue(acquired)
        self.desktop.alive[os.getpid()] = True
        second, owner = sr.acquire_lock(lock, self.desktop)
        self.assertFalse(second)
        self.assertEqual(owner["pid"], os.getpid())
        sr.release_lock(lock)
        self.assertFalse(lock.exists())

    def test_dry_run_writes_nothing_and_warns_about_overrides(self):
        plan = make_plan(jobs=[
            {"name": "plain", "args": list(ARGS)},
            {"name": "tweaked", "args": ARGS + ["--max-passes", "10"]},
        ], preset="quick")
        folder = self.tmp / "sweeps" / plan.name
        out = []
        code = sr.dry_run(plan, folder, self.desktop, self.project, self.runs, out=out.append)
        self.assertEqual(code, 0)
        self.assertFalse(folder.exists())
        text = "\n".join(out)
        self.assertIn("would start a new run", text)
        self.assertIn("WARNING: overrides the preset for max_passes", text)
        self.assertIn("--preset quick", text)
        self.assertEqual(text.count("WARNING"), 1)     # only the job that overrides

    def test_a_bad_plan_path_is_refused_without_touching_anything(self):
        import contextlib
        with contextlib.redirect_stderr(StringIO()):
            self.assertEqual(sr.main([str(self.tmp / "missing.json")]), 2)


if __name__ == "__main__":
    unittest.main()
