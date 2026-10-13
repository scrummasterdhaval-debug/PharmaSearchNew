"""Daily: download every register again as often as its regulator republishes it.

    python tools/refresh_registers.py            # refresh whatever is due
    python tools/refresh_registers.py --status   # say how old each copy is
    python tools/refresh_registers.py --only "AIFA Italy" --force

Twelve regulators publish their whole register as a file, which this project
keeps as a local index (sources/open_registers). Until now a copy was only
replaced when someone searched it after it had gone stale, so a register
nobody searched for a month was a month out of date, and a download that
failed was noticed by nobody.

This is the job that fixes that. Run it daily. Each register declares how long
its copy stays good -- a week for most, a month for Brazil -- and only the ones
past that age are fetched, so a daily run costs nothing on the days nothing is
due. One register failing does not stop the others; the run ends non-zero and
names what failed, which is what a scheduled task reports on.

It is safe while the server is running: a new index is built beside the old
one and only swapped in when it is complete.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import sources.source_registry  # noqa: E402,F401  every connector registers its register on import
from sources.open_registers import (  # noqa: E402
    REGISTERS,
    OpenRegister,
    index_info,
    refresh_register,
)

LOG_PATH = Path(os.getenv("PHARMASEARCH_LOG_DIR") or (BACKEND / "logs")) / "refresh_registers.log"


def log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def age_seconds(register: OpenRegister) -> float | None:
    """How long ago this register was last downloaded; None if never."""
    info = index_info(register)
    if not info:
        return None
    try:
        return max(0.0, time.time() - float(info.get("fetched_epoch", 0)))
    except (TypeError, ValueError):
        return None


def is_due(register: OpenRegister, max_age_seconds: float | None = None) -> bool:
    """True when the copy is missing or older than the register's own cadence."""
    age = age_seconds(register)
    if age is None:
        return True
    limit = register.max_age_seconds if max_age_seconds is None else max_age_seconds
    return age > limit


def _row_count(register: OpenRegister) -> int:
    info = index_info(register) or {}
    try:
        return int(info.get("rows", 0))
    except (TypeError, ValueError):
        return 0


def _age_text(register: OpenRegister) -> str:
    age = age_seconds(register)
    if age is None:
        return "never downloaded"
    hours = age / 3600
    return f"{hours:.0f}h old" if hours < 48 else f"{hours / 24:.1f} days old"


def selected_registers(only: list[str] | None) -> list[OpenRegister]:
    """The registers named on the command line, or all of them, in a fixed order."""
    registers = sorted(REGISTERS.values(), key=lambda register: register.source.lower())
    if not only:
        return registers
    wanted = {name.strip().lower() for name in only}
    chosen = [register for register in registers if register.source.lower() in wanted]
    missing = wanted - {register.source.lower() for register in chosen}
    if missing:
        raise SystemExit(
            "No such register: " + ", ".join(sorted(missing))
            + "\nKnown registers: " + ", ".join(register.source for register in registers)
        )
    return chosen


def status_lines(only: list[str] | None = None) -> list[str]:
    lines = []
    for register in selected_registers(only):
        due = "DUE" if is_due(register) else "current"
        rows = _row_count(register)
        lines.append(
            f"{register.source:<22} {register.country:<16} {_age_text(register):>16}"
            f"  {rows:>8,} rows  every {register.max_age_seconds // 86400}d  {due}"
        )
    return lines


def refresh_due(
    only: list[str] | None = None,
    force: bool = False,
    max_age_seconds: float | None = None,
) -> tuple[list[str], list[str]]:
    """Refresh every due register in turn. Returns (refreshed, failed) source names.

    One at a time on purpose: several of these files are hundreds of megabytes,
    and a regulator's server is a shared resource, not a pipe to fill.
    """
    refreshed: list[str] = []
    failed: list[str] = []
    for register in selected_registers(only):
        if not (force or is_due(register, max_age_seconds)):
            log(f"skip {register.source} ({_age_text(register)})")
            continue
        started = time.time()
        try:
            path = refresh_register(register)
        except Exception:
            failed.append(register.source)
            log(f"FAILED {register.source}\n" + traceback.format_exc())
            continue
        if path is None:
            # refresh_register returns None when it is already running (the
            # server warms registers on start) or when the download failed; it
            # logs the reason itself.
            failed.append(register.source)
            log(f"FAILED {register.source} (no index written)")
            continue
        refreshed.append(register.source)
        log(f"ok {register.source} {_row_count(register):,} rows in {time.time() - started:.0f}s")
    return refreshed, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true", help="Print how old each copy is and exit")
    parser.add_argument("--only", nargs="*", default=None, help="Register names, e.g. --only \"AIFA Italy\"")
    parser.add_argument("--force", action="store_true", help="Refresh even copies that are still current")
    parser.add_argument("--max-age-days", type=float, default=None, help="Override every register's own cadence")
    args = parser.parse_args(argv)

    if args.status:
        print("\n".join(status_lines(args.only)))
        return 0

    max_age = None if args.max_age_days is None else args.max_age_days * 86400
    started = datetime.now(timezone.utc)
    log(f"run started ({'forced' if args.force else 'due only'})")
    refreshed, failed = refresh_due(args.only, args.force, max_age)
    minutes = (datetime.now(timezone.utc) - started).total_seconds() / 60
    summary = (
        f"run finished in {minutes:.0f} min: {len(refreshed)} refreshed"
        f"{', ' + str(len(failed)) + ' failed: ' + ', '.join(failed) if failed else ''}"
    )
    log(summary)
    print(summary)
    print("\n".join(status_lines(args.only)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
