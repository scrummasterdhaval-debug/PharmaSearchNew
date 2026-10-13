"""Admin-triggered background work, and the handle needed to stop it.

A harvest runs for a long time, so the route that starts one must return
immediately and leave something behind that reports progress and can be
cancelled. That handle is a Task, kept in memory for the life of the process.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.logging_config import get_logger
from services.evidence_backfill import backfill_evidence
from services.harvest import run_harvest


logger = get_logger(__name__)

# Finished tasks stay visible for a while so an operator can read the outcome
# of a run that ended while they were away.
_MAX_TASKS = 40

STATUS_RUNNING = "running"
STATUS_FINISHED = "finished"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Task:
    task_id: str
    kind: str
    label: str
    started_by: str
    started_at: str
    status: str = STATUS_RUNNING
    finished_at: str = ""
    rows: int = 0
    pairs_done: int = 0
    last_source: str = ""
    last_molecule: str = ""
    error: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    stop_event: threading.Event = field(default_factory=threading.Event)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "label": self.label,
            "started_by": self.started_by,
            "started_at": self.started_at,
            "status": self.status,
            "finished_at": self.finished_at,
            "rows": self.rows,
            "pairs_done": self.pairs_done,
            "last_source": self.last_source,
            "last_molecule": self.last_molecule,
            "error": self.error,
            "result": self.result,
            "cancelling": self.stop_event.is_set() and self.status == STATUS_RUNNING,
        }


_tasks: dict[str, Task] = {}
_lock = threading.Lock()


def _trim() -> None:
    if len(_tasks) <= _MAX_TASKS:
        return
    finished = sorted(
        (task for task in _tasks.values() if task.status != STATUS_RUNNING),
        key=lambda task: task.finished_at or task.started_at,
    )
    for task in finished[: len(_tasks) - _MAX_TASKS]:
        _tasks.pop(task.task_id, None)


def list_tasks() -> list[dict[str, Any]]:
    with _lock:
        tasks = [task.to_dict() for task in _tasks.values()]
    running = [task for task in tasks if task["status"] == STATUS_RUNNING]
    rest = [task for task in tasks if task["status"] != STATUS_RUNNING]
    running.sort(key=lambda task: task["started_at"], reverse=True)
    rest.sort(key=lambda task: task["finished_at"] or task["started_at"], reverse=True)
    return running + rest


def get_task(task_id: str) -> dict[str, Any] | None:
    with _lock:
        task = _tasks.get(task_id)
    return task.to_dict() if task else None


def running_count() -> int:
    with _lock:
        return sum(1 for task in _tasks.values() if task.status == STATUS_RUNNING)


def cancel_task(task_id: str) -> bool:
    """Ask a task to stop. It finishes the molecule in flight, then unwinds."""
    with _lock:
        task = _tasks.get(task_id)
        if not task or task.status != STATUS_RUNNING:
            return False
        task.stop_event.set()
    logger.info("Admin cancelled task %s", task_id)
    return True


def start_harvest(
    sources: list[str] | None,
    molecules: list[str] | None,
    molecule_limit: int | None,
    resume: bool,
    started_by: str,
) -> str:
    """Run a harvest on a daemon thread and return the handle that watches it."""
    task_id = uuid.uuid4().hex[:12]
    scope = ", ".join(sources) if sources else "every enabled source"
    across = f"{len(molecules)} molecule(s)" if molecules else (
        f"first {molecule_limit} molecules" if molecule_limit else "the full vocabulary"
    )
    task = Task(
        task_id=task_id,
        kind="harvest",
        label=f"{scope} across {across}",
        started_by=started_by,
        started_at=_now(),
    )
    with _lock:
        _tasks[task_id] = task
        _trim()

    def on_progress(source: str, molecule: str, stored: int) -> None:
        with _lock:
            task.pairs_done += 1
            task.rows += stored
            task.last_source = source
            task.last_molecule = molecule

    def worker() -> None:
        try:
            result = run_harvest(
                sources=sources,
                molecule_limit=molecule_limit,
                molecules=molecules,
                resume=resume,
                stop_event=task.stop_event,
                on_progress=on_progress,
            )
            with _lock:
                task.result = result.to_dict()
                task.rows = result.rows
                task.status = STATUS_CANCELLED if task.stop_event.is_set() else STATUS_FINISHED
        except Exception as exc:  # a failed run must still report why
            logger.exception("Admin harvest %s failed", task_id)
            with _lock:
                task.status = STATUS_FAILED
                task.error = str(exc)[:1000]
        finally:
            with _lock:
                task.finished_at = _now()

    threading.Thread(target=worker, name=f"admin-harvest-{task_id}", daemon=True).start()
    logger.info("Admin %s started harvest %s (%s)", started_by, task_id, task.label)
    return task_id


def start_evidence_backfill(started_by: str) -> str:
    """Reconstruct provenance for stored rows that were saved without it.

    Rows saved now record their own, so this is only needed for what was
    harvested before that -- and for a database restored from an older export.
    """
    task_id = uuid.uuid4().hex[:12]
    task = Task(
        task_id=task_id,
        kind="evidence-backfill",
        label="Evidence for rows saved without it",
        started_by=started_by,
        started_at=_now(),
    )
    with _lock:
        _tasks[task_id] = task
        _trim()

    def on_progress(scanned: int, total: int) -> None:
        with _lock:
            task.pairs_done = scanned
            task.last_source = f"{scanned}/{total} rows"

    def worker() -> None:
        try:
            result = backfill_evidence(progress=on_progress)
            with _lock:
                task.result = result
                task.rows = result["assertions_written"]
                task.status = STATUS_FINISHED
        except Exception as exc:  # a failed run must still report why
            logger.exception("Admin evidence backfill %s failed", task_id)
            with _lock:
                task.status = STATUS_FAILED
                task.error = str(exc)[:1000]
        finally:
            with _lock:
                task.finished_at = _now()

    threading.Thread(target=worker, name=f"admin-backfill-{task_id}", daemon=True).start()
    logger.info("Admin %s started evidence backfill %s", started_by, task_id)
    return task_id
