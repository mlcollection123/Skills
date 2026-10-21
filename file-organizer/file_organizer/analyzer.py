"""Content-based sub-categorisation of documents and duplicate detection."""
from __future__ import annotations

import itertools
import logging
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor

from .common import Reporter, win_path
from .config import (
    CATEGORY_DOCUMENTS, KEYSET_BATCH, KEYWORD_RULES, MIN_TEXT_HITS, OOXML_MAX_BYTES,
    PDF_MAX_BYTES, PDF_MAX_PAGES, TEXT_READ_LIMIT,
)
from .db import iter_batches
from .fsutil import full_hash, partial_hash

try:  # optional dependency: searchable-PDF text
    import pypdf
    logging.getLogger("pypdf").setLevel(logging.CRITICAL)
except Exception:  # pragma: no cover
    pypdf = None

PLAIN_EXTS = {".txt", ".md", ".csv"}
OOXML_EXTS = {".docx", ".xlsx", ".pptx"}

_PATTERNS = {
    sub: re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b", re.IGNORECASE)
    for sub, words in KEYWORD_RULES.items()
}


# ---------------------------------------------------------------- document text -------------
def extract_text(path: str, ext: str, size: int) -> str | None:
    """Best-effort text from the start of a document; None if unsupported or unreadable."""
    try:
        if ext in PLAIN_EXTS:
            with open(win_path(path), "rb") as f:
                raw = f.read(TEXT_READ_LIMIT)
            if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                return raw.decode("utf-16", errors="ignore")
            return raw.decode("utf-8", errors="ignore")
        if ext == ".pdf":
            if pypdf is None or size > PDF_MAX_BYTES:
                return None
            with open(win_path(path), "rb") as f:
                reader = pypdf.PdfReader(f)
                if reader.is_encrypted:
                    return None
                parts, budget = [], TEXT_READ_LIMIT
                for page in itertools.islice(reader.pages, PDF_MAX_PAGES):
                    text = page.extract_text() or ""
                    parts.append(text)
                    budget -= len(text)
                    if budget <= 0:
                        break
                return " ".join(parts)
        if ext in OOXML_EXTS:
            if size > OOXML_MAX_BYTES:
                return None
            return _ooxml_text(path, ext)
    except Exception:
        return None  # corrupt or locked: fall back to the file name only
    return None


def _ooxml_text(path: str, ext: str) -> str:
    with zipfile.ZipFile(win_path(path)) as z:
        if ext == ".docx":
            targets = ["word/document.xml"]
        elif ext == ".xlsx":
            targets = ["xl/sharedStrings.xml"]
        else:
            targets = sorted(n for n in z.namelist()
                             if n.startswith("ppt/slides/slide") and n.endswith(".xml"))[:10]
        out, budget = [], TEXT_READ_LIMIT
        for t in targets:
            try:
                with z.open(t) as f:
                    data = f.read(min(budget, 4 * 1024 * 1024))  # bounded read: zip-bomb safe
            except KeyError:
                continue
            out.append(re.sub(r"<[^>]+>", " ", data.decode("utf-8", errors="ignore")))
            budget -= len(data)
            if budget <= 0:
                break
    return " ".join(out)


def classify_text(text: str | None, filename: str) -> str | None:
    """Pick the best keyword sub-category from the file name and extracted text."""
    name = re.sub(r"[_\-.]+", " ", filename)
    best, best_score = None, 0
    for sub, pat in _PATTERNS.items():
        name_hits = len(pat.findall(name))
        text_hits = len(pat.findall(text)) if text else 0
        if name_hits == 0 and text_hits < MIN_TEXT_HITS:
            continue
        score = text_hits + 5 * name_hits
        if score > best_score:
            best, best_score = sub, score
    return best


def _parallel(fn, rows, workers: int):
    """Map `fn` over rows with a thread pool, one page at a time (bounded memory)."""
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, rows))


def classify_documents(conn, run_id: int, rep: Reporter, workers: int, read_content: bool) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM files WHERE run_id=? AND category=?",
                         (run_id, CATEGORY_DOCUMENTS)).fetchone()[0]
    done = 0
    counts: dict[str, int] = {}

    def work(row):
        fid, path, ext, size = row
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        text = extract_text(path, ext, size) if read_content else None
        return classify_text(text, name), fid

    sql = ("SELECT id, path, ext, size FROM files WHERE run_id=? AND category=? "
           "AND id>? ORDER BY id LIMIT ?")
    for rows in iter_batches(conn, sql, (run_id, CATEGORY_DOCUMENTS), 256):
        rep.check()
        updates = _parallel(work, rows, workers)
        conn.executemany("UPDATE files SET subcategory=? WHERE id=?", updates)
        conn.commit()
        for sub, _ in updates:
            if sub:
                counts[sub] = counts.get(sub, 0) + 1
        done += len(rows)
        rep.progress("Reading documents", done, total)
    rep.progress("Reading documents", done, total, force=True)
    return counts


# ---------------------------------------------------------------- duplicates ----------------
def _hash_pass(conn, run_id: int, stage: str, algo: str, workers: int, rep: Reporter) -> None:
    """stage 'partial': quick fingerprint for size collisions; 'full': whole-file hash."""
    level, extra = (1, "") if stage == "partial" else (2, " AND hash IS NULL")
    label = "Quick-hashing candidates" if stage == "partial" else "Full-hashing candidates"
    total = conn.execute(f"SELECT COUNT(*) FROM files WHERE run_id=? AND cand=?{extra}",
                         (run_id, level)).fetchone()[0]
    sql = (f"SELECT id, path, size, mtime FROM files WHERE run_id=? AND cand=?{extra} "
           "AND id>? ORDER BY id LIMIT ?")
    done = 0

    def work(item):
        fid, path, size, mtime = item
        try:
            if stage == "partial":
                p, f = partial_hash(path, size, algo)
                return fid, path, size, mtime, p, f, None
            return fid, path, size, mtime, None, full_hash(path, algo, rep.cancel), None
        except OSError as e:
            return fid, path, size, mtime, None, None, str(e)

    for rows in iter_batches(conn, sql, (run_id, level), KEYSET_BATCH):
        rep.check()
        todo, applied = [], []
        for fid, path, size, mtime in rows:  # cache lookups stay on this thread
            c = conn.execute('SELECT size, mtime, partial, "full" FROM hash_cache '
                             "WHERE path=? AND algo=?", (path, algo)).fetchone()
            valid = c is not None and c[0] == size and abs((c[1] or 0) - mtime) < 1e-6
            if valid and stage == "partial" and c[2]:
                applied.append((c[2], c[3], fid))
            elif valid and stage == "full" and c[3]:
                applied.append((None, c[3], fid))
            else:
                todo.append((fid, path, size, mtime))
        results = _parallel(work, todo, workers)
        for fid, path, size, mtime, p, f, err in results:
            if err:
                rep.log(f"[skip] cannot hash {path}: {err}")
                continue
            if stage == "partial":
                conn.execute('INSERT OR REPLACE INTO hash_cache(path, algo, size, mtime, partial, "full") '
                             "VALUES (?,?,?,?,?,?)", (path, algo, size, mtime, p, f))
                applied.append((p, f, fid))
            else:
                conn.execute('UPDATE hash_cache SET "full"=? WHERE path=? AND algo=?', (f, path, algo))
                applied.append((None, f, fid))
        conn.executemany("UPDATE files SET partial_hash=COALESCE(?, partial_hash), hash=COALESCE(?, hash) "
                         "WHERE id=?", applied)
        conn.commit()
        done += len(rows)
        rep.progress(label, done, total)
    rep.progress(label, done, total, force=True)


def find_duplicates(conn, run_id: int, algo: str, workers: int, rep: Reporter) -> dict:
    """Flag extra copies of identical files, hashing as little data as possible.

    1. Only files that share an exact size can be duplicates.
    2. Of those, a first+last-64KB fingerprint weeds out most non-matches cheaply.
    3. Only survivors are read in full.
    The oldest file (then shortest path) of each identical group is the keeper.
    """
    conn.execute("UPDATE files SET cand=0, is_dup=0, hash=NULL, partial_hash=NULL WHERE run_id=?", (run_id,))
    conn.execute(
        "UPDATE files SET cand=1 WHERE run_id=? AND size>0 AND size IN "
        "(SELECT size FROM files WHERE run_id=? AND size>0 GROUP BY size HAVING COUNT(*)>1)",
        (run_id, run_id))
    conn.commit()
    _hash_pass(conn, run_id, "partial", algo, workers, rep)
    conn.execute(
        "UPDATE files SET cand=2 WHERE run_id=? AND cand=1 AND partial_hash IS NOT NULL AND "
        "(size, partial_hash) IN (SELECT size, partial_hash FROM files WHERE run_id=? AND cand=1 "
        "AND partial_hash IS NOT NULL GROUP BY size, partial_hash HAVING COUNT(*)>1)",
        (run_id, run_id))
    conn.commit()
    _hash_pass(conn, run_id, "full", algo, workers, rep)
    conn.execute(
        "WITH ranked AS (SELECT id, ROW_NUMBER() OVER (PARTITION BY size, hash "
        "ORDER BY mtime, length(path), path) AS rn FROM files WHERE run_id=? AND hash IS NOT NULL) "
        "UPDATE files SET is_dup=1 WHERE id IN (SELECT id FROM ranked WHERE rn>1)", (run_id,))
    conn.commit()
    n, wasted = conn.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE run_id=? AND is_dup=1",
                             (run_id,)).fetchone()
    groups = conn.execute("SELECT COUNT(DISTINCT hash) FROM files WHERE run_id=? AND is_dup=1",
                          (run_id,)).fetchone()[0]
    return {"duplicate_files": n, "duplicate_groups": groups, "wasted_bytes": wasted}
