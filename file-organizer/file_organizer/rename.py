"""Smart rename: read a bill, invoice or receipt and name it `2026-05-14 Acme Ltd 1,240.00.pdf`.

Off by default. A new name is proposed only when a date and at least one of vendor or amount are found, so a
file the reader cannot understand keeps its own name. The dry-run report shows every rename before anything happens."""
from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor

from . import analyzer, lexicon
from .common import Reporter

MAX_CANDIDATES = 20000
RENAMEABLE = {".pdf", ".docx", ".txt", ".md", ".rtf", ".html", ".htm", ".xlsx"}
_NAME_HINT = re.compile(r"invoice|receipt|bill|statement|τιμολ|απόδειξ|αποδειξ|λογαριασμ", re.I)

_MONTHS = {m: i + 1 for i, names in enumerate(
    ("jan january ιαν", "feb february φεβ", "mar march μαρ", "apr april απρ", "may μαι μαΐου", "jun june ιουν",
     "jul july ιουλ", "aug august αυγ", "sep sept september σεπ", "oct october οκτ", "nov november νοε",
     "dec december δεκ")) for m in names.split()}
_MON = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_ISO = re.compile(r"(?<!\d)((?:19|20)\d{2})[-/.](0?[1-9]|1[0-2])[-/.](0?[1-9]|[12]\d|3[01])(?!\d)")
_DATE_DMY = re.compile(r"(?<!\d)(0?[1-9]|[12]\d|3[01])[-/.](0?[1-9]|1[0-2])[-/.]((?:19|20)\d{2})(?!\d)")
_DATE_DMON = re.compile(rf"(?<!\d)(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?\s+({_MON})\.?,?\s+((?:19|20)\d{{2}})", re.I)
_DATE_MONDY = re.compile(rf"\b({_MON})\.?\s+(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?,?\s+((?:19|20)\d{{2}})", re.I)
_DATE_LABEL = re.compile(r"(invoice date|date of issue|issue date|issued|date|ημερομηνία|ημ/νία)\W{0,3}$", re.I)

_NUM = r"\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{2})|\d+(?:[.,]\d{2})"
_CUR = r"(?:€|\$|£|EUR|USD|GBP)"
_AMOUNT_LABELED = re.compile(
    rf"(?:grand total|total due|amount due|balance due|total amount|total|amount|σύνολο|συνολο|πληρωτέο)[^\w€$£]{{0,12}}"
    rf"({_CUR})?\s*({_NUM})\s*({_CUR})?", re.I)
_AMOUNT_ANY = re.compile(rf"({_CUR})\s*({_NUM})|({_NUM})\s*({_CUR})", re.I)
_VENDOR_LABEL = re.compile(
    r"(?:invoice from|bill from|issued by|supplier|vendor|seller|from|εκδότης|προμηθευτής)\s*[:\-]?\s*([^\n\r]{3,45})", re.I)
_SKIP_LINE = re.compile(r"invoice|receipt|statement|bill|tax|vat|page|date|total|amount|www\.|http|@|τιμολ|απόδειξ", re.I)
_SYMBOL = {"EUR": "€", "USD": "$", "GBP": "£"}
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _norm_number(raw: str) -> float | None:
    s = raw.replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if len(s.split(",")[-1]) == 2 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def find_date(text: str) -> str | None:
    """First plausible date, preferring one that follows a 'date' label. Day-first for 01/02/2026."""
    found: list[tuple[int, str]] = []
    for m in _DATE_ISO.finditer(text):
        found.append((m.start(), f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}"))
    for m in _DATE_DMY.finditer(text):
        found.append((m.start(), f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}"))
    for m in _DATE_DMON.finditer(text):
        found.append((m.start(), f"{m[3]}-{_MONTHS[m[2].lower().rstrip('.')]:02d}-{int(m[1]):02d}"))
    for m in _DATE_MONDY.finditer(text):
        found.append((m.start(), f"{m[3]}-{_MONTHS[m[1].lower().rstrip('.')]:02d}-{int(m[2]):02d}"))
    if not found:
        return None
    found.sort()
    for pos, iso in found:
        if _DATE_LABEL.search(text[max(0, pos - 24):pos]):
            return iso
    return found[0][1]


def find_amount(text: str) -> str | None:
    m = _AMOUNT_LABELED.search(text)
    cur, num = None, None
    if m:
        cur, num = (m[1] or m[3]), m[2]
    else:
        best = None
        for a in _AMOUNT_ANY.finditer(text):
            c, n = (a[1], a[2]) if a[1] else (a[4], a[3])
            v = _norm_number(n)
            if v is not None and (best is None or v > best[0]):
                best = (v, c, n)
        if best:
            _, cur, num = best
    value = _norm_number(num) if num else None
    if value is None or value <= 0:
        return None
    sym = _SYMBOL.get((cur or "").upper(), cur or "")
    return f"{sym}{value:,.2f}"


def find_vendor(text: str) -> str | None:
    m = _VENDOR_LABEL.search(text)
    if m:
        v = re.split(r"\s{2,}|\t|,|\|", m[1].strip())[0]
    else:
        v = ""
        for line in text.splitlines()[:12]:
            line = line.strip()
            if 3 <= len(line) <= 40 and re.search(r"[A-Za-zͰ-Ͽ]{3}", line) and not _SKIP_LINE.search(line):
                v = line
                break
    v = _BAD.sub("", v).strip(" .-:")
    words = v.split()
    return " ".join(words[:4]) if 3 <= len(v) else None


def propose_name(text: str | None, ext: str) -> str | None:
    if not text:
        return None
    date = find_date(text)
    vendor, amount = find_vendor(text), find_amount(text)
    if not date or not (vendor or amount):
        return None
    stem = " ".join(p for p in (date, vendor, amount) if p)
    return _BAD.sub("", stem)[:90].strip() + ext


def is_candidate(path: str, ext: str, role: str | None, sub: str | None) -> bool:
    if ext not in RENAMEABLE:
        return False
    return role == "Payments & Banking" or sub in ("Invoices", "Receipts") or bool(_NAME_HINT.search(os.path.basename(path)))


def propose_all(conn, run_id: int, rep: Reporter, workers: int) -> int:
    """Fill files.newname for bills and receipts. Returns how many got a new name."""
    rows = [r for r in conn.execute(
        "SELECT id, path, ext, size, role, subcategory FROM files WHERE run_id=? AND cleanup=0 AND is_dup=0 "
        "AND ext IN (%s)" % ",".join("?" * len(RENAMEABLE)), (run_id, *sorted(RENAMEABLE)))
        if is_candidate(r[1], r[2], r[4], r[5])][:MAX_CANDIDATES]

    def work(r):
        try:
            return r[0], propose_name(analyzer.extract_text(r[1], r[2], r[3]), r[2])
        except Exception:
            return r[0], None

    named, done = 0, 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for fid, new in pool.map(work, rows):
            rep.check()
            done += 1
            if new:
                conn.execute("UPDATE files SET newname=? WHERE id=?", (new, fid))
                named += 1
            rep.progress("Reading bills and receipts", done, len(rows))
    conn.commit()
    return named
