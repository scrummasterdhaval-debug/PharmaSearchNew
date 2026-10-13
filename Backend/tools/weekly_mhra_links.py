"""Weekly: back up the store, then replace links to MHRA documents that no longer exist.

    python tools/weekly_mhra_links.py

MHRA deletes the old PDF whenever it revises a label, so stored UK links go
dead week by week. This is what the Windows scheduled task
"PharmaSearch weekly MHRA links" runs. Each run:

1. copies the database to backups/weekly-mhra-links-<timestamp>.db and keeps
   the newest four of those, so a bad run can always be undone;
2. replaces links to deleted MHRA PDFs with the licence's current document;
3. appends what it did to logs/weekly_mhra_links.log.

It is safe while the server is running: SQLite waits for the server's writes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import traceback
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from config import DB_PATH  # noqa: E402

# On a server these live on the mounted disk, not inside the container.
BACKUP_DIR = Path(os.getenv("PHARMASEARCH_BACKUP_DIR") or (BACKEND / "backups"))
LOG_PATH = Path(os.getenv("PHARMASEARCH_LOG_DIR") or (BACKEND / "logs")) / "weekly_mhra_links.log"
BACKUPS_KEPT = 4
BACKUP_PREFIX = "weekly-mhra-links-"


def log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def back_up(db_path: Path = DB_PATH, backup_dir: Path = BACKUP_DIR, kept: int = BACKUPS_KEPT) -> Path:
    """A consistent copy of the store, taken through SQLite, and old copies pruned."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{BACKUP_PREFIX}{datetime.now():%Y%m%d-%H%M%S}.db"
    source = sqlite3.connect(db_path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    for old in sorted(backup_dir.glob(f"{BACKUP_PREFIX}*.db"))[:-kept]:
        old.unlink()
    return target


def main() -> int:
    try:
        backup = back_up()
        log(f"backup {backup.name}")

        from services.data_repairs import relink_dead_mhra_documents

        result = relink_dead_mhra_documents()
        log("done " + json.dumps(result))
        return 0
    except Exception:  # the log is the only place a scheduled run can report
        log("FAILED\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
