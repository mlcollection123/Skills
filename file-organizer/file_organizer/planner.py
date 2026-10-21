"""Turns the index into a sorting plan and writes the dry-run preview report."""
from __future__ import annotations

import csv
import os
import re
import sqlite3
import time

from .common import Reporter, human_size, win_path
from .config import CATEGORY_DOCUMENTS, CATEGORY_EXEC, KEYSET_BATCH
from . import photos
from .db import iter_batches

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _clean(name: str, limit: int = 60) -> str:
    return _BAD.sub("_", name).strip(" .")[:limit] or "Other"


def target_subdir(category: str, subcategory: str | None, ext: str, is_dup: bool, segregate: bool,
                  path: str = "", taken: float | None = None, trip: str | None = None,
                  project: str | None = None, role: str | None = None, status: str | None = None,
                  subgroup: str | None = None) -> str:
    if project:
        parts = ["Projects", _clean(project), _clean(subgroup or role or "Other")]
        if status == "draft":
            parts.append("Drafts")
        if is_dup and segregate:
            parts.insert(0, "_Duplicates")
        return os.path.join(*parts)
    if taken is not None and photos.is_photo(category, ext):
        rel = photos.photo_subdir(path, taken, trip)
        return os.path.join("_Duplicates", rel) if (is_dup and segregate) else rel
    if category == CATEGORY_DOCUMENTS:
        leaf = subcategory or "General"
    else:
        leaf = ext[1:].upper() if ext else "NO_EXTENSION"
    leaf = _BAD.sub("_", leaf).strip(" .")[:32] or "NO_EXTENSION"
    parts = [category, leaf]
    if is_dup and segregate:
        parts.insert(0, "_Duplicates")
    return os.path.join(*parts)


def _insert_unique(conn, run_id, file_id, src, dest_root, rel_dir, size, mtime) -> str:
    """Insert a plan row with a destination name that is unique in this plan and on disk."""
    name = os.path.basename(src)
    stem, ext = os.path.splitext(name)
    n = 0
    while True:
        candidate = name if n == 0 else f"{stem} ({n}){ext}"
        dst = os.path.join(dest_root, rel_dir, candidate)
        if not os.path.lexists(win_path(dst)):
            try:
                conn.execute(
                    "INSERT INTO plan(run_id, file_id, src, dst, dst_key, size, mtime, status) "
                    "VALUES (?,?,?,?,?,?,?, 'planned')",
                    (run_id, file_id, src, dst, dst.lower(), size, mtime))
                return dst
            except sqlite3.IntegrityError:
                pass  # name already taken by another planned file: try the next suffix
        n += 1


def build_plan(conn, run_id: int, dest: str, include_exec: bool, segregate_dups: bool,
               rep: Reporter, smart_photos: bool = True) -> dict:
    conn.execute("DELETE FROM plan WHERE run_id=?", (run_id,))
    conn.commit()
    dest_root = os.path.abspath(dest)
    total = conn.execute("SELECT COUNT(*) FROM files WHERE run_id=?", (run_id,)).fetchone()[0]
    sql = ("SELECT id, path, ext, size, mtime, category, subcategory, is_dup, taken, trip, "
           "project, role, status, subgroup FROM files "
           "WHERE run_id=? AND id>? ORDER BY id LIMIT ?")
    done = planned = skipped = 0
    for rows in iter_batches(conn, sql, (run_id,), KEYSET_BATCH):
        rep.check()
        for (fid, path, ext, size, mtime, cat, sub, is_dup, taken, trip,
                 project, role, status, subgroup) in rows:
            if cat == CATEGORY_EXEC and not include_exec:
                conn.execute(
                    "INSERT INTO plan(run_id, file_id, src, size, mtime, status, reason) "
                    "VALUES (?,?,?,?,?, 'skipped', ?)",
                    (run_id, fid, path, size, mtime,
                     "executable/system file: flagged for cleanup review, left in place"))
                skipped += 1
            else:
                rel = target_subdir(cat, sub, ext, bool(is_dup), segregate_dups, path=path,
                                    taken=taken if smart_photos else None, trip=trip,
                                    project=project, role=role, status=status, subgroup=subgroup)
                _insert_unique(conn, run_id, fid, path, dest_root, rel, size, mtime)
                planned += 1
        conn.commit()
        done += len(rows)
        rep.progress("Building plan", done, total)
    rep.progress("Building plan", done, total, force=True)
    return {"planned": planned, "skipped": skipped}


def summarize(conn, run_id: int) -> dict:
    by_cat = conn.execute(
        "SELECT category, COUNT(*), COALESCE(SUM(size),0) FROM files WHERE run_id=? GROUP BY category "
        "ORDER BY 3 DESC", (run_id,)).fetchall()
    by_sub = conn.execute(
        "SELECT subcategory, COUNT(*) FROM files WHERE run_id=? AND subcategory IS NOT NULL "
        "GROUP BY subcategory ORDER BY 2 DESC", (run_id,)).fetchall()
    dup_n, dup_b = conn.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files "
                                "WHERE run_id=? AND is_dup=1", (run_id,)).fetchone()
    clean_n, clean_b = conn.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files "
                                    "WHERE run_id=? AND cleanup=1", (run_id,)).fetchone()
    plan = dict(conn.execute("SELECT status, COUNT(*) FROM plan WHERE run_id=? GROUP BY status",
                             (run_id,)).fetchall())
    plan_bytes = conn.execute("SELECT COALESCE(SUM(size),0) FROM plan WHERE run_id=? AND status='planned'",
                              (run_id,)).fetchone()[0]
    trips = conn.execute("SELECT trip, COUNT(*) FROM files WHERE run_id=? AND trip IS NOT NULL "
                         "GROUP BY trip ORDER BY 2 DESC LIMIT 25", (run_id,)).fetchall()
    projects = conn.execute("SELECT project, COUNT(*), COALESCE(SUM(size),0) FROM files WHERE run_id=? AND "
                            "project IS NOT NULL GROUP BY project ORDER BY 2 DESC LIMIT 40",
                            (run_id,)).fetchall()
    return {"projects": projects, "trips": trips, "by_category": by_cat, "by_subcategory": by_sub, "duplicates": (dup_n, dup_b),
            "cleanup": (clean_n, clean_b), "plan": plan, "plan_bytes": plan_bytes}


def write_report(conn, run_id: int, report_dir: str, source: str, dest: str) -> tuple[str, str]:
    """Stream the plan to a CSV (opens in Excel) and write a short text summary."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(report_dir, f"dryrun_{run_id}_{stamp}.csv")
    txt_path = os.path.join(report_dir, f"dryrun_{run_id}_{stamp}_summary.txt")
    sql = ("SELECT p.id, p.status, f.category, f.subcategory, f.is_dup, f.cleanup, p.size, p.src, "
           "p.dst, p.reason, f.project, f.role, f.reason FROM plan p JOIN files f ON f.id = p.file_id "
           "WHERE p.run_id=? AND p.id>? ORDER BY p.id LIMIT ?")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["plan_id", "status", "category", "subcategory", "duplicate", "cleanup_flag",
                    "size_bytes", "source", "destination", "note", "project", "role", "why_grouped"])
        for rows in iter_batches(conn, sql, (run_id,), KEYSET_BATCH):
            w.writerows(rows)
    s = summarize(conn, run_id)
    lines = [f"DRY RUN #{run_id}  (nothing has been copied, moved or linked)",
             f"Source:      {source}", f"Destination: {dest}", "", "Files by category:"]
    for cat, n, b in s["by_category"]:
        lines.append(f"  {cat:<20} {n:>9,} files  {human_size(b):>10}")
    if s["by_subcategory"]:
        lines += ["", "Document sub-categories (from names and text):"]
        lines += [f"  {sub:<20} {n:>9,}" for sub, n in s["by_subcategory"]]
    if s["projects"]:
        lines += ["", "Projects found (files grouped by what they belong to):"]
        lines += [f"  {p:<32} {n:>7,} files  {human_size(b):>10}" for p, n, b in s["projects"]]
    if s["trips"]:
        lines += ["", "Photo trips detected (from GPS + dates):"]
        lines += [f"  {t:<30} {n:>7,} photos" for t, n in s["trips"]]
    lines += ["", f"Duplicate files: {s['duplicates'][0]:,}  (wasted {human_size(s['duplicates'][1])})",
              f"Executables/system flagged for cleanup: {s['cleanup'][0]:,}  ({human_size(s['cleanup'][1])})",
              f"Plan: {s['plan'].get('planned', 0):,} to sort ({human_size(s['plan_bytes'])}), "
              f"{s['plan'].get('skipped', 0):,} left in place", "", f"Full preview: {csv_path}"]
    with open(txt_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return csv_path, txt_path
