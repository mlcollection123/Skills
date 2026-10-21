"""High-level jobs run on the worker thread: dry run, execute, undo."""
from __future__ import annotations

import json
import os
import time

from . import analyzer, db, executor, photos, planner, projects, report, scanner
from .common import Cancelled, Reporter, human_size, is_inside, norm
from .config import HASH_WORKERS, report_dir

DEFAULT_OPTS = {
    "include_exec": False,     # move executables/system files too (default: flag and leave alone)
    "segregate_dups": True,    # send extra duplicate copies to <dest>/_Duplicates for review
    "detect_dups": True,
    "read_content": True,      # peek inside txt/md/csv/pdf/docx/xlsx/pptx for keywords
    "group_projects": True,    # link files that belong together into project folders (names, folders, text)
    "smart_photos": True,      # photo folders from EXIF date + GPS (trips, events, year-month)
    "my_names": "",            # comma separated names that must never become a project, e.g. "John Smith"
    "home_country": "",        # ISO code, e.g. "GB": photos taken at home are not called trips
    "hash_algo": "sha256",     # or "md5" (faster, fine for spotting duplicates)
    "workers": HASH_WORKERS,
    "verify": False,           # re-hash every copied file after copying (slow, thorough)
}


def with_defaults(opts: dict | None) -> dict:
    return {**DEFAULT_OPTS, **(opts or {})}


def validate_paths(src: str, dest: str) -> str | None:
    """Return an error message, or None if the pair is safe to use."""
    if not src or not os.path.isdir(src):
        return "Source directory does not exist."
    if not dest:
        return "Choose a destination directory."
    if is_inside(dest, src):
        return "The destination must not be inside the source (it would be re-scanned)."
    if is_inside(src, dest):
        return "The source must not be inside the destination."
    return None


def latest_ready_plan(conn, src: str, dest: str) -> int | None:
    """Most recent completed dry run for exactly this source and destination."""
    rows = conn.execute("SELECT id, source, dest FROM runs WHERE kind='dryrun' AND status='complete' "
                        "ORDER BY id DESC").fetchall()
    for rid, s, d in rows:
        if norm(s) == norm(src) and norm(d) == norm(dest):
            return rid
    return None


def plan_overview(db_path: str, src: str, dest: str) -> dict | None:
    conn = db.connect(db_path)
    try:
        rid = latest_ready_plan(conn, src, dest)
        if rid is None:
            return None
        n, b = conn.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM plan WHERE run_id=? "
                            "AND status='planned'", (rid,)).fetchone()
        return {"run_id": rid, "files": n, "bytes": b}
    finally:
        conn.close()


def dry_run(db_path: str, src: str, dest: str, opts: dict | None, rep: Reporter) -> dict:
    """Scan, classify, find duplicates and write a plan. Touches nothing on disk but the database
    and the report folder."""
    opts = with_defaults(opts)
    err = validate_paths(src, dest)
    if err:
        raise RuntimeError(err)
    conn = db.connect(db_path)
    run_id = db.new_run(conn, "dryrun", os.path.abspath(src), os.path.abspath(dest), opts)
    t0 = time.monotonic()
    try:
        rep.log(f"DRY RUN #{run_id}: nothing will be copied, moved or linked.")
        if os.path.dirname(os.path.abspath(src)) == os.path.abspath(src):
            rep.log("Note: the source is a drive root. System folders (Windows, Program Files, "
                    "$RECYCLE.BIN, ...) are skipped, but a smaller first run is wise.")
        rep.log(f"Scanning {src} ...")
        stats = scanner.scan_into_db(conn, run_id, src, rep)
        rep.log(f"Indexed {stats.files:,} files ({human_size(stats.bytes)}); {stats.errors} unreadable, "
                f"{stats.skipped_links} links skipped, {stats.skipped_cloud} cloud-only placeholders skipped.")
        if stats.files == 0:
            raise RuntimeError("No files found in the source directory.")
        if opts["smart_photos"]:
            ph = photos.analyze(conn, run_id, rep, opts["workers"], opts["home_country"])
            if ph["photos"]:
                rep.log(f"Photos: {ph['photos']:,} read, {ph['with_date']:,} with a date, "
                        f"{ph['with_gps']:,} with GPS, {len(ph['trips'])} trips found.")
        grouping = {"projects": 0, "grouped": 0}
        if opts["group_projects"]:
            idx = projects.index_files(conn, run_id, src, rep, opts["workers"], opts["read_content"],
                                      opts["my_names"])
            subs = idx["subcategories"]
            grouping = projects.discover(conn, run_id, idx["indexed"], rep)
            rep.log(f"Projects: {grouping['grouped']:,} files grouped into {grouping['projects']} project "
                    f"folders" + (f" ({grouping.get('ruled', 0):,} by your rules)" if grouping.get("ruled") else "")
                    + ".")
        else:
            subs = analyzer.classify_documents(conn, run_id, rep, opts["workers"], opts["read_content"])
        if subs:
            rep.log("Document sub-categories: " + ", ".join(f"{k}={v:,}" for k, v in subs.items()))
        dup = {"duplicate_files": 0, "duplicate_groups": 0, "wasted_bytes": 0}
        if opts["detect_dups"]:
            dup = analyzer.find_duplicates(conn, run_id, opts["hash_algo"], opts["workers"], rep)
            rep.log(f"Duplicates: {dup['duplicate_files']:,} extra copies in {dup['duplicate_groups']:,} "
                    f"groups, wasting {human_size(dup['wasted_bytes'])}.")
        counts = planner.build_plan(conn, run_id, dest, opts["include_exec"], opts["segregate_dups"], rep,
                                    opts["smart_photos"])
        csv_path, txt_path = planner.write_report(conn, run_id, report_dir(), src, dest)
        html_path = report.write_html(conn, run_id, os.path.splitext(csv_path)[0] + ".html")
        summary = {"files": stats.files, "bytes": stats.bytes, **dup, **counts,
                   "projects": grouping["projects"], "grouped": grouping["grouped"],
                   "report_csv": csv_path, "report_txt": txt_path, "report_html": html_path,
                   "seconds": round(time.monotonic() - t0, 1)}
        db.finish_run(conn, run_id, "complete", summary)
        _drop_old_dry_runs(conn, run_id, src, dest)
        with open(txt_path, encoding="utf-8") as fh:
            rep.log("\n" + fh.read())
        rep.log("Dry run complete. Review the report, then press 'Execute Sorting'.")
        return summary
    except Cancelled:
        db.finish_run(conn, run_id, "cancelled")
        raise
    except BaseException:
        db.finish_run(conn, run_id, "failed")
        raise
    finally:
        conn.close()


def _drop_old_dry_runs(conn, keep_id: int, src: str, dest: str) -> None:
    """Free database space: older dry-run data for the same folders is superseded."""
    old = [r[0] for r in conn.execute(
        "SELECT id FROM runs WHERE kind='dryrun' AND id<? AND source=? AND dest=?",
        (keep_id, os.path.abspath(src), os.path.abspath(dest))).fetchall()]
    for rid in old:
        conn.execute("DELETE FROM plan WHERE run_id=?", (rid,))
        conn.execute("DELETE FROM tokens WHERE file_id IN (SELECT id FROM files WHERE run_id=?)", (rid,))
        conn.execute("DELETE FROM files WHERE run_id=?", (rid,))
        conn.execute("UPDATE runs SET status='superseded' WHERE id=?", (rid,))
    conn.commit()


def execute(db_path: str, src: str, dest: str, mode: str, opts: dict | None, rep: Reporter) -> dict:
    opts = with_defaults(opts)
    err = validate_paths(src, dest)
    if err:
        raise RuntimeError(err)
    conn = db.connect(db_path)
    try:
        rid = latest_ready_plan(conn, src, dest)
        if rid is None:
            raise RuntimeError("No completed dry run for this source and destination. "
                               "Run a dry run first.")
        result = executor.execute_plan(conn, rid, mode, opts["verify"], rep)
        rep.log(f"Finished ({result['status']}): {result['done']:,} done, {result['failed']:,} failed, "
                f"{result['changed']:,} skipped because the source changed.")
        return result
    finally:
        conn.close()


def undo(db_path: str, rep: Reporter) -> dict:
    conn = db.connect(db_path)
    try:
        last = executor.latest_undoable(conn)
        if last is None:
            raise RuntimeError("There is no run to undo.")
        res = executor.undo_execution(conn, last[0], rep)
        rep.log(f"Undo: {res['reverted']:,} reverted, {res['nothing_to_undo']:,} already clean, "
                f"{res['conflicts']:,} conflicts, {res['errors']:,} errors.")
        return res
    finally:
        conn.close()
