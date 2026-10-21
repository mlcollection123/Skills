"""Project grouping: work out which files belong together and what to call the folder.

How it decides (no cloud, no AI model, everything local):

1. Every file gets *evidence tokens* from its name, its folder names, reference codes found in either
   (e.g. RX-48213), and the text inside it (PDF, Word, Excel, PowerPoint, HTML, JSON, text).
   Greek and Latin spellings are folded together, so 'Ορχιδιά', 'Orchidia' and 'Orchidia' are one word.
2. Words that only describe a document type ('payment', 'agreement', 'signed') are ignored for linking.
3. Each file picks its strongest *anchor*: a distinctive token shared with other files, weighted by where
   it was found (file name > folder > text) and how rare it is.
4. Anchors that keep appearing on the same files are merged (they are aliases of one project).
5. A group of at least MIN_PROJECT_FILES files becomes a project folder, named after its strongest
   anchor as written in the file names.
"""
from __future__ import annotations

import json
import math
import os
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from . import analyzer, lexicon
from .common import Reporter
from .config import (
    ANCHOR_CAP_FRAC, ANCHOR_CAP_MIN, BRIDGE_RATIO, CATEGORY_DOCUMENTS, MEDIA_EXTS, MERGE_RATIO,
    MIN_PROJECT_FILES, TEXT_ONLY_MIN_FILES, app_dir,
)
from .db import iter_batches

SRC_NAME, SRC_FOLDER, SRC_CODE, SRC_TEXT = 1, 2, 3, 4
_FOLDER_WEIGHTS = (2.0, 1.5, 1.0, 0.5, 0.5)     # nearest folder counts most
_TEXT_TOP = 15
_TOP_ANCHORS = 5
_TEXT_KNOWN = 12
_TEXT_CHARS = 120_000
_MARKUP_TEXT = analyzer.PLAIN_EXTS | analyzer.MARKUP_EXTS | analyzer.OOXML_EXTS | {".pdf"}


# ----------------------------------------------------------------------------- rules file
def rules_path() -> str:
    return os.path.join(app_dir(), "projects.json")


def load_ignore(extra: str = "") -> set[str]:
    """Words that must never link files, e.g. your own name. In projects.json: {"ignore": ["john"]}.
    `extra` is a comma separated list from the app's "My name" setting."""
    try:
        with open(rules_path(), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raw = {}
    listed = list(raw.get("ignore", [])) + [x for x in re.split(r"[,;]", extra or "") if x.strip()]
    return {k for a in listed for k in (lexicon.canon(w) for w in lexicon.words(str(a))) if k}


def load_rules() -> dict[str, list[str]]:
    """Optional user rules: {"Orchidia Plot": ["orchidia", "ορχιδιά", "plot 917"]}.

    Any file whose name, folder or text contains one of the aliases is filed under that project.
    """
    try:
        with open(rules_path(), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    rules = {}
    for label, aliases in raw.items():
        if label == "ignore":
            continue
        keys = [k for a in aliases for k in ([lexicon.canon(w) for w in lexicon.words(str(a))]
                                             + sorted(lexicon.codes(str(a)))) if k]
        if keys:
            rules[str(label)] = keys
    return rules


# ------------------------------------------------------------------------------ features
def _add(toks: dict, key: str, src: int, weight: float, surface: str | None) -> None:
    cur = toks.get((key, src))
    if cur is None or weight > cur[0]:
        toks[(key, src)] = (weight, surface)


def _usable(key: str) -> bool:
    return len(key) >= 3 and key not in lexicon.GENERIC and not key.isdigit()


def name_features(path: str, ext: str, root: str) -> dict:
    """Evidence from the file name and its folders only (cheap: no file is opened)."""
    base = os.path.splitext(os.path.basename(path))[0]
    rel_dir = os.path.relpath(os.path.dirname(path), root)
    parts = [] if rel_dir in (".", "") else rel_dir.split(os.sep)
    # nearest folder first. The scan root itself is excluded: it is shared by every file, so it
    # says nothing about which files belong together.
    chain = list(reversed(parts))

    toks: dict = {}
    name_words = lexicon.words(base)
    name_keys = [lexicon.canon(w) for w in name_words]
    for w, k in zip(name_words, name_keys):
        if _usable(k):
            _add(toks, k, SRC_NAME, 3.0, w)
    for k in lexicon.codes(base):
        _add(toks, k, SRC_CODE, 3.0, k)

    draft = lexicon.is_draft(base)
    subfolder = None
    for depth, fname in enumerate(chain[:5]):
        if lexicon.is_draft(fname):
            draft = draft or depth < 3
            continue
        if lexicon.is_generic_folder(fname):
            continue
        if subfolder is None:
            subfolder = fname
        wgt = _FOLDER_WEIGHTS[depth]
        for w in lexicon.words(fname):
            k = lexicon.canon(w)
            if _usable(k):
                _add(toks, k, SRC_FOLDER, wgt, w)
        for k in lexicon.codes(fname):
            _add(toks, k, SRC_CODE, wgt, k)

    stem = " ".join(sorted(k for k in name_keys
                           if k and k not in lexicon.STATUS_WORDS and not (k.isdigit() and len(k) < 3)))
    if any(_usable(k) for k in stem.split()):     # same document in several versions: link the copies
        _add(toks, "~" + stem, SRC_CODE, 2.0, None)
    return {"tokens": [(k, s, w, surf) for (k, s), (w, surf) in toks.items()],
            "role": _role(name_keys, {}, ext), "draft": draft, "stem": stem, "subfolder": subfolder}


def text_features(path: str, ext: str, size: int, category: str, known: set[str]) -> dict:
    """Evidence from inside the file. `known` holds names that several files already share, so a
    single mention of one (a plot's name in a contract) still counts."""
    text = analyzer.extract_text(path, ext, size)
    if not text:
        return {"tokens": [], "role": None, "subcat": None}
    text = text[:_TEXT_CHARS]
    counts: Counter = Counter()
    for w in lexicon.alpha_words(text):
        k = lexicon.canon(w)
        if k:
            counts[k] += 1
    toks: dict = {}
    n_top = n_known = 0
    for k, c in counts.most_common():
        if not _usable(k):
            continue
        if k in known and n_known < _TEXT_KNOWN:
            _add(toks, k, SRC_TEXT, 1.0 + math.log(c), None)
            n_known += 1
        elif c >= 2 and n_top < _TEXT_TOP:
            _add(toks, k, SRC_TEXT, 1.0 + math.log(c), None)
            n_top += 1
    for k in lexicon.codes(text[:20_000]):
        _add(toks, k, SRC_CODE, 1.5, k)
    subcat = analyzer.classify_text(text, os.path.basename(path)) if category == CATEGORY_DOCUMENTS else None
    return {"tokens": [(k, s, w, surf) for (k, s), (w, surf) in toks.items()],
            "role": _role([], counts, ext), "subcat": subcat}


def _role(name_keys, text_counts, ext) -> str | None:
    score: Counter = Counter()
    for k in name_keys:
        r = lexicon.ROLE_KEYS.get(k)
        if r:
            score[r] += 3
    if score or ext in lexicon.CAD_EXTS:      # what the file is called beats what its text mentions
        text_counts = {}
    for k, c in text_counts.items():
        r = lexicon.ROLE_KEYS.get(k)
        if r:
            score[r] += min(c, 3)
    if ext in lexicon.CAD_EXTS:
        score["Design & Drawings"] += 5
    if not score or max(score.values()) < 2:
        return lexicon.EXT_ROLE.get(ext)         # a .kmz is a map, a .zip an archive
    top = max(score.values())
    return next(r for r in lexicon.ROLE_PRIORITY if score.get(r) == top)


# ---------------------------------------------------------------------------- index pass
def index_files(conn, run_id: int, root: str, rep: Reporter, workers: int, read_content: bool,
                my_names: str = "") -> dict:
    """Two passes. 1: names, folders and codes of every eligible file (no file is opened).
    2: text inside documents, keeping any word that matches a name several files share."""
    conn.execute("DROP INDEX IF EXISTS idx_tokens_tok")
    conn.execute("DROP INDEX IF EXISTS idx_tokens_file")
    conn.execute("DELETE FROM tokens WHERE file_id IN (SELECT id FROM files WHERE run_id=?)", (run_id,))
    conn.commit()
    root = os.path.abspath(root)
    where = "run_id=? AND cleanup=0 AND taken IS NULL"
    total = conn.execute(f"SELECT COUNT(*) FROM files WHERE {where}", (run_id,)).fetchone()[0]
    ignore = load_ignore(my_names)

    def drop_ignored(toks):
        return [t for t in toks if t[0] not in ignore] if ignore else toks

    # ---- pass 1: names
    sql = f"SELECT id, path, ext FROM files WHERE {where} AND id>? ORDER BY id LIMIT ?"
    done = 0
    for rows in iter_batches(conn, sql, (run_id,), 2000):
        rep.check()
        tok_rows, upd = [], []
        for fid, path, ext in rows:
            f = name_features(path, ext, root)
            tok_rows.extend((fid, k, s, w, surf) for k, s, w, surf in drop_ignored(f["tokens"]))
            upd.append((f["role"], "draft" if f["draft"] else None, f["stem"], f["subfolder"], fid))
        conn.executemany("INSERT INTO tokens(file_id, tok, src, w, surf) VALUES (?,?,?,?,?)", tok_rows)
        conn.executemany("UPDATE files SET role=?, status=?, stem=?, subfolder=? WHERE id=?", upd)
        conn.commit()
        done += len(rows)
        rep.progress("Reading file and folder names", done, total)
    rep.progress("Reading file and folder names", done, total, force=True)

    subcats: Counter = Counter()
    if read_content or total:
        cap = max(ANCHOR_CAP_MIN, int(ANCHOR_CAP_FRAC * total))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tokens_tok ON tokens(tok)")
        known = {t for (t,) in conn.execute(
            "SELECT tok FROM tokens WHERE src IN (1,2) AND tok NOT LIKE '~%' GROUP BY tok "
            "HAVING COUNT(DISTINCT file_id) BETWEEN 2 AND ?", (cap,))}
        exts = sorted(_MARKUP_TEXT)
        marks = ",".join("?" * len(exts))
        n_text = conn.execute(f"SELECT COUNT(*) FROM files WHERE {where} AND ext IN ({marks})",
                              (run_id, *exts)).fetchone()[0]
        sql = (f"SELECT id, path, ext, size, category FROM files WHERE {where} AND ext IN ({marks}) "
               "AND id>? ORDER BY id LIMIT ?")

        def work(row):
            fid, path, ext, size, category = row
            try:
                f = text_features(path, ext, size, category, known) if read_content else \
                    {"tokens": [], "role": None, "subcat": None}
            except Exception:       # a damaged file must never stop the scan
                f = {"tokens": [], "role": None, "subcat": None}
            return fid, f

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for rows in iter_batches(conn, sql, (run_id, *exts), 256):
                rep.check()
                tok_rows, upd = [], []
                for fid, f in ex.map(work, rows):
                    tok_rows.extend((fid, k, s, w, surf) for k, s, w, surf in drop_ignored(f["tokens"]))
                    upd.append((f["role"], f["subcat"], fid))
                    if f["subcat"]:
                        subcats[f["subcat"]] += 1
                conn.executemany("INSERT INTO tokens(file_id, tok, src, w, surf) VALUES (?,?,?,?,?)", tok_rows)
                conn.executemany("UPDATE files SET role=COALESCE(role, ?), subcategory=? WHERE id=?", upd)
                conn.commit()
                done += len(rows)
                rep.progress("Reading text inside documents", done, n_text)
        rep.progress("Reading text inside documents", done, n_text, force=True)
    return {"indexed": total, "subcategories": dict(subcats)}


# ------------------------------------------------------------------------------ clustering
class _UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def discover(conn, run_id: int, n_files: int, rep: Reporter) -> dict:
    """Find project groups among the indexed files and write project/role/subgroup to the database."""
    conn.execute("UPDATE files SET project=NULL, cluster=NULL, reason=NULL, subgroup=NULL WHERE run_id=?",
                 (run_id,))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tokens_tok ON tokens(tok)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tokens_file ON tokens(file_id)")
    conn.commit()
    if n_files == 0:
        return {"projects": 0, "grouped": 0, "labels": []}

    rep.progress("Linking related files", 0, None, force=True)
    ruled = _apply_rules(conn, run_id)

    cap = max(ANCHOR_CAP_MIN, int(ANCHOR_CAP_FRAC * n_files))
    conn.execute("DROP TABLE IF EXISTS temp.anchor")
    conn.execute(
        "CREATE TEMP TABLE anchor AS SELECT t.tok AS tok, COUNT(DISTINCT t.file_id) AS df, "
        "MAX(CASE WHEN t.src < 4 THEN 1 ELSE 0 END) AS strong FROM tokens t "
        "JOIN files f ON f.id = t.file_id WHERE f.run_id=? GROUP BY t.tok "
        "HAVING COUNT(DISTINCT t.file_id) BETWEEN 2 AND ?", (run_id, cap))
    conn.execute("DELETE FROM temp.anchor WHERE strong=0 AND df<?", (TEXT_ONLY_MIN_FILES,))   # text-only words need support
    conn.execute("CREATE UNIQUE INDEX temp.anchor_tok ON anchor(tok)")

    conn.execute("DROP TABLE IF EXISTS temp.best")
    conn.execute("CREATE TEMP TABLE best(file_id INTEGER, rnk INTEGER, tok TEXT, score REAL, surf TEXT, txt INTEGER)")
    claims: Counter = Counter()
    co: Counter = Counter()
    cur = conn.execute(
        "SELECT t.file_id, t.tok, SUM(t.w), a.df, MAX(t.surf), a.strong, MIN(t.src) FROM tokens t "
        "JOIN anchor a ON a.tok = t.tok JOIN files f ON f.id = t.file_id "
        "WHERE f.run_id=? AND f.project IS NULL GROUP BY t.file_id, t.tok ORDER BY t.file_id", (run_id,))
    batch: list = []

    def flush_file(fid, items):
        items.sort(key=lambda x: -x[1])
        top = items[:_TOP_ANCHORS]
        for i, (tok, score, surf, _src) in enumerate(top, 1):
            batch.append((fid, i, tok, score, surf, int(_src == 4)))
            claims[tok] += 1
        # Star around the best anchor: bounded memory. A word found only inside this file's text never links two
        # anchors: a common word such as "account" must not fuse two unrelated groups.
        if top[0][3] != 4:
            for other in top[1:]:
                if other[3] != 4:
                    a, b = sorted((top[0][0], other[0]))
                    co[(a, b)] += 1

    cur_fid, items, seen = None, [], 0
    for fid, tok, sw, df, surf, is_strong, min_src in cur:
        if fid != cur_fid:
            if cur_fid is not None:
                flush_file(cur_fid, items)
                seen += 1
                if len(batch) >= 20_000:
                    conn.executemany("INSERT INTO temp.best VALUES (?,?,?,?,?,?)", batch)
                    batch.clear()
                    rep.check()
            cur_fid, items = fid, []
        # favour words shared by many files, and words that also appear in some file or folder name
        items.append((tok, sw * math.log(1 + df) * (1.6 if is_strong else 1.0), surf, min_src))
    if cur_fid is not None:
        flush_file(cur_fid, items)
    conn.executemany("INSERT INTO temp.best VALUES (?,?,?,?,?,?)", batch)
    conn.execute("CREATE INDEX temp.best_file ON best(file_id)")

    # Anchors that keep landing on the same files are aliases of one project.
    uf = _UnionFind()
    for tok in claims:
        uf.find(tok)
    for (a, b), c in co.items():
        if c >= 2 and c / min(claims[a], claims[b]) >= MERGE_RATIO:
            uf.union(a, b)

    # Second step, between whole groups: files that carry evidence for two groups bridge them. Two or more
    # bridging files (a draft that names the plot, a signed copy that only has the code) join the groups.
    sizes0: Counter = Counter()
    for (tok,) in conn.execute("SELECT tok FROM best WHERE rnk=1"):
        sizes0[uf.find(tok)] += 1
    bridge: Counter = Counter()          # any evidence: used to attach a small document family to a group
    bridge_named: Counter = Counter()    # evidence from names, folders and codes only: may join two groups
    cur_fid, roots, named = None, set(), set()

    def close_roots():
        for found, counter in ((roots, bridge), (named, bridge_named)):
            if len(found) > 1:
                ordered = sorted(found)
                for i in range(len(ordered)):
                    for j in range(i + 1, len(ordered)):
                        counter[(ordered[i], ordered[j])] += 1

    for fid, tok, txt in conn.execute("SELECT file_id, tok, txt FROM best ORDER BY file_id, rnk"):
        if fid != cur_fid:
            close_roots()
            cur_fid, roots, named = fid, set(), set()
        roots.add(uf.find(tok))
        if not txt:
            named.add(uf.find(tok))   # a word found only in text must not fuse two real groups
    close_roots()
    family_roots = {uf.find(t) for t in claims if t.startswith("~") or any(ch.isdigit() for ch in t)}
    attach: dict[str, list] = defaultdict(list)
    for (a, b), c in bridge.items():
        small = min(sizes0[a] or 1, sizes0[b] or 1)
        cn = bridge_named[(a, b)]
        if cn >= 2 and cn / small >= BRIDGE_RATIO:
            uf.union(a, b)
        for lone, other in ((a, b), (b, a)):         # a document family too small to stand alone (a draft and
            if sizes0[lone] < MIN_PROJECT_FILES <= sizes0[other] and lone in family_roots:   # its signed copy)
                attach[lone].append((c, other))      # joins the one group it touches. A lone file with a
                                                     # generic word in common does not.
    for lone, options in attach.items():
        options.sort(reverse=True)
        if len(options) == 1 or options[0][0] > options[1][0]:
            uf.union(lone, options[0][1])

    strong = {t for (t,) in conn.execute("SELECT tok FROM anchor WHERE strong=1") if t in uf.parent}
    sizes: Counter = Counter()
    root_has_strong: set = set()
    for fid, tok in conn.execute("SELECT file_id, tok FROM best WHERE rnk=1"):
        r = uf.find(tok)
        sizes[r] += 1
        if tok in strong:
            root_has_strong.add(r)
    valid = {r for r, n in sizes.items() if n >= MIN_PROJECT_FILES and r in root_has_strong}
    cluster_ids = {r: i for i, r in enumerate(sorted(valid), 1)}
    # A file linked only by a word found inside its text (nothing in its name or folder) needs a second,
    # different anchor of the same group. One common word such as "account" must not pull a loose document in.
    text_only = {fid for (fid,) in conn.execute(
        "SELECT b.file_id FROM best b JOIN tokens t ON t.file_id = b.file_id AND t.tok = b.tok "
        "WHERE b.rnk = 1 GROUP BY b.file_id HAVING MIN(t.src) = 4")}
    support: Counter = Counter()
    if text_only:
        for fid, tok in conn.execute("SELECT file_id, tok FROM best"):
            if fid in text_only:
                support[(fid, uf.find(tok))] += 1
    upd = [(cluster_ids[uf.find(tok)], fid) for fid, tok in
           conn.execute("SELECT file_id, tok FROM best WHERE rnk=1")
           if uf.find(tok) in valid and (fid not in text_only or support[(fid, uf.find(tok))] >= 2)]
    conn.executemany("UPDATE files SET cluster=? WHERE id=?", upd)
    conn.commit()

    labels = _name_clusters(conn, run_id, set(cluster_ids.values()))
    for cid, label in labels.items():
        conn.execute("UPDATE files SET project=? WHERE run_id=? AND cluster=?", (label, run_id, cid))
    conn.execute(
        "UPDATE files SET reason=(SELECT 'matched: ' || COALESCE(b.surf, b.tok) FROM temp.best b "
        "WHERE b.file_id = files.id AND b.rnk = 1) WHERE run_id=? AND project IS NOT NULL AND reason IS NULL",
        (run_id,))
    conn.commit()

    _share_roles(conn, run_id)
    _keep_source_subfolders(conn, run_id)
    attached = _attach_media(conn, run_id, set(labels.values()), rep)
    grouped = conn.execute("SELECT COUNT(*) FROM files WHERE run_id=? AND project IS NOT NULL",
                           (run_id,)).fetchone()[0]
    projects = conn.execute(
        "SELECT project, COUNT(*) FROM files WHERE run_id=? AND project IS NOT NULL GROUP BY project "
        "ORDER BY 2 DESC", (run_id,)).fetchall()
    rep.progress("Linking related files", grouped, None, force=True)
    return {"projects": len(projects), "grouped": grouped, "labels": projects, "ruled": ruled,
            "media_attached": attached}


def _apply_rules(conn, run_id: int) -> int:
    n = 0
    for label, keys in load_rules().items():
        for k in keys:
            cur = conn.execute(
                "UPDATE files SET project=?, reason=? WHERE run_id=? AND project IS NULL AND id IN "
                "(SELECT file_id FROM tokens WHERE tok=?)", (label, f"rule: {label}", run_id, k))
            n += cur.rowcount
    conn.commit()
    return n


def _name_clusters(conn, run_id: int, cluster_ids: set[int]) -> dict[int, str]:
    """Label each cluster with its strongest name/folder anchor, spelled as in the file names."""
    score: dict[int, Counter] = defaultdict(Counter)
    for cid, tok, src, c in conn.execute(
            "SELECT f.cluster, t.tok, t.src, COUNT(DISTINCT t.file_id) FROM tokens t "
            "JOIN files f ON f.id = t.file_id JOIN temp.anchor a ON a.tok = t.tok "
            "WHERE f.run_id=? AND f.cluster IS NOT NULL AND t.src IN (1,2,3) AND t.tok NOT LIKE '~%' "
            "GROUP BY f.cluster, t.tok, t.src", (run_id,)):
        # file names count most; reference codes only name a project when nothing else can
        score[cid][tok] += c * {SRC_NAME: 3.0, SRC_FOLDER: 1.0, SRC_CODE: 0.2}[src]
    labels: dict[int, str] = {}
    used: Counter = Counter()
    for cid in sorted(cluster_ids):
        if not score[cid]:
            continue
        ranked = score[cid].most_common(4)
        best = ranked[0][1]
        # equally strong words usually form one phrase ("Orchid House", "Kitchen Renovation")
        picks = [t for t, sc in ranked if sc >= 0.75 * best][:3]
        shown = {}
        for tok in picks:
            rows = conn.execute(
                "SELECT t.surf, COUNT(*) FROM tokens t JOIN files f ON f.id = t.file_id "
                "WHERE f.run_id=? AND f.cluster=? AND t.tok=? AND t.src IN (1,2,3) AND t.surf IS NOT NULL "
                "GROUP BY t.surf ORDER BY 2 DESC", (run_id, cid, tok)).fetchall()
            shown[tok] = _display(rows) or tok
        if len(picks) > 1:
            picks = _phrase_order(conn, run_id, cid, picks, shown)
        label = " ".join(shown[t] for t in picks)
        used[label.lower()] += 1
        if used[label.lower()] > 1:
            label = f"{label} ({used[label.lower()]})"
        labels[cid] = label
    return labels


def _phrase_order(conn, run_id: int, cid: int, picks: list[str], shown: dict[str, str]) -> list[str]:
    """Order words as they are written in a file name that contains all of them."""
    for (path,) in conn.execute("SELECT path FROM files WHERE run_id=? AND cluster=? LIMIT 300", (run_id, cid)):
        name = os.path.basename(path).lower()
        pos = {t: name.find(shown[t].lower()) for t in picks}
        if all(v >= 0 for v in pos.values()):
            return sorted(picks, key=pos.get)
    return picks[:1]          # words never appear together: name the group after the strongest one


def _display(rows) -> str | None:
    """Prefer a Latin spelling (most frequent first), else the most frequent spelling."""
    if not rows:
        return None
    latin = [r for r in rows if any("a" <= ch.lower() <= "z" for ch in r[0])]
    surf = (latin or rows)[0][0].strip()
    if surf.isdigit() or any(ch.isdigit() for ch in surf):
        return surf.upper()
    return surf if any(ch.isupper() for ch in surf) else surf[:1].upper() + surf[1:]


def _share_roles(conn, run_id: int) -> None:
    """A signed copy inherits the role of its draft (same name apart from status words)."""
    cur = conn.execute(
        "SELECT id, project, stem, role FROM files WHERE run_id=? AND project IS NOT NULL AND stem != '' "
        "ORDER BY project, stem, id", (run_id,))
    group, key, updates = [], None, []

    def close():
        votes = Counter(r for _, r in group if r)
        if votes:
            best = votes.most_common(1)[0][0]
            updates.extend((best, i) for i, r in group if not r)

    for fid, project, stem, role in cur:
        if (project, stem) != key:
            close()
            group, key = [], (project, stem)
        group.append((fid, role))
    close()
    conn.executemany("UPDATE files SET role=? WHERE id=?", updates)
    conn.commit()


def _keep_source_subfolders(conn, run_id: int) -> None:
    """Keep a meaningful folder the user already made inside a project (e.g. 'Acme Ltd'), if it holds 3+ files."""
    pairs = conn.execute(
        "SELECT project, subfolder, COUNT(*) FROM files WHERE run_id=? AND project IS NOT NULL "
        "AND subfolder IS NOT NULL GROUP BY project, subfolder HAVING COUNT(*) >= ?",
        (run_id, MIN_PROJECT_FILES)).fetchall()
    for project, sub, _ in pairs:
        proj_keys = {lexicon.canon(w) for w in lexicon.words(project)}
        sub_keys = {lexicon.canon(w) for w in lexicon.words(sub)}
        if proj_keys & sub_keys:
            continue            # that folder just repeats the project name
        conn.execute("UPDATE files SET subgroup=? WHERE run_id=? AND project=? AND subfolder=?",
                     (sub, run_id, project, sub))
    conn.commit()


def _label_keys(label: str) -> set[str]:
    return {k for k in (lexicon.canon(w) for w in lexicon.words(label)) if _usable(k)}


def _attach_media(conn, run_id: int, labels: set[str], rep: Reporter) -> int:
    """Photos, RAW files and videos whose folder or file name carries a project's name join that project.

    A folder called 'Orchid Drone 27.09.2026' next to the project's documents is project material, so its
    pictures go to Projects/Orchid/Drone 27.09.2026 instead of a generic trip or date folder.
    """
    keymap: dict[str, str] = {}
    for label in sorted(labels):
        for k in _label_keys(label):
            keymap.setdefault(k, label)
    if not keymap:
        return 0
    exts = sorted(MEDIA_EXTS)
    marks = ",".join("?" * len(exts))
    sql = (f"SELECT id, path FROM files WHERE run_id=? AND project IS NULL AND cleanup=0 AND "
           f"(taken IS NOT NULL OR ext IN ({marks})) AND id>? ORDER BY id LIMIT ?")
    run = conn.execute("SELECT source FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    n = 0
    for rows in iter_batches(conn, sql, (run_id, *exts), 2000):
        rep.check()
        upd = []
        for fid, path in rows:
            rel_dir = os.path.relpath(os.path.dirname(path), run)
            parts = [] if rel_dir in (".", "") else list(reversed(rel_dir.split(os.sep)))[:4]
            votes: Counter = Counter()
            hit = None
            for depth, text in enumerate([os.path.splitext(os.path.basename(path))[0]] + parts):
                for k in (lexicon.canon(w) for w in lexicon.words(text)):
                    if k in keymap:
                        votes[keymap[k]] += 1
                        hit = hit or (text if depth else None)
            if not votes:
                continue
            label = votes.most_common(1)[0][0]
            sub = _remainder(next((p for p in parts if not lexicon.is_generic_folder(p)), None), _label_keys(label))
            upd.append((label, sub, lexicon.ROLE_MEDIA, f"matched: folder or name carries '{label}'", fid))
        conn.executemany("UPDATE files SET project=?, subgroup=?, role=?, reason=? WHERE id=?", upd)
        conn.commit()
        n += len(upd)
    return n


def _remainder(folder: str | None, label_keys: set[str]) -> str | None:
    """'Orchid Drone 27.09.2026' minus the project word -> 'Drone 27.09.2026'. None if nothing is left."""
    if not folder:
        return None
    pieces = re.split(r"([^\W_]+)", folder)             # words stay, separators between them stay too
    kept = [p for p in pieces if not (p and re.fullmatch(r"[^\W_]+", p) and lexicon.canon(p) in label_keys)]
    text = re.sub(r"\s+", " ", "".join(kept)).strip(" -_.,;:()[]")
    return text if sum(ch.isalpha() for ch in text) >= 3 else None
