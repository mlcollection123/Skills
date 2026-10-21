"""Applies a dry-run plan (copy / move / symlink) with a crash-safe journal, and undoes it."""
from __future__ import annotations

import errno
import json
import os
import shutil
import time

from . import db
from .common import Cancelled, Reporter, human_size, is_inside, win_path
from .config import EXEC_BATCH
from .fsutil import full_hash, is_cross_device

PART_SUFFIX = ".fo_part"
MODES = ("copy", "move", "symlink")


class SymlinkPrivilegeError(RuntimeError):
    pass


# --------------------------------------------------------------------- single-file operations
def _ensure_parent(dst: str) -> None:
    os.makedirs(win_path(os.path.dirname(dst)), exist_ok=True)


def _copy_verified(src: str, dst: str, verify: bool, cancel) -> None:
    """Copy to a temporary name, verify, then rename into place (never a half-written dst)."""
    if os.path.lexists(win_path(dst)):
        raise FileExistsError(dst)
    _ensure_parent(dst)
    part = dst + PART_SUFFIX
    try:
        shutil.copyfile(win_path(src), win_path(part))
        try:
            shutil.copystat(win_path(src), win_path(part))
        except OSError:
            pass  # timestamps are nice to have, not essential
        if os.path.getsize(win_path(part)) != os.path.getsize(win_path(src)):
            raise OSError("size mismatch after copy")
        if verify and full_hash(src, "sha256", cancel) != full_hash(part, "sha256", cancel):
            raise OSError("hash mismatch after copy")
        os.rename(win_path(part), win_path(dst))
    except BaseException:
        try:
            os.remove(win_path(part))
        except OSError:
            pass
        raise


def _move_file(src: str, dst: str, verify: bool, cancel) -> None:
    if os.path.lexists(win_path(dst)):
        raise FileExistsError(dst)
    _ensure_parent(dst)
    try:
        os.rename(win_path(src), win_path(dst))  # same volume: instant, atomic
        return
    except OSError as e:
        if not is_cross_device(e):
            raise
    _copy_verified(src, dst, verify, cancel)   # other drive: copy, verify, only then delete
    os.remove(win_path(src))


def _make_symlink(src: str, dst: str) -> None:
    if os.path.lexists(win_path(dst)):
        raise FileExistsError(dst)
    _ensure_parent(dst)
    try:
        os.symlink(os.path.abspath(src), win_path(dst))
    except OSError as e:
        if getattr(e, "winerror", None) == 1314:
            raise SymlinkPrivilegeError(_symlink_help()) from e
        raise


def _symlink_help() -> str:
    return ("Windows refused to create a symbolic link (error 1314). Either turn on Developer Mode "
            "(Settings > Privacy & security > For developers) or run this program as Administrator, "
            "then try again. Nothing was changed.")


def _check_symlink_allowed(dest: str) -> None:
    os.makedirs(win_path(dest), exist_ok=True)
    probe = os.path.join(dest, ".fo_symlink_probe")
    try:
        if os.path.lexists(win_path(probe)):
            os.remove(win_path(probe))
        os.symlink(os.path.abspath(dest), win_path(probe), target_is_directory=True)
    except OSError as e:
        if getattr(e, "winerror", None) == 1314:
            raise SymlinkPrivilegeError(_symlink_help()) from e
        raise
    finally:
        try:
            os.remove(win_path(probe))
        except OSError:
            pass


# --------------------------------------------------------------------------------- execute
def execute_plan(conn, plan_run: int, mode: str, verify: bool, rep: Reporter) -> dict:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    run = conn.execute("SELECT source, dest FROM runs WHERE id=?", (plan_run,)).fetchone()
    source, dest = run
    total, total_bytes = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(size),0) FROM plan WHERE run_id=? AND status='planned'",
        (plan_run,)).fetchone()
    if total == 0:
        raise RuntimeError("The plan has nothing left to do. Run a new dry run.")

    os.makedirs(win_path(dest), exist_ok=True)
    if mode == "copy":
        free = shutil.disk_usage(dest).free
        if free < total_bytes * 1.02:
            raise RuntimeError(f"Not enough free space in the destination: need "
                               f"{human_size(total_bytes)}, have {human_size(free)}. Nothing was changed.")
    if mode == "symlink":
        _check_symlink_allowed(dest)

    exec_id = db.new_run(conn, "execute", source, dest,
                         {"mode": mode, "verify": verify, "plan_run": plan_run})
    rep.log(f"Executing {total:,} files ({human_size(total_bytes)}) in '{mode}' mode. Run #{exec_id}. "
            f"Every action is journaled, so 'Undo Last Run' can reverse it.")
    stats = {"done": 0, "failed": 0, "changed": 0, "bytes": 0}
    consecutive_fail = 0
    processed = 0
    last = 0
    status = "complete"
    try:
        while True:
            rows = conn.execute(
                "SELECT id, src, dst, size, mtime FROM plan WHERE run_id=? AND status='planned' "
                "AND id>? ORDER BY id LIMIT ?", (plan_run, last, EXEC_BATCH)).fetchall()
            if not rows:
                break
            last = rows[-1][0]
            rep.check()
            todo = []
            for pid, src, dst, size, mtime in rows:
                try:
                    st = os.stat(win_path(src))
                    unchanged = st.st_size == size and abs(st.st_mtime - mtime) <= 2
                except OSError:
                    unchanged = False
                if not unchanged:
                    conn.execute("UPDATE plan SET status='changed', reason=? WHERE id=?",
                                 ("source missing or modified since the dry run; skipped", pid))
                    stats["changed"] += 1
                else:
                    todo.append((pid, src, dst, size))
            # Write intent first, commit, then touch the disk: a crash leaves 'pending' rows
            # that Undo knows how to resolve.
            jids = []
            for pid, src, dst, size in todo:
                cur = conn.execute(
                    "INSERT INTO journal(exec_id, plan_id, op, src, dst, size, status, ts) "
                    "VALUES (?,?,?,?,?,?, 'pending', ?)", (exec_id, pid, mode, src, dst, size, time.time()))
                jids.append(cur.lastrowid)
            conn.commit()
            try:
                for (pid, src, dst, size), jid in zip(todo, jids):
                    rep.check()
                    try:
                        if mode == "copy":
                            _copy_verified(src, dst, verify, rep.cancel)
                        elif mode == "move":
                            _move_file(src, dst, verify, rep.cancel)
                        else:
                            _make_symlink(src, dst)
                        conn.execute("UPDATE journal SET status='done' WHERE id=?", (jid,))
                        conn.execute("UPDATE plan SET status='done' WHERE id=?", (pid,))
                        stats["done"] += 1
                        stats["bytes"] += size
                        consecutive_fail = 0
                    except SymlinkPrivilegeError:
                        raise
                    except OSError as e:
                        conn.execute("UPDATE journal SET status='failed', error=? WHERE id=?", (str(e), jid))
                        conn.execute("UPDATE plan SET status='failed', reason=? WHERE id=?", (str(e), pid))
                        stats["failed"] += 1
                        consecutive_fail += 1
                        rep.log(f"[fail] {src}: {e}")
                        if e.errno == errno.ENOSPC:
                            raise RuntimeError("Destination disk is full. Stopped; use Undo to revert.") from e
                        if consecutive_fail >= 50:
                            raise RuntimeError("50 files in a row failed. Stopped; check the log.") from e
                    processed += 1
                    rep.progress("Executing", processed, total)
            except BaseException:
                conn.execute("UPDATE journal SET status='skipped' WHERE exec_id=? AND status='pending'",
                             (exec_id,))
                conn.commit()
                raise
            conn.commit()
    except Cancelled:
        status = "cancelled"
        rep.log("Cancelled. Files already processed stay as they are; use 'Undo Last Run' to revert them.")
    except BaseException:
        db.finish_run(conn, exec_id, "failed", stats)
        raise
    rep.progress("Executing", processed, total, force=True)
    db.finish_run(conn, exec_id, status, stats)
    stats["exec_id"] = exec_id
    stats["status"] = status
    return stats


# ------------------------------------------------------------------------------------ undo
def latest_undoable(conn) -> tuple | None:
    return conn.execute(
        "SELECT id, dest, options, status FROM runs WHERE kind='execute' AND status!='reverted' "
        "ORDER BY id DESC LIMIT 1").fetchone()


def undo_execution(conn, exec_id: int, rep: Reporter) -> dict:
    row = conn.execute("SELECT dest, options FROM runs WHERE id=? AND kind='execute'", (exec_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"Run #{exec_id} not found.")
    dest, options = row[0], json.loads(row[1] or "{}")
    plan_run = options.get("plan_run")
    total = conn.execute("SELECT COUNT(*) FROM journal WHERE exec_id=? AND status IN ('done','pending')",
                         (exec_id,)).fetchone()[0]
    rep.log(f"Undoing run #{exec_id} ({options.get('mode')}): {total:,} journal entries.")
    counts = {"reverted": 0, "nothing_to_undo": 0, "conflicts": 0, "errors": 0}
    parents: set[str] = set()
    last = 1 << 62
    processed = 0
    try:
        while True:
            rows = conn.execute(
                "SELECT id, plan_id, op, src, dst, size, status FROM journal WHERE exec_id=? "
                "AND status IN ('done','pending') AND id<? ORDER BY id DESC LIMIT ?",
                (exec_id, last, EXEC_BATCH)).fetchall()
            if not rows:
                break
            last = rows[-1][0]
            for jid, pid, op, src, dst, size, status in rows:
                rep.check()
                try:
                    outcome = _undo_one(op, src, dst, size, rep)
                except OSError as e:
                    counts["errors"] += 1
                    conn.execute("UPDATE journal SET error=? WHERE id=?", (f"undo failed: {e}", jid))
                    rep.log(f"[undo-fail] {dst}: {e}")
                    continue
                if outcome == "conflict":
                    counts["conflicts"] += 1
                    rep.log(f"[undo-skip] {dst}: both source and destination exist and differ; left alone")
                    continue
                counts["reverted" if outcome == "reverted" else "nothing_to_undo"] += 1
                conn.execute("UPDATE journal SET status='reverted' WHERE id=?", (jid,))
                if pid is not None:
                    conn.execute("UPDATE plan SET status='planned', reason=NULL WHERE id=?", (pid,))
                parents.add(os.path.dirname(dst))
                processed += 1
                rep.progress("Undoing", processed, total)
            conn.commit()
    except Cancelled:
        rep.log("Undo cancelled part-way. Run Undo again to continue.")
        conn.commit()
        return {**counts, "complete": False}
    _prune_empty_dirs(parents, dest)
    complete = counts["conflicts"] == 0 and counts["errors"] == 0
    if complete:
        db.finish_run(conn, exec_id, "reverted", counts)
    rep.progress("Undoing", processed, total, force=True)
    return {**counts, "complete": complete, "plan_run": plan_run}


def _undo_one(op: str, src: str, dst: str, size: int, rep: Reporter) -> str:
    """Reverse one journaled action. Returns 'reverted', 'nothing' or 'conflict'. Never deletes sources."""
    part = dst + PART_SUFFIX
    if os.path.lexists(win_path(part)):
        os.remove(win_path(part))
    if op == "symlink":
        if os.path.islink(win_path(dst)):
            os.remove(win_path(dst))
            return "reverted"
        return "nothing"
    if not os.path.lexists(win_path(dst)):
        return "nothing"                       # the action never happened
    if op == "copy":
        if os.path.getsize(win_path(dst)) == size:
            os.remove(win_path(dst))           # remove only the copy, the original is untouched
            return "reverted"
        return "conflict"
    if op == "move":
        if not os.path.lexists(win_path(src)):
            _move_file(dst, src, False, rep.cancel)   # put it back where it came from
            return "reverted"
        if os.path.getsize(win_path(src)) == os.path.getsize(win_path(dst)):
            os.remove(win_path(dst))           # cross-drive move that failed before deleting the source
            return "reverted"
        return "conflict"
    return "nothing"


def _prune_empty_dirs(dirs: set[str], dest_root: str) -> None:
    """Remove folders this program created (only if empty), never the destination root itself."""
    for d in sorted(dirs, key=len, reverse=True):
        cur = d
        while cur and is_inside(cur, dest_root) and os.path.normcase(os.path.abspath(cur)) != \
                os.path.normcase(os.path.abspath(dest_root)):
            try:
                os.rmdir(win_path(cur))
            except OSError:
                break  # not empty or in use: stop climbing
            cur = os.path.dirname(cur)
