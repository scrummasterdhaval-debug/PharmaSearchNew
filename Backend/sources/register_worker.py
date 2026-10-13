"""Build one register in a process of its own.

    python -m sources.register_worker "DMP Norway"

Started by open_registers when PHARMASEARCH_REGISTER_PROCESS=child. The
website stays in the parent; if this process needs more memory than the
server has, it is the one stopped, and the site carries on.
"""
from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    # Ask the kernel to stop this process, not the website, when memory runs
    # out. Raising one's own score needs no privilege; elsewhere it is absent.
    try:
        with open("/proc/self/oom_score_adj", "w", encoding="ascii") as handle:
            handle.write("1000")
    except OSError:
        pass

    import sources.source_registry  # noqa: F401  every connector registers its register on import
    from sources.open_registers import REGISTERS, refresh_register

    register = REGISTERS.get(argv[0])
    if register is None:
        print(f"No register named {argv[0]!r}", file=sys.stderr)
        return 2
    return 0 if refresh_register(register) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
