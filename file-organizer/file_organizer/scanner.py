"""Streaming directory walk that writes file metadata to SQLite in batches."""
from __future__ import annotations

import os
from dataclasses import dataclass

from .common import Reporter, win_path
from .config import CATEGORY_EXEC, SCAN_BATCH, SKIP_DIR_NAMES, category_for
from .fsutil import guess_mime

FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
# Cloud placeholders (OneDrive "Files On-Demand" etc.): reading them would download them.
CLOUD_ONLY = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS


@dataclass
class ScanStats:
    files: int = 0
    bytes: int = 0
    errors: int = 0
    skipped_cloud: int = 0
    skipped_links: int = 0


def iter_files(root: str, rep: Reporter, stats: ScanStats, skip_dirs=SKIP_DIR_NAMES):
    """Yield one tuple per regular file, depth first, never holding the tree in memory.

    Symlinks, junctions and cloud-only placeholders are skipped on purpose.
    """
    stack = [os.path.abspath(root)]
    while stack:
        rep.check()
        d = stack.pop()
        try:
            it = os.scandir(win_path(d))
        except OSError as e:
            stats.errors += 1
            rep.log(f"[skip] cannot open folder {d}: {e}")
            continue
        with it:
            for entry in it:
                try:
                    if entry.is_symlink():
                        stats.skipped_links += 1
                        continue
                    st = entry.stat(follow_symlinks=False)
                    attrs = getattr(st, "st_file_attributes", 0)
                    if entry.is_dir(follow_symlinks=False):
                        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                            stats.skipped_links += 1
                        elif entry.name.lower() not in skip_dirs:
                            stack.append(os.path.join(d, entry.name))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if attrs & CLOUD_ONLY:
                        stats.skipped_cloud += 1
                        continue
                    ext = os.path.splitext(entry.name)[1].lower()
                    created = getattr(st, "st_birthtime", None) or st.st_ctime
                    category = category_for(ext)
                    yield (
                        os.path.join(d, entry.name), ext, st.st_size, created, st.st_mtime,
                        guess_mime(ext), category, 1 if category == CATEGORY_EXEC else 0,
                    )
                except OSError as e:
                    stats.errors += 1
                    rep.log(f"[skip] {os.path.join(d, entry.name)}: {e}")


def scan_into_db(conn, run_id: int, root: str, rep: Reporter) -> ScanStats:
    stats = ScanStats()
    sql = ("INSERT INTO files(run_id, path, ext, size, ctime, mtime, mime, category, cleanup) "
           "VALUES (?,?,?,?,?,?,?,?,?)")
    batch: list[tuple] = []
    for rec in iter_files(root, rep, stats):
        batch.append((run_id, *rec))
        stats.files += 1
        stats.bytes += rec[2]
        if len(batch) >= SCAN_BATCH:
            conn.executemany(sql, batch)
            conn.commit()
            batch.clear()
        rep.progress("Scanning", stats.files)
    if batch:
        conn.executemany(sql, batch)
        conn.commit()
    rep.progress("Scanning", stats.files, force=True)
    return stats
