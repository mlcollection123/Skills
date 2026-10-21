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

from file_organizer import analyzer, db, engine, lexicon, photos, projects  # noqa: E402
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


def make_photo(path, when=None, gps=None, shade=0):
    """Write a tiny JPEG with EXIF capture time and/or GPS (degrees, minutes, seconds)."""
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ex = Image.Exif()
    if when:
        ex[306] = when
    if gps:
        (la, lo) = gps
        ex[0x8825] = {1: "N" if la >= 0 else "S", 2: (abs(int(la)), int(abs(la) % 1 * 60), 0.0),
                      3: "E" if lo >= 0 else "W", 4: (abs(int(lo)), int(abs(lo) % 1 * 60), 0.0)}
    Image.new("RGB", (8 + shade, 8), (shade * 7 % 255, shade * 13 % 255, 90)).save(path, exif=ex)


@unittest.skipUnless(all(photos.available()), "needs: pip install Pillow geonamescache")
class PhotoTests(unittest.TestCase):
    BERLIN, MUNICH, PARIS, ROME = (52.52, 13.40), (48.14, 11.57), (48.85, 2.35), (41.90, 12.50)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src, self.dst = os.path.join(self.tmp, "src"), os.path.join(self.tmp, "dst")
        self.db = os.path.join(self.tmp, "index.db")
        os.environ["FILE_ORGANIZER_HOME"] = os.path.join(self.tmp, "home")
        self.rep = Reporter(lambda m: None, min_interval=0)
        n = 0
        for i, (gps, day) in enumerate([(self.BERLIN, 10), (self.BERLIN, 11), (self.MUNICH, 14),
                                        (self.MUNICH, 15)]):          # one Germany trip
            n += 1
            make_photo(f"{self.src}/DCIM/100APPLE/g{i}.jpg", f"2025:07:{day:02d} 10:00:00", gps, n)
        for i, day in enumerate((3, 4, 5)):                              # one France trip
            n += 1
            make_photo(f"{self.src}/DCIM/100APPLE/f{i}.jpg", f"2025:08:{day:02d} 12:00:00", self.PARIS, n)
        n += 1
        make_photo(f"{self.src}/DCIM/100APPLE/rome.jpg", "2025:09:01 09:00:00", self.ROME, n)   # stray
        for i in range(2):                                               # no GPS, in a named folder
            n += 1
            make_photo(f"{self.src}/Birthday Party/b{i}.jpg", f"2024:05:0{i + 1} 18:00:00", None, n)
        n += 1
        make_photo(f"{self.src}/misc/dated.jpg", "2023:03:09 08:00:00", None, n)               # date only
        n += 1
        make_photo(f"{self.src}/misc/nometa.jpg", None, None, n)                              # no EXIF
        n += 1
        make_photo(f"{self.src}/misc/IMG-20220102-WA0001.jpg", None, None, n)                 # date in name

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plan(self, **opts):
        engine.dry_run(self.db, self.src, self.dst, opts, self.rep)
        conn = db.connect(self.db)
        return {os.path.basename(s): os.path.relpath(d, self.dst).replace("\\", "/")
                for s, d in conn.execute("SELECT src, dst FROM plan WHERE dst IS NOT NULL")}

    def test_trips_events_and_dates(self):
        p = self.plan()
        for f in ("g0.jpg", "g1.jpg", "g2.jpg", "g3.jpg"):
            self.assertEqual(os.path.dirname(p[f]), "Photos/Travel/Germany 2025", f)
        for f in ("f0.jpg", "f1.jpg", "f2.jpg"):
            self.assertEqual(os.path.dirname(p[f]), "Photos/Travel/France 2025", f)
        self.assertEqual(os.path.dirname(p["rome.jpg"]), "Photos/2025/2025-09")   # 1 photo: no trip
        self.assertEqual(os.path.dirname(p["b0.jpg"]), "Photos/Events/Birthday Party 2024")
        self.assertEqual(os.path.dirname(p["dated.jpg"]), "Photos/2023/2023-03")
        self.assertEqual(os.path.dirname(p["IMG-20220102-WA0001.jpg"]), "Photos/2022/2022-01")
        self.assertEqual(os.path.dirname(p["nometa.jpg"]), "Graphics-Visuals/JPG")  # no date: old rules

    def test_home_country_photos_are_not_trips(self):
        p = self.plan(home_country="FR")
        self.assertEqual(os.path.dirname(p["f0.jpg"]), "Photos/2025/2025-08")
        self.assertEqual(os.path.dirname(p["g0.jpg"]), "Photos/Travel/Germany 2025")

    def test_two_trips_same_country_same_year_get_months(self):
        make_photo(f"{self.src}/DCIM/100APPLE/g9.jpg", "2025:10:20 10:00:00", self.BERLIN, 40)
        make_photo(f"{self.src}/DCIM/100APPLE/g10.jpg", "2025:10:21 10:00:00", self.BERLIN, 41)
        make_photo(f"{self.src}/DCIM/100APPLE/g11.jpg", "2025:10:22 10:00:00", self.BERLIN, 42)
        p = self.plan()
        self.assertEqual(os.path.dirname(p["g0.jpg"]), "Photos/Travel/Germany 2025-07")
        self.assertEqual(os.path.dirname(p["g9.jpg"]), "Photos/Travel/Germany 2025-10")

    def test_switch_off_restores_plain_folders(self):
        p = self.plan(smart_photos=False)
        self.assertEqual(os.path.dirname(p["g0.jpg"]), "Graphics-Visuals/JPG")

    def test_geocoder(self):
        geo = photos.Geo()
        self.assertEqual(geo.country(*self.BERLIN), "DE")
        self.assertEqual(geo.country(*self.PARIS), "FR")
        self.assertIsNone(geo.country(0.5, -30.0))          # middle of the Atlantic

    def test_folder_hint(self):
        self.assertIsNone(photos.folder_hint("/x/DCIM/100APPLE/a.jpg"))
        self.assertIsNone(photos.folder_hint("/x/New folder (3)/a.jpg"))
        self.assertEqual(photos.folder_hint("/x/Summer Holiday/a.jpg"), "Summer Holiday")

    def test_executes_and_undoes_photo_layout(self):
        self.plan()
        before = snapshot(self.src)
        r = engine.execute(self.db, self.src, self.dst, "copy", {}, self.rep)
        self.assertEqual(r["failed"], 0)
        self.assertTrue(os.path.isdir(os.path.join(self.dst, "Photos", "Travel", "Germany 2025")))
        self.assertTrue(engine.undo(self.db, self.rep)["complete"])
        self.assertEqual(snapshot(self.src), before)
        self.assertEqual(os.listdir(self.dst), [])


class ProjectTests(unittest.TestCase):
    """Synthetic data only: nothing in here comes from a real person's documents."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src, self.dst = os.path.join(self.tmp, "in"), os.path.join(self.tmp, "out")
        self.db = os.path.join(self.tmp, "index.db")
        os.environ["FILE_ORGANIZER_HOME"] = os.path.join(self.tmp, "home")
        self.rep = Reporter(lambda m: None, min_interval=0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plan(self, **opts):
        opts.setdefault("detect_dups", False)       # the fake files are tiny and similar
        engine.dry_run(self.db, self.src, self.dst, opts, self.rep)
        conn = db.connect(self.db)
        return {os.path.basename(s): os.path.relpath(d, self.dst).replace("\\", "/")
                for s, d in conn.execute("SELECT src, dst FROM plan WHERE dst IS NOT NULL")}

    def test_spellings_and_scripts_are_one_word(self):
        keys = {lexicon.canon(w) for w in ("Ορχιδιά", "Orchidia", "ORCHIDIA", "ΟΡΧΙΔΙΑ", "ορχιδιών")}
        self.assertEqual(len(keys), 1)
        self.assertEqual(lexicon.codes("RΧ-48213_Reservation"), lexicon.codes("RX-48213 signed"))

    def test_groups_by_name_across_languages_and_drafts(self):
        d = self.src
        write(f"{d}/Orchidia - site plan.txt", "site plan")
        write(f"{d}/Orchidia - house drawings.txt", "drawings")
        write(f"{d}/Ορχιδιά Πληρωμή 1.txt", "payment")
        write(f"{d}/Ορχιδιά Πληρωμή 2.txt", "payment")
        write(f"{d}/Ορχιδιά - Τίτλος Ιδιοκτησίας.txt", "title")
        write(f"{d}/RX-48213 Reservation Agreement.txt", "the reservation of Orchidia plot, Orchidia village")
        write(f"{d}/RΧ-48213 Reservation Agreement Signed.txt", "")
        # an unrelated project and some loners
        for n in ("Kitchen Renovation quote", "Kitchen Renovation invoice", "Kitchen Renovation photos list"):
            write(f"{d}/{n}.txt", "oak cabinets")
        write(f"{d}/shopping.txt", "milk")
        write(f"{d}/lunch.txt", "pasta")
        p = self.plan()
        folders = {n: f.split("/")[1] for n, f in p.items() if f.startswith("Projects/")}
        orchidia = {n for n, proj in folders.items() if proj == folders["Orchidia - site plan.txt"]}
        self.assertEqual(len(orchidia), 7, folders)                         # Greek + Latin + signed/draft
        self.assertIn("RΧ-48213 Reservation Agreement Signed.txt", orchidia)
        kitchen = {n for n, proj in folders.items() if "Kitchen" in n}
        self.assertEqual(len({folders[n] for n in kitchen}), 1)
        self.assertNotEqual(folders["Kitchen Renovation quote.txt"], folders["Orchidia - site plan.txt"])
        self.assertNotIn("shopping.txt", folders)                           # unrelated files stay out
        self.assertNotIn("lunch.txt", folders)

    def test_roles_drafts_and_inherited_roles(self):
        d = self.src
        write(f"{d}/Orchid House - payment 1.txt", "x")
        write(f"{d}/Orchid House - payment 2.txt", "x")
        write(f"{d}/Orchid House - floor plan.txt", "x")
        write(f"{d}/Orchid House - Lease Agreement.txt", "x")
        write(f"{d}/Orchid House - Lease Agreement Signed.txt", "x")
        write(f"{d}/Orchid House - Lease Agreement Draft.txt", "x")
        p = self.plan()
        self.assertEqual(os.path.dirname(p["Orchid House - payment 1.txt"]).split("/")[-1], "Payments & Banking")
        self.assertEqual(os.path.dirname(p["Orchid House - floor plan.txt"]).split("/")[-1], "Design & Drawings")
        self.assertEqual(os.path.dirname(p["Orchid House - Lease Agreement Signed.txt"]).split("/")[-1],
                         "Contracts & Agreements")
        self.assertTrue(p["Orchid House - Lease Agreement Draft.txt"].endswith(
            "Contracts & Agreements/Drafts/Orchid House - Lease Agreement Draft.txt"))

    def test_keeps_a_meaningful_source_subfolder_and_ignores_generic_ones(self):
        d = self.src
        for n in ("a", "b", "c", "d"):
            write(f"{d}/Orchid House/Acme Ltd/{n}.txt", f"Acme contract {n}")
        write(f"{d}/Orchid House/Orchid House - plan.txt", "x")
        write(f"{d}/Orchid House/Orchid House - tax.txt", "x")
        write(f"{d}/Orchid House/Downloads/Orchid House - id.txt", "x")
        p = self.plan()
        self.assertTrue(p["a.txt"].startswith("Projects/Orchid House/Acme Ltd/"), p["a.txt"])
        self.assertNotIn("Downloads", p["Orchid House - id.txt"])
        self.assertNotIn("Orchid House/Orchid House", p["Orchid House - plan.txt"])   # no folder repeating the project

    def test_scan_root_name_is_not_a_project(self):
        root = os.path.join(self.tmp, "Archive 2019 stuff")
        self.src = root
        for n in ("alpha one", "beta two", "gamma three", "delta four"):
            write(f"{root}/{n}.txt", "x")
        p = self.plan()
        self.assertFalse(any(f.startswith("Projects/") for f in p.values()), p)

    def test_user_rules_force_a_project_and_ignore_list_blocks_a_word(self):
        d = self.src
        write(f"{d}/zebra notes.txt", "x")
        write(f"{d}/zebra budget.txt", "x")
        write(f"{d}/ζέβρα photos.txt", "x")
        home = os.environ["FILE_ORGANIZER_HOME"]
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "projects.json"), "w", encoding="utf-8") as fh:
            fh.write('{"Safari Trip": ["zebra"], "ignore": ["budget"]}')
        p = self.plan()
        self.assertTrue(all(p[n].startswith("Projects/Safari Trip/") for n in ("zebra notes.txt", "zebra budget.txt",
                                                                                 "ζέβρα photos.txt")), p)

    def test_two_name_free_scans_stay_unassigned(self):
        d = self.src
        for n in ("scan0001", "scan0002", "scan0003", "scan0004"):
            write(f"{d}/{n}.txt", "x")
        p = self.plan()
        self.assertFalse(any(f.startswith("Projects/") for f in p.values()), p)

    def test_grouping_can_be_switched_off(self):
        d = self.src
        for n in ("Orchid House plan", "Orchid House tax", "Orchid House id"):
            write(f"{d}/{n}.txt", "x")
        p = self.plan(group_projects=False)
        self.assertFalse(any(f.startswith("Projects/") for f in p.values()), p)

    def test_project_files_execute_and_undo(self):
        d = self.src
        for n in ("Orchid House plan", "Orchid House tax", "Orchid House id"):
            write(f"{d}/{n}.txt", n)
        before = snapshot(self.src)
        self.plan()
        r = engine.execute(self.db, self.src, self.dst, "move", {}, self.rep)
        self.assertEqual(r["failed"], 0)
        self.assertTrue(os.path.isdir(os.path.join(self.dst, "Projects", "Orchid House")))
        self.assertTrue(engine.undo(self.db, self.rep)["complete"])
        self.assertEqual(snapshot(self.src), before)


@unittest.skipUnless(all(photos.available()), "needs: pip install Pillow geonamescache")
class RealDriveLayoutTests(unittest.TestCase):
    """A drive like a real one (invented names): project papers, a drone folder with JPEG+RAW+video pairs,
    and loose documents that belong to nothing."""
    LIMASSOL = (34.68, 33.04)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src, self.dst = os.path.join(self.tmp, "E"), os.path.join(self.tmp, "out")
        self.db = os.path.join(self.tmp, "index.db")
        os.environ["FILE_ORGANIZER_HOME"] = os.path.join(self.tmp, "home")
        self.rep = Reporter(lambda m: None, min_interval=0)
        d = self.src
        # project documents, some inside a counterparty folder with drafts
        for n in ("site plan", "house drawings", "Orchidia title deed", "Orchidia payment 1", "Orchidia payment 2"):
            write(f"{d}/Orchidia Plot/orchidia {n}.txt", n)
        write(f"{d}/Orchidia Plot/Acme Ltd/RX-48213 Reservation Agreement Signed.txt", "signed")
        write(f"{d}/Orchidia Plot/Acme Ltd/ID card.txt", "id")
        write(f"{d}/Orchidia Plot/Acme Ltd/Draft/RX-48213 Reservation Agreement.txt",
              "agreement for the Orchidia plot, Orchidia village")
        write(f"{d}/Orchidia Plot/Acme Ltd/Bank statement.txt", "bank")
        # a planning application for the same plot, in its own folder
        for n in ("1_Orchidia zoning section", "2_Orchidia title", "3_Orchidia survey", "4_Orchidia application"):
            write(f"{d}/Preliminary Query OA8/{n}.txt", n)
        # drone folder: JPEG with EXIF, RAW twin without, videos and subtitle files
        for i in range(6):
            stem = f"DJI_2026092711{20 + i}00_{160 + i:04d}_D"
            make_photo(f"{d}/Orchidia Drone 27.09.2026/DJI_001/{stem}.JPG", "2026:09:27 11:2%d:00" % i,
                       self.LIMASSOL, 10 + i)
            write(f"{d}/Orchidia Drone 27.09.2026/DJI_001/{stem}.DNG", b"not really raw %d" % i)
        for i in range(3):
            stem = f"DJI_2026092711{30 + i}00_{170 + i:04d}_D"
            write(f"{d}/Orchidia Drone 27.09.2026/DJI_001/{stem}.MP4", b"video %d" % i)
            write(f"{d}/Orchidia Drone 27.09.2026/DJI_001/{stem}.SRT", b"subs %d" % i)
        # loose, unrelated documents
        write(f"{d}/John_Smith_Family_Trust_Deed.txt", "trust of John Smith")
        write(f"{d}/John Smith - passport.txt", "passport")
        write(f"{d}/Everyday Problem app.txt", "individuals individuals individuals")
        write(f"{d}/Rental Document.txt", "document document document")
        write(f"{d}/Book1.txt", "tax tax")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plan(self, **opts):
        opts.setdefault("detect_dups", False)
        engine.dry_run(self.db, self.src, self.dst, opts, self.rep)
        conn = db.connect(self.db)
        return {os.path.basename(s): os.path.relpath(d, self.dst).replace("\\", "/")
                for s, d in conn.execute("SELECT src, dst FROM plan WHERE dst IS NOT NULL")}

    def test_drive_is_sorted_the_way_a_person_would(self):
        p = self.plan()
        projects = {f.split("/")[1] for f in p.values() if f.startswith("Projects/")}
        self.assertEqual(projects, {"Orchidia"}, p)              # one project, no DJI_ or OA8 fragments
        self.assertTrue(p["orchidia site plan.txt"].startswith("Projects/Orchidia/Design & Drawings/"), p)
        # the counterparty folder is kept, drafts inside it stay together
        self.assertEqual(p["ID card.txt"], "Projects/Orchidia/Acme Ltd/ID card.txt")
        self.assertEqual(p["RX-48213 Reservation Agreement Signed.txt"],
                         "Projects/Orchidia/Acme Ltd/RX-48213 Reservation Agreement Signed.txt")
        self.assertTrue(p["RX-48213 Reservation Agreement.txt"].endswith("Acme Ltd/Drafts/RX-48213 Reservation Agreement.txt"))
        # the application folder is kept as its own sub-folder of the project
        self.assertTrue(p["1_Orchidia zoning section.txt"].startswith("Projects/Orchidia/Preliminary Query OA8/"), p)

    def test_drone_folder_joins_the_project_with_raw_and_video_split(self):
        p = self.plan()
        jpg = "DJI_202609271120" + "00_0160_D"
        self.assertEqual(os.path.dirname(p[f"{jpg}.JPG"]), "Projects/Orchidia/Drone 27.09.2026")
        self.assertEqual(os.path.dirname(p[f"{jpg}.DNG"]), "Projects/Orchidia/Drone 27.09.2026/RAW")
        mp4 = "DJI_202609271130" + "00_0170_D"
        self.assertEqual(os.path.dirname(p[f"{mp4}.MP4"]), "Projects/Orchidia/Drone 27.09.2026/Videos")
        self.assertEqual(os.path.dirname(p[f"{mp4}.SRT"]), "Projects/Orchidia/Drone 27.09.2026/Videos")

    def test_without_a_matching_project_media_form_a_trip_with_raw_and_videos(self):
        shutil.rmtree(os.path.join(self.src, "Orchidia Plot"))
        shutil.rmtree(os.path.join(self.src, "Preliminary Query OA8"))
        p = self.plan()
        jpg = "DJI_202609271120" + "00_0160_D"
        base = "Photos/Travel/Cyprus 2026/Orchidia Drone 27.09.2026"
        self.assertEqual(os.path.dirname(p[f"{jpg}.JPG"]), base)
        self.assertEqual(os.path.dirname(p[f"{jpg}.DNG"]), base + "/RAW")        # twin of the JPEG
        self.assertEqual(os.path.dirname(p["DJI_20260927113000_0170_D.MP4"]), base + "/Videos")  # dated by name

    def test_home_country_makes_it_an_event_not_a_trip(self):
        shutil.rmtree(os.path.join(self.src, "Orchidia Plot"))
        shutil.rmtree(os.path.join(self.src, "Preliminary Query OA8"))
        p = self.plan(home_country="CY")
        self.assertTrue(p["DJI_20260927112000_0160_D.JPG"].startswith("Photos/Events/Orchidia Drone 27.09.2026"), p)

    def test_loose_documents_and_owner_name_do_not_join_the_project(self):
        p = self.plan()
        for n in ("Everyday Problem app.txt", "Rental Document.txt", "Book1.txt"):
            self.assertFalse(p[n].startswith("Projects/"), (n, p[n]))
        # two files sharing only the owner's name are not a "project"
        self.assertFalse(p["John Smith - passport.txt"].startswith("Projects/Orchidia"), p)

    def test_camera_timestamps_are_not_reference_codes(self):
        self.assertEqual(lexicon.codes("DJI_20260927112754_0166_D"), set())
        self.assertEqual(lexicon.codes("GOPR1234"), set())


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src, self.dst = os.path.join(self.tmp, "src"), os.path.join(self.tmp, "out")
        self.db = os.path.join(self.tmp, "index.db")
        os.environ["FILE_ORGANIZER_HOME"] = os.path.join(self.tmp, "home")
        for i in range(4):
            write(f"{self.src}/Birch Farm/birch farm invoice {i}.txt", f"birch {i}")
        for i in range(3):
            make_photo(f"{self.src}/pics/IMG_{i}.jpg", f"2025:05:0{i + 1} 10:00:00", (52.52, 13.40), i)
        write(f"{self.src}/a.txt", "same")
        write(f"{self.src}/b.txt", "same")
        engine.dry_run(self.db, self.src, self.dst, {}, Reporter(lambda m: None, min_interval=0))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_report_numbers_and_html(self):
        from file_organizer import report
        conn = db.connect(self.db)
        rid = report.latest_dry_run(conn)
        d = report.build_report(conn, rid)
        self.assertEqual(d["totals"]["files"], 9)
        self.assertEqual(d["totals"]["duplicates"], 1)
        self.assertEqual([p["label"] for p in d["projects"]], ["Birch Farm"])
        self.assertEqual(d["totals"]["trips"], 1)
        self.assertEqual(sum(m["photos"] for m in d["months"]), 3)
        self.assertTrue(any(i["kind"] == "tip" for i in d["insights"]))
        path = report.write_html(conn, rid, os.path.join(self.tmp, "r.html"))
        html = open(path, encoding="utf-8").read()
        self.assertIn("Birch Farm", html)
        self.assertNotIn("</script><script>alert", html)
        rows, total = report.query_rows(conn, rid, self.dst, "invoice", "Birch Farm")
        self.assertEqual((len(rows), total), (4, 4))
        self.assertFalse(os.path.isabs(rows[0][0]))
        conn.close()

    def test_my_names_never_become_a_project(self):
        for i in range(4):
            write(f"{self.src}/Jo Bloggs/jo bloggs passport {i}.txt", f"jo {i}")
        engine.dry_run(self.db, self.src, self.dst, {"my_names": "Jo Bloggs"}, Reporter(lambda m: None, min_interval=0))
        conn = db.connect(self.db)
        names = {r[0] for r in conn.execute("SELECT DISTINCT project FROM files WHERE project IS NOT NULL "
                                            "AND run_id=(SELECT MAX(id) FROM runs)")}
        conn.close()
        self.assertEqual(names, {"Birch Farm"})

    def test_charts_render(self):
        from file_organizer import charts
        for img, size in ((charts.donut([("a", 3), ("b", 1)], 100, 14, "4", "files"), 100),
                          (charts.ring(0.5, 100, 10, "50%", "x"), 100)):
            self.assertEqual(img.size, (size * charts.OUT, size * charts.OUT))
        self.assertEqual(charts.hbars([("x", 5, "5")], 200, 30, 80).size, (200 * charts.OUT, 30 * charts.OUT))
        self.assertTrue(charts.timeline([], 300, 100))


class SortioParityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src, self.dst = os.path.join(self.tmp, "src"), os.path.join(self.tmp, "out")
        self.db = os.path.join(self.tmp, "index.db")
        os.environ["FILE_ORGANIZER_HOME"] = os.path.join(self.tmp, "home")
        self.rep = Reporter(lambda m: None, min_interval=0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def targets(self):
        conn = db.connect(self.db)
        out = {os.path.basename(s): os.path.relpath(d, self.dst).replace("\\", "/")
               for s, d in conn.execute("SELECT src, dst FROM plan WHERE dst IS NOT NULL")}
        conn.close()
        return out

    def test_find_fields_in_a_bill(self):
        from file_organizer import rename
        text = "ACME Plumbing Ltd\nInvoice date: 14 May 2026\nPipe repair EUR 1,000.00\nTotal due: \u20ac1,240.00"
        self.assertEqual(rename.propose_name(text, ".pdf"), "2026-05-14 ACME Plumbing Ltd \u20ac1,240.00.pdf")
        self.assertEqual(rename.find_date("paid on 03/02/2026"), "2026-02-03")      # day first
        self.assertIsNone(rename.propose_name("nothing useful in here", ".pdf"))

    def test_smart_rename_is_opt_in_and_only_for_bills(self):
        write(f"{self.src}/scan001.txt", "ACME Plumbing Ltd\nInvoice date: 2026-05-14\nTotal due: \u20ac99.50\n")
        write(f"{self.src}/invoice_old.txt", "ACME Plumbing Ltd\nInvoice date: 2026-05-14\nTotal due: \u20ac99.50\n")
        write(f"{self.src}/notes.txt", "ACME Plumbing Ltd\nInvoice date: 2026-05-14\nTotal due: \u20ac99.50\n")
        engine.dry_run(self.db, self.src, self.dst, {"detect_dups": False}, self.rep)
        self.assertIn("invoice_old.txt", self.targets())                    # off by default
        engine.dry_run(self.db, self.src, self.dst, {"detect_dups": False, "smart_rename": True}, self.rep)
        t = self.targets()
        self.assertTrue(t["invoice_old.txt"].endswith("2026-05-14 ACME Plumbing Ltd \u20ac99.50.txt"), t)
        self.assertTrue(t["notes.txt"].endswith("/notes.txt"), t)           # not a bill by name or role
        self.assertTrue(t["scan001.txt"].endswith("/scan001.txt"), t)

    def test_user_rule_wins_and_empty_rule_is_ignored(self):
        from file_organizer import userrules
        write(f"{self.src}/tax return 2025.pdf", b"x")
        write(f"{self.src}/holiday.pdf", b"y")
        userrules.save([{"name": "all", "to": "Everything"},
                        {"name": "Tax", "name_contains": ["tax"], "ext": ["pdf"], "to": "Finance/Tax/{year}"}])
        engine.dry_run(self.db, self.src, self.dst, {"detect_dups": False}, self.rep)
        t = self.targets()
        self.assertRegex(t["tax return 2025.pdf"], r"^Finance/Tax/\d{4}/")
        self.assertNotIn("Everything", t["holiday.pdf"])

    def test_rule_cannot_escape_destination(self):
        from file_organizer import userrules
        rules = userrules.compile_rules([{"name": "x", "ext": ["pdf"], "to": "../../etc"}])
        hit = userrules.match(rules, "a.pdf", ".pdf", 1, 0, "Documents")
        self.assertNotIn("..", hit[1].split(os.sep))
        self.assertFalse(os.path.isabs(hit[1]))

    def test_audit_log_and_fingerprint(self):
        from file_organizer import report
        write(f"{self.src}/a.txt", "a")
        engine.dry_run(self.db, self.src, self.dst, {}, self.rep)
        res = engine.execute(self.db, self.src, self.dst, "copy", {}, self.rep)
        self.assertTrue(os.path.exists(res["audit_csv"]))
        self.assertIn("a.txt", open(res["audit_csv"], encoding="utf-8-sig").read())
        fp = report.fingerprint(self.src)
        write(f"{self.src}/b.txt", "b")
        self.assertNotEqual(report.fingerprint(self.src), fp)


if __name__ == "__main__":
    unittest.main()
