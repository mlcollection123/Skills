"""Entry point.

    python -m file_organizer                      -> desktop UI
    python -m file_organizer dryrun SRC DEST      -> headless dry run
    python -m file_organizer execute SRC DEST --mode copy|move|symlink --yes
    python -m file_organizer undo
"""
from __future__ import annotations

import argparse
import sys

from . import engine
from .common import Reporter
from .config import db_path


def _print_sink(msg: tuple) -> None:
    if msg[0] == "log":
        print(msg[1], flush=True)
    elif msg[0] == "progress":
        _, phase, done, total = msg
        tail = f"/{total:,}" if total else ""
        print(f"\r{phase}: {done:,}{tail}   ", end="", flush=True)


def _opts(a) -> dict:
    return {"include_exec": a.include_exec, "segregate_dups": not a.no_segregate,
            "detect_dups": not a.no_dups, "read_content": not a.no_content,
            "hash_algo": a.hash, "workers": a.workers, "verify": a.verify}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="file_organizer")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("dryrun", "execute"):
        p = sub.add_parser(name)
        p.add_argument("source")
        p.add_argument("dest")
        p.add_argument("--include-exec", action="store_true")
        p.add_argument("--no-segregate", action="store_true")
        p.add_argument("--no-dups", action="store_true")
        p.add_argument("--no-content", action="store_true")
        p.add_argument("--hash", choices=["sha256", "md5"], default="sha256")
        p.add_argument("--workers", type=int, default=4)
        p.add_argument("--verify", action="store_true")
        if name == "execute":
            p.add_argument("--mode", choices=["copy", "move", "symlink"], required=True)
            p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    sub.add_parser("undo")
    a = ap.parse_args(argv)

    if a.cmd is None:
        from .gui import main as gui_main
        gui_main()
        return 0

    rep = Reporter(_print_sink)
    try:
        if a.cmd == "dryrun":
            engine.dry_run(db_path(), a.source, a.dest, _opts(a), rep)
        elif a.cmd == "execute":
            ov = engine.plan_overview(db_path(), a.source, a.dest)
            if ov is None:
                print("No completed dry run for this source/destination. Run 'dryrun' first.")
                return 2
            if not a.yes:
                ans = input(f"{a.mode.upper()} {ov['files']:,} files? [y/N] ")
                if ans.strip().lower() != "y":
                    return 1
            engine.execute(db_path(), a.source, a.dest, a.mode, _opts(a), rep)
        else:
            engine.undo(db_path(), rep)
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
