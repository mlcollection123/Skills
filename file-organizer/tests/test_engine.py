"""End-to-end tests of the engine on a synthetic tree (no GUI needed).

Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from file_organizer import analyzer, db, engine  # noqa: E402
from file_organizer.common import Cancelled, Reporter  # noqa: E402


def write(path: str, data: bytes | str = b"x") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as f:
        f.write(data)
    return path


def snapshot(root: str) -> dict[str, str]:
    """relative path -> content hash, for exact before/after comparisons."""
    out = {}
    for d, _, files in os.walk(root):
        for n in files:
            p = os.path.join(d, n)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = hashlib.sha256(f.read()).hexdigest()
    return out


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "src")
        self.dst = os.path.join(self.tmp, "dst")
        self.db = os.path.join(self.tmp, "index.db")
        os.environ["FILE_ORGANIZER_HOME"] = os.path.join(self.tmp, "home")
        self.logs: list[str] = []
        self.rep = Reporter(lambda m: self.logs.append(m[1]) if m[0] == "log" else None, min_interval=0)

        write(os.path.join(self.src, "a", "invoice_march.txt"), "Invoice #1")
        write(os.path.join(self.src, "a", "notes.txt"), "tax return. The tax office wants the tax form.")
        write(os.path.join(self.src, "b", "random.txt"), "nothing special here")
        write(os.path.join(self.src, "b", "pic.png"), b"\x89PNG" + b"1" * 5000)
        write(os.path.join(self.src, "b", "pic_copy.png"), b"\x89PNG" + b"1" * 5000)      # duplicate
        write(os.path.join(self.src, "b", "pic_other.png"), b"\x89PNG" + b"2" * 5000)     # same size, differs
        write(os.path.join(self.src, "c", "bundle.zip"), b"PK-not-really")
        write(os.path.join(self.src, "c", "setup.exe"), b"MZ" * 100)
        write(os.path.join(self.src, "c", "lib.dll"), b"MZ" * 50)
        write(os.path.join(self.src, "c", "weird"), b"no extension")
        write(os.path.join(self.src, "d", "invoice_march.txt"), "another invoice with the same name")
        write(os.path.join(self.src, ".git", "config"), "must be skipped")
        with zipfile.ZipFile(os.path.join(self.src, "d", "contract.docx"), "w") as z:
            z.writestr("word/document.xml", "<w:t>This contract and the contract terms</w:t>")
        self.before = snapshot(self.src)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------------------------
    def dry(self, **opts):
        return engine.dry_run(self.db, self.src, self.dst, opts, self.rep)

    def test_dry_run_changes_nothing_and_builds_plan(self):
        s = self.dry()
        self.assertFalse(os.path.exists(self.dst), "dry run must not create the destination")
        self.assertEqual(snapshot(self.src), self.before)
        self.assertEqual(s["files"], 12)                      # .git skipped
        self.assertEqual(s["duplicate_files"], 1)
        conn = db.connect(self.db)
        rows = dict(conn.execute("SELECT status, COUNT(*) FROM plan GROUP BY status").fetchall())
        self.assertEqual(rows, {"planned": 10, "skipped": 2})  # exe + dll stay in place
        self.assertTrue(os.path.exists(s["report_csv"]))

    def test_categories_subcategories_and_duplicates(self):
        self.dry()
        conn = db.connect(self.db)
        cat = dict(conn.execute("SELECT path, category FROM files").fetchall())
        self.assertEqual(cat[os.path.join(self.src, "c", "weird")], "Other")
        sub = {os.path.basename(p) + "|" + os.path.basename(os.path.dirname(p)): s
               for p, s in conn.execute("SELECT path, subcategory FROM files WHERE category='Documents'")}
        self.assertEqual(sub["invoice_march.txt|a"], "Invoices")
        self.assertEqual(sub["notes.txt|a"], "Tax")             # text hits only
        self.assertIsNone(sub["random.txt|b"])
        self.assertEqual(sub["contract.docx|d"], "Contracts")   # read from inside the docx
        dups = [os.path.basename(p) for (p,) in conn.execute("SELECT path FROM files WHERE is_dup=1")]
        self.assertEqual(len(dups), 1)
        self.assertIn(dups[0], ("pic.png", "pic_copy.png"))
        self.assertNotIn("pic_other.png", dups)

    def test_colliding_names_get_suffixes(self):
        self.dry()
        conn = db.connect(self.db)
        dsts = [d for (d,) in conn.execute("SELECT dst FROM plan WHERE dst IS NOT NULL")]
        self.assertEqual(len(dsts), len({d.lower() for d in dsts}))
        invoices = [d for d in dsts if "invoice_march" in d]
        self.assertEqual(len(invoices), 2)

    def test_execute_requires_dry_run(self):
        with self.assertRaises(RuntimeError):
            engine.execute(self.db, self.src, self.dst, "copy", {}, self.rep)

    def test_copy_then_undo(self):
        self.dry()
        r = engine.execute(self.db, self.src, self.dst, "copy", {"verify": True}, self.rep)
        self.assertEqual((r["done"], r["failed"]), (10, 0))
        self.assertEqual(snapshot(self.src), self.before, "copy must leave originals intact")
        self.assertEqual(len(snapshot(self.dst)), 10)
        self.assertTrue(os.path.exists(os.path.join(self.dst, "_Duplicates")))
        u = engine.undo(self.db, self.rep)
        self.assertTrue(u["complete"])
        self.assertEqual(snapshot(self.src), self.before)
        self.assertEqual(os.listdir(self.dst), [], "undo should prune the folders it created")

    def test_move_then_undo_restores_everything(self):
        self.dry()
        r = engine.execute(self.db, self.src, self.dst, "move", {}, self.rep)
        self.assertEqual(r["done"], 10)
        remaining = snapshot(self.src)
        self.assertEqual(set(remaining), {os.path.join("c", "setup.exe"), os.path.join("c", "lib.dll"),
                                          os.path.join(".git", "config")})
        self.assertEqual(len(snapshot(self.dst)), 10)
        u = engine.undo(self.db, self.rep)
        self.assertTrue(u["complete"])
        self.assertEqual(snapshot(self.src), self.before)

    @unittest.skipIf(os.name == "nt", "symlink privileges vary on Windows")
    def test_symlink_then_undo(self):
        self.dry()
        r = engine.execute(self.db, self.src, self.dst, "symlink", {}, self.rep)
        self.assertEqual(r["done"], 10)
        links = [os.path.join(d, n) for d, _, fs in os.walk(self.dst) for n in fs]
        self.assertTrue(all(os.path.islink(p) for p in links))
        self.assertEqual(snapshot(self.src), self.before)
        self.assertTrue(engine.undo(self.db, self.rep)["complete"])
        self.assertEqual(os.listdir(self.dst), [])

    def test_changed_source_is_skipped_not_clobbered(self):
        self.dry()
        write(os.path.join(self.src, "b", "random.txt"), "edited after the dry run, longer now")
        r = engine.execute(self.db, self.src, self.dst, "move", {}, self.rep)
        self.assertEqual(r["changed"], 1)
        self.assertTrue(os.path.exists(os.path.join(self.src, "b", "random.txt")))

    def test_existing_destination_file_is_never_overwritten(self):
        self.dry()
        # something appears at a planned destination between dry run and execute
        conn = db.connect(self.db)
        dst = conn.execute("SELECT dst FROM plan WHERE dst LIKE '%random.txt'").fetchone()[0]
        write(dst, "precious pre-existing file")
        r = engine.execute(self.db, self.src, self.dst, "copy", {}, self.rep)
        self.assertEqual(r["failed"], 1)
        with open(dst) as f:
            self.assertEqual(f.read(), "precious pre-existing file")

    def test_cancel_then_undo(self):
        self.dry()
        rep = Reporter(lambda m: None, min_interval=0)
        calls = {"n": 0}
        orig = rep.progress

        def progress(*a, **k):
            calls["n"] += 1
            if a[0] == "Executing" and a[1] >= 3:
                rep.cancel.set()
            orig(*a, **k)
        rep.progress = progress
        r = engine.execute(self.db, self.src, self.dst, "move", {}, rep)
        self.assertEqual(r["status"], "cancelled")
        self.assertTrue(0 < r["done"] < 10)
        self.assertTrue(engine.undo(self.db, self.rep)["complete"])
        self.assertEqual(snapshot(self.src), self.before)

    def test_destination_inside_source_is_rejected(self):
        with self.assertRaises(RuntimeError):
            engine.dry_run(self.db, self.src, os.path.join(self.src, "out"), {}, self.rep)

    def test_cancelled_dry_run_leaves_no_executable_plan(self):
        rep = Reporter(lambda m: None)
        rep.cancel.set()
        with self.assertRaises(Cancelled):
            engine.dry_run(self.db, self.src, self.dst, {}, rep)
        self.assertIsNone(engine.plan_overview(self.db, self.src, self.dst))

    def test_rerun_dry_run_uses_hash_cache_and_replaces_old_data(self):
        self.dry()
        self.dry()
        conn = db.connect(self.db)
        runs = conn.execute("SELECT COUNT(DISTINCT run_id) FROM files").fetchone()[0]
        self.assertEqual(runs, 1)
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM hash_cache").fetchone()[0], 0)

    def test_classify_text_rules(self):
        self.assertEqual(analyzer.classify_text(None, "Invoice-2024.pdf"), "Invoices")
        self.assertIsNone(analyzer.classify_text("a single mention of tax", "doc.txt"))
        self.assertEqual(analyzer.classify_text("receipt receipt total", "x.txt"), "Receipts")


if __name__ == "__main__":
    unittest.main()
