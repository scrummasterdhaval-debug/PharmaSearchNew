"""One clock for the three jobs that keep the data current.

    python tools/scheduled_jobs.py            # run until stopped
    python tools/scheduled_jobs.py --status   # when each job last ran and is next due
    python tools/scheduled_jobs.py --run registers   # run one job now and stop

    Every day 03:00   registers     download any register past its own cadence
    Sunday    02:00   live-sources  re-ask the live regulators about stored molecules
    Monday    10:00   mhra-links    back up, then repair links MHRA has deleted

This exists for the server, where there is no Task Scheduler: the container
runs this one process instead of three cron-ish loops. On Windows the same
three jobs are registered with Task Scheduler and this file is not used.

When a job last ran is kept in a small JSON file beside the logs, so a restart
neither repeats a job it has already done today nor skips one it missed -- a
job whose moment passed while the machine was off runs at the next check.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from tools import refresh_live_sources, refresh_registers, weekly_mhra_links  # noqa: E402

LOG_DIR = Path(os.getenv("PHARMASEARCH_LOG_DIR") or (BACKEND / "logs"))
LOG_PATH = LOG_DIR / "scheduled_jobs.log"
STATE_PATH = LOG_DIR / "scheduled_jobs.json"
CHECK_SECONDS = 300


@dataclass(frozen=True)
class Job:
    """A job, the moment it runs, and what it runs.

    ``weekday`` is Monday 0 .. Sunday 6, or None for every day.
    """

    name: str
    hour: int
    minute: int
    run: Callable[[], int]
    weekday: int | None = None

    @property
    def when(self) -> str:
        days = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
        day = "every day" if self.weekday is None else days[self.weekday]
        return f"{day} {self.hour:02d}:{self.minute:02d}"


JOBS: tuple[Job, ...] = (
    # Registers first, and early: it is the job that brings in new data, and
    # the other two are cheaper if it has already run.
    Job("registers", 3, 0, lambda: refresh_registers.main([])),
    Job("live-sources", 2, 0, lambda: refresh_live_sources.main(["--molecules", "60", "--minutes", "300"]), weekday=6),
    Job("mhra-links", 10, 0, weekly_mhra_links.main, weekday=0),
)


def log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def read_state(path: Path = STATE_PATH) -> dict[str, str]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(state: dict[str, str], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


def last_occurrence(job: Job, now: datetime) -> datetime:
    """The most recent moment this job was supposed to run, at or before now."""
    moment = now.replace(hour=job.hour, minute=job.minute, second=0, microsecond=0)
    if job.weekday is None:
        return moment if moment <= now else moment - timedelta(days=1)
    behind = (now.weekday() - job.weekday) % 7
    moment -= timedelta(days=behind)
    return moment if moment <= now else moment - timedelta(days=7)


def is_due(job: Job, now: datetime, state: dict[str, str]) -> bool:
    """True when this job's moment has passed and it has not run since.

    A job that has never run is due at once rather than at its next moment:
    a new server should not wait six days for Sunday to fill its store.
    """
    stamp = state.get(job.name)
    if not stamp:
        return True
    try:
        last_run = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    return last_run < last_occurrence(job, now)


def due_jobs(now: datetime, state: dict[str, str]) -> list[Job]:
    return [job for job in JOBS if is_due(job, now, state)]


def run_job(job: Job, state: dict[str, str]) -> int:
    log(f"{job.name}: starting")
    started = time.time()
    try:
        code = int(job.run() or 0)
    except Exception:
        code = 1
        log(f"{job.name}: FAILED\n" + traceback.format_exc())
    minutes = (time.time() - started) / 60
    log(f"{job.name}: finished in {minutes:.0f} min with exit code {code}")
    # Recorded whether or not it succeeded: a job that fails every run must not
    # be retried in a loop every five minutes against the same regulator.
    state[job.name] = datetime.now().isoformat(timespec="seconds")
    write_state(state)
    return code


def status_text(now: datetime | None = None) -> str:
    now = now or datetime.now()
    state = read_state()
    lines = []
    for job in JOBS:
        last = state.get(job.name, "never")
        lines.append(f"{job.name:<14} {job.when:<20} last run {last:<20} {'DUE' if is_due(job, now, state) else ''}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true", help="Print when each job last ran and exit")
    parser.add_argument("--run", metavar="JOB", default=None, help="Run one job now: " + ", ".join(job.name for job in JOBS))
    parser.add_argument("--once", action="store_true", help="Run whatever is due now, then stop")
    args = parser.parse_args(argv)

    if args.status:
        print(status_text())
        return 0

    if args.run:
        job = next((item for item in JOBS if item.name == args.run), None)
        if job is None:
            raise SystemExit("No such job: " + args.run + "\nKnown jobs: " + ", ".join(item.name for item in JOBS))
        return run_job(job, read_state())

    log("scheduler started: " + "; ".join(f"{job.name} {job.when}" for job in JOBS))
    while True:
        state = read_state()
        for job in due_jobs(datetime.now(), state):
            run_job(job, state)
        if args.once:
            return 0
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
