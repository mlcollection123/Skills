"""Desktop dashboard (customtkinter). Heavy work runs on one background thread; the UI only polls a queue."""
from __future__ import annotations

import json
import os
import pathlib
import queue
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from . import charts, db, engine, report, userrules
from .common import Cancelled, Reporter, human_size
from .config import HASH_WORKERS, app_dir, db_path, report_dir

BG, PANEL, CARD, LINE = "#0b0e17", "#0f1424", "#161d33", "#243049"
TEXT, MUTED, ACCENT, MINT, WARN, DANGER = "#e8ecf6", "#8a96b2", "#7c8cff", "#6ee7d8", "#fbbf24", "#f87171"
FONT = "Segoe UI"
MODES = {"Copy": "copy", "Move": "move", "Symlink": "symlink"}
MAX_LOG_CHARS = 400_000
PAGE_ROWS = 200
NAV = ["Organize", "Insights", "Review", "Rules", "Settings", "Activity"]
SETTINGS_KEYS = ("src", "dst", "mode", "include_exec", "segregate", "detect_dups", "read_content", "group_projects",
                 "smart_photos", "smart_rename", "watch", "watch_min", "my_names", "home", "verify", "algo", "workers")


def _settings_file() -> str:
    return os.path.join(app_dir(), "settings.json")


class App(ctk.CTk):
    def __init__(self) -> None:
        ctk.set_appearance_mode("dark")
        super().__init__(fg_color=BG)
        self.title("File Organizer")
        self.geometry("1180x780")
        self.minsize(1000, 680)

        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.closing = False
        self._phase = None
        self._phase_t0 = 0.0
        self._phase_n0 = 0
        self._ring_at = 0.0
        self._job_t0 = 0.0
        self._images: dict[str, ctk.CTkImage] = {}
        self.last_html: str | None = None
        self.last_csv: str | None = None
        self.review_run: int | None = None
        self.review_page = 0
        self.review_total = 0

        self.src, self.dst = tk.StringVar(), tk.StringVar()
        self.mode = tk.StringVar(value="Copy")
        self.include_exec, self.segregate = tk.BooleanVar(value=False), tk.BooleanVar(value=True)
        self.detect_dups, self.read_content = tk.BooleanVar(value=True), tk.BooleanVar(value=True)
        self.group_projects, self.smart_photos = tk.BooleanVar(value=True), tk.BooleanVar(value=True)
        self.my_names, self.home = tk.StringVar(), tk.StringVar()
        self.verify = tk.BooleanVar(value=False)
        self.smart_rename, self.watch = tk.BooleanVar(value=False), tk.BooleanVar(value=False)
        self.watch_min = tk.IntVar(value=15)
        self._watch_fp = None
        self._watch_last = 0.0
        self.algo = tk.StringVar(value="sha256")
        self.workers = tk.IntVar(value=HASH_WORKERS)
        self._first_run = not os.path.exists(_settings_file())
        self._load_settings()

        self._style_tree()
        self._build_shell()
        self.pages = {name: getattr(self, f"_page_{name.lower()}")() for name in NAV}
        for v in (self.src, self.dst):
            v.trace_add("write", lambda *_: self._refresh_buttons())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.show("Organize")
        self.after(100, self._poll)
        self.after(30000, self._watch_tick)
        self._refresh_buttons()
        self._log("Ready. Pick a source and destination, then press Analyze. Nothing changes until you press Execute.")
        self._find_latest_report()
        self._load_insights()
        if self._first_run:
            self.after(700, self._ask_name)

    # ------------------------------------------------------------------------------ settings
    def _vars(self) -> dict:
        return {"src": self.src, "dst": self.dst, "mode": self.mode, "include_exec": self.include_exec,
                "segregate": self.segregate, "detect_dups": self.detect_dups, "read_content": self.read_content,
                "group_projects": self.group_projects, "smart_photos": self.smart_photos,
                "smart_rename": self.smart_rename, "watch": self.watch, "watch_min": self.watch_min,
                "my_names": self.my_names, "home": self.home, "verify": self.verify, "algo": self.algo,
                "workers": self.workers}

    def _load_settings(self) -> None:
        try:
            with open(_settings_file(), encoding="utf-8") as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            return
        for k, var in self._vars().items():
            if k in saved:
                try:
                    var.set(saved[k])
                except (tk.TclError, ValueError):
                    pass

    def _ask_name(self) -> None:
        """Your own name appears on many papers; ask once so it never becomes a project folder."""
        dlg = ctk.CTkInputDialog(title="Welcome",
                                 text="Your name, so it is never treated as a project folder.\n"
                                      "Several names are fine, separated by commas. You can skip this.")
        name = (dlg.get_input() or "").strip()
        if name:
            self.my_names.set(name)
        self._save_settings()

    def _save_settings(self) -> None:
        try:
            os.makedirs(app_dir(), exist_ok=True)
            data = {}
            for k, var in self._vars().items():
                try:
                    data[k] = var.get()
                except (tk.TclError, ValueError):
                    pass
            with open(_settings_file(), "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
        except OSError:
            pass

    # ---------------------------------------------------------------------------------- shell
    def _style_tree(self) -> None:
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure("Dark.Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT, rowheight=28,
                     borderwidth=0, font=(FONT, 10))
        st.configure("Dark.Treeview.Heading", background=PANEL, foreground=MUTED, relief="flat",
                     font=(FONT, 10, "bold"), borderwidth=0)
        st.layout("Dark.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        st.map("Dark.Treeview", background=[("selected", "#27315a")], foreground=[("selected", TEXT)])
        st.map("Dark.Treeview.Heading", background=[("active", PANEL)])

    def _build_shell(self) -> None:
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        side = ctk.CTkFrame(self, width=210, fg_color=PANEL, corner_radius=0)
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_propagate(False)
        ctk.CTkLabel(side, text="File Organizer", font=(FONT, 20, "bold"), text_color=TEXT).pack(
            anchor="w", padx=22, pady=(26, 0))
        ctk.CTkLabel(side, text="private, on this PC only", font=(FONT, 11), text_color=MUTED).pack(
            anchor="w", padx=22, pady=(0, 26))
        self.nav_btns: dict[str, ctk.CTkButton] = {}
        for name in NAV:
            b = ctk.CTkButton(side, text=name, anchor="w", height=40, corner_radius=12, fg_color="transparent",
                              hover_color="#1a2240", text_color=MUTED, font=(FONT, 13, "bold"),
                              command=lambda n=name: self.show(n))
            b.pack(fill="x", padx=14, pady=2)
            self.nav_btns[name] = b
        self.chip = ctk.CTkLabel(side, text="Idle", font=(FONT, 11), text_color=MUTED, fg_color=CARD,
                                 corner_radius=10, height=30)
        self.chip.pack(side="bottom", fill="x", padx=14, pady=18)
        self.body = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self.body.grid(row=0, column=1, sticky="nsew")
        self.body.rowconfigure(0, weight=1)
        self.body.columnconfigure(0, weight=1)

    def show(self, name: str) -> None:
        for n, page in self.pages.items():
            page.grid_forget()
            self.nav_btns[n].configure(fg_color="#1d2547" if n == name else "transparent",
                                       text_color=TEXT if n == name else MUTED)
        self.pages[name].grid(row=0, column=0, sticky="nsew", padx=28, pady=24)
        if name == "Review":
            self._review_refresh(reset=self.review_run != self._current_run())

    def _card(self, parent, **kw) -> ctk.CTkFrame:
        return ctk.CTkFrame(parent, fg_color=CARD, border_width=1, border_color=LINE, corner_radius=18, **kw)

    def _title(self, parent, title: str, sub: str) -> None:
        ctk.CTkLabel(parent, text=title, font=(FONT, 26, "bold"), text_color=TEXT).pack(anchor="w")
        ctk.CTkLabel(parent, text=sub, font=(FONT, 12), text_color=MUTED).pack(anchor="w", pady=(0, 16))

    def _button(self, parent, text, command, kind="ghost", **kw) -> ctk.CTkButton:
        colors = {"primary": (ACCENT, "#6a79ee", "#ffffff"), "ghost": ("#1d2547", "#27315a", TEXT),
                  "go": ("#1f6f64", "#27877a", "#ffffff"), "danger": ("#4a2430", "#63303f", "#ffd6d6")}[kind]
        return ctk.CTkButton(parent, text=text, command=command, fg_color=colors[0], hover_color=colors[1],
                             text_color=colors[2], corner_radius=12, font=(FONT, 13, "bold"), **{"height": 40, **kw})

    def _img(self, label: ctk.CTkLabel, key: str, pil, size: tuple[int, int]) -> None:
        img = ctk.CTkImage(light_image=pil, dark_image=pil, size=size)
        self._images[key] = img
        label.configure(image=img, text="")

    # --------------------------------------------------------------------------- page: organize
    def _page_organize(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self.body, fg_color="transparent")
        self._title(page, "Organize", "Point it at a messy folder or drive. It plans first and changes nothing until you say so.")
        box = self._card(page)
        box.pack(fill="x")
        box.columnconfigure(1, weight=1)
        for r, (label, var) in enumerate((("Source", self.src), ("Destination", self.dst))):
            ctk.CTkLabel(box, text=label, font=(FONT, 12, "bold"), text_color=MUTED, width=90, anchor="w").grid(
                row=r, column=0, padx=(20, 8), pady=(18 if r == 0 else 8, 8 if r == 0 else 18), sticky="w")
            ctk.CTkEntry(box, textvariable=var, height=38, corner_radius=10, fg_color=PANEL, border_color=LINE,
                         font=(FONT, 12)).grid(row=r, column=1, sticky="ew", pady=(18 if r == 0 else 8, 8 if r == 0 else 18))
            self._button(box, "Browse", lambda v=var: self._browse(v), width=90).grid(
                row=r, column=2, padx=(10, 20), pady=(18 if r == 0 else 8, 8 if r == 0 else 18))

        row = ctk.CTkFrame(page, fg_color="transparent")
        row.pack(fill="x", pady=16)
        self.btn_dry = self._button(row, "Analyze", self.on_dry_run, "primary", width=150)
        self.btn_dry.pack(side="left")
        self.btn_exec = self._button(row, "Execute", self.on_execute, "go", width=130)
        self.btn_exec.pack(side="left", padx=10)
        self.btn_undo = self._button(row, "Undo last", self.on_undo, width=110)
        self.btn_undo.pack(side="left")
        self.btn_cancel = self._button(row, "Cancel", self.cancel.set, "danger", width=100)
        self.btn_cancel.pack(side="left", padx=10)
        seg = ctk.CTkSegmentedButton(row, values=list(MODES), variable=self.mode, height=36, corner_radius=12,
                                     fg_color=PANEL, selected_color=ACCENT, selected_hover_color="#6a79ee",
                                     unselected_color=PANEL, unselected_hover_color="#1d2547", font=(FONT, 12, "bold"))
        seg.pack(side="right")
        ctk.CTkLabel(row, text="On execute:", font=(FONT, 12), text_color=MUTED).pack(side="right", padx=10)
        watch = ctk.CTkFrame(page, fg_color="transparent", height=1)
        watch.pack(fill="x", pady=(0, 12))
        ctk.CTkSwitch(watch, text="Watch the source folder", variable=self.watch, progress_color=ACCENT,
                      button_color="#ffffff", font=(FONT, 12), text_color=TEXT).pack(side="left")
        ctk.CTkLabel(watch, text="  re-analyze when files change, every", font=(FONT, 12), text_color=MUTED).pack(side="left")
        ctk.CTkOptionMenu(watch, variable=tk.StringVar(value=str(self.watch_min.get())), width=70, height=28,
                          values=["5", "15", "30", "60"], fg_color=PANEL, button_color="#1d2547",
                          button_hover_color="#27315a", command=lambda v: self.watch_min.set(int(v))).pack(side="left", padx=6)
        ctk.CTkLabel(watch, text="minutes. It only analyzes; you still press Execute.", font=(FONT, 12),
                     text_color=MUTED).pack(side="left")

        prog = self._card(page)
        prog.pack(fill="both", expand=True)
        prog.columnconfigure(1, weight=1)
        self.ring_lbl = ctk.CTkLabel(prog, text="")
        self.ring_lbl.grid(row=0, column=0, rowspan=3, padx=26, pady=22)
        self.phase_lbl = ctk.CTkLabel(prog, text="Waiting for a job", font=(FONT, 18, "bold"), text_color=TEXT)
        self.phase_lbl.grid(row=0, column=1, sticky="sw", pady=(34, 0))
        self.detail_lbl = ctk.CTkLabel(prog, text="Press Analyze to build a plan.", font=(FONT, 12), text_color=MUTED)
        self.detail_lbl.grid(row=1, column=1, sticky="nw")
        self.result_lbl = ctk.CTkLabel(prog, text="", font=(FONT, 12), text_color=MINT, justify="left", anchor="w")
        self.result_lbl.grid(row=2, column=1, sticky="nw", pady=(8, 0))
        self._img(self.ring_lbl, "ring", charts.ring(0, 170, 14, "0%", "idle"), (170, 170))
        steps = ctk.CTkFrame(prog, fg_color="transparent", height=1)
        steps.grid(row=3, column=0, columnspan=2, sticky="ew", padx=26, pady=(10, 26))
        for i, (head, body) in enumerate((("1  Analyze", "Scans, groups files into projects, trips and types, finds duplicates. Changes nothing."),
                                          ("2  Review", "Check Insights and Review. Open the full report in your browser."),
                                          ("3  Execute", "Copies or moves files. Every step is journaled, so Undo works."))):
            steps.columnconfigure(i, weight=1, uniform="s")
            box = ctk.CTkFrame(steps, fg_color=PANEL, corner_radius=14, height=1)
            box.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
            ctk.CTkLabel(box, text=head, font=(FONT, 13, "bold"), text_color=MINT).pack(anchor="w", padx=16, pady=(14, 2))
            ctk.CTkLabel(box, text=body, font=(FONT, 11), text_color=MUTED, wraplength=220, justify="left",
                         anchor="w").pack(anchor="w", padx=16, pady=(0, 14))
        prog.rowconfigure(3, weight=1)
        return page

    # --------------------------------------------------------------------------- page: insights
    def _page_insights(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self.body, fg_color="transparent")
        head = ctk.CTkFrame(page, fg_color="transparent")
        head.pack(fill="x")
        left = ctk.CTkFrame(head, fg_color="transparent")
        left.pack(side="left")
        self._title(left, "Insights", "What the last analysis found, before anything is touched.")
        self._button(head, "Open full report", self._open_html, "primary", width=150).pack(side="right")
        self._button(head, "CSV", self._open_csv, width=70).pack(side="right", padx=8)
        self.scroll = ctk.CTkScrollableFrame(page, fg_color="transparent", corner_radius=0)
        self.scroll.pack(fill="both", expand=True)
        self.scroll.columnconfigure((0, 1), weight=1, uniform="c")

        self.kpi_row = ctk.CTkFrame(self.scroll, fg_color="transparent")
        self.kpi_row.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.kpis = []
        for i in range(5):
            self.kpi_row.columnconfigure(i, weight=1, uniform="k")
            card = self._card(self.kpi_row)
            card.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
            cap = ctk.CTkLabel(card, text="", font=(FONT, 11), text_color=MUTED)
            cap.pack(anchor="w", padx=16, pady=(14, 0))
            val = ctk.CTkLabel(card, text="-", font=(FONT, 28, "bold"), text_color=MINT)
            val.pack(anchor="w", padx=16)
            sub = ctk.CTkLabel(card, text="", font=(FONT, 11), text_color=MUTED)
            sub.pack(anchor="w", padx=16, pady=(0, 14))
            self.kpis.append((cap, val, sub))

        self.c_donut = self._panel("Where files will go", 1, 0)
        self.c_found = self._panel("What we found", 1, 1)
        self.c_proj = self._panel("Projects", 2, 0)
        self.c_trips = self._panel("Photo trips", 2, 1)
        self.c_time = self._panel("Photos and videos over time", 3, 0, span=2)
        self.empty_lbl = ctk.CTkLabel(self.scroll, text="No analysis yet. Run Analyze on the Organize page.",
                                      font=(FONT, 13), text_color=MUTED)
        self.empty_lbl.grid(row=4, column=0, columnspan=2, pady=30)
        return page

    def _panel(self, title: str, row: int, col: int, span: int = 1) -> ctk.CTkFrame:
        card = self._card(self.scroll)
        card.grid(row=row, column=col, columnspan=span, sticky="nsew", pady=(12, 0),
                  padx=(0 if col == 0 else 6, 6 if col == 0 and span == 1 else 0))
        ctk.CTkLabel(card, text=title.upper(), font=(FONT, 11, "bold"), text_color=MUTED).pack(
            anchor="w", padx=18, pady=(16, 8))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        return inner

    def _fill_insights(self, d: dict | None) -> None:
        for w in (self.c_donut, self.c_found, self.c_proj, self.c_trips, self.c_time):
            for child in w.winfo_children():
                child.destroy()
        if not d:
            self.empty_lbl.grid()
            for cap, val, sub in self.kpis:
                cap.configure(text=""), val.configure(text="-"), sub.configure(text="")
            return
        self.empty_lbl.grid_remove()
        t = d["totals"]
        for (cap, val, sub), (c, v, s) in zip(self.kpis, (
                ("Files scanned", f"{t['files']:,}", human_size(t["bytes"])),
                ("Project folders", f"{t['projects']:,}", f"{t['in_projects']:,} files grouped"),
                ("Photo trips", f"{t['trips']:,}", f"{t['media']:,} dated media files"),
                ("Duplicates", f"{t['duplicates']:,}", f"{human_size(t['duplicate_bytes'])} reclaimable"),
                ("Left in place", f"{t['left']:,}", f"{t['flagged']:,} flagged"))):
            cap.configure(text=c), val.configure(text=v), sub.configure(text=s)

        dest = d["destinations"][:8]
        lab = ctk.CTkLabel(self.c_donut, text="")
        lab.pack(side="left")
        self._img(lab, "donut", charts.donut([(x["label"], x["files"]) for x in dest], 190, 26, f"{t['files']:,}",
                                              "files"), (190, 190))
        legend = ctk.CTkFrame(self.c_donut, fg_color="transparent")
        legend.pack(side="left", padx=16)
        for i, x in enumerate(dest):
            line = ctk.CTkFrame(legend, fg_color="transparent")
            line.pack(anchor="w", pady=2)
            ctk.CTkLabel(line, text="●", text_color=charts.PALETTE[i % len(charts.PALETTE)],
                         font=(FONT, 12)).pack(side="left")
            ctk.CTkLabel(line, text=f" {x['label']}  {x['files']:,}", text_color=TEXT, font=(FONT, 12)).pack(side="left")

        colors = {"good": "#34d399", "tip": "#60a5fa", "warn": WARN}
        for ins in d["insights"] or [{"kind": "good", "text": "Nothing to flag."}]:
            row = ctk.CTkFrame(self.c_found, fg_color=PANEL, corner_radius=12)
            row.pack(fill="x", pady=4)
            ctk.CTkFrame(row, width=5, height=1, fg_color=colors[ins["kind"]], corner_radius=3).pack(
                side="left", fill="y", padx=(8, 10), pady=8)
            ctk.CTkLabel(row, text=ins["text"], font=(FONT, 12), text_color=TEXT, wraplength=340,
                         justify="left", anchor="w").pack(side="left", fill="x", expand=True, pady=10, padx=(0, 10))

        def bars(parent, key, items, empty):
            if not items:
                ctk.CTkLabel(parent, text=empty, text_color=MUTED, font=(FONT, 12)).pack(anchor="w")
                return
            lab = ctk.CTkLabel(parent, text="")
            lab.pack(anchor="w")
            self._img(lab, key, charts.hbars(items, 380, 32, 130), (380, 32 * len(items)))

        bars(self.c_proj, "proj", [(p["label"], p["files"], f"{p['files']:,}") for p in d["projects"][:8]],
             "No projects found.")
        bars(self.c_trips, "trips", [(x["label"], x["files"], f"{x['files']:,}") for x in d["trips"][:8]],
             "No trips found.")
        lab = ctk.CTkLabel(self.c_time, text="")
        lab.pack(anchor="w")
        self._img(lab, "time", charts.timeline(d["months"], 740, 170), (740, 170))

    # ----------------------------------------------------------------------------- page: review
    def _page_review(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self.body, fg_color="transparent")
        self._title(page, "Review", "Every planned move. Search, filter, then decide.")
        bar = ctk.CTkFrame(page, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 10))
        self.q_var = tk.StringVar()
        ent = ctk.CTkEntry(bar, textvariable=self.q_var, placeholder_text="Search path, project, role...", height=38,
                           corner_radius=10, fg_color=CARD, border_color=LINE, font=(FONT, 12))
        ent.pack(side="left", fill="x", expand=True)
        ent.bind("<Return>", lambda _e: self._review_refresh(reset=True, keep_projects=True))
        self.proj_var = tk.StringVar(value="All projects")
        self.proj_menu = ctk.CTkOptionMenu(bar, variable=self.proj_var, values=["All projects"], width=190, height=38,
                                           corner_radius=10, fg_color=CARD, button_color="#1d2547",
                                           button_hover_color="#27315a", font=(FONT, 12),
                                           command=lambda _v: self._review_refresh(reset=True, keep_projects=True))
        self.proj_menu.pack(side="left", padx=10)
        self._button(bar, "Search", lambda: self._review_refresh(reset=True, keep_projects=True), "primary",
                     width=90).pack(side="left")
        wrap = self._card(page)
        wrap.pack(fill="both", expand=True)
        cols = ("src", "dst", "project", "role", "size")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", style="Dark.Treeview", selectmode="browse")
        self.tree.configure(takefocus=False)
        for c, text, w, stretch in (("src", "Source (inside the scanned folder)", 300, True), ("dst", "New location", 300, True),
                                    ("project", "Project", 130, False), ("role", "Role", 150, False),
                                    ("size", "Size", 80, False)):
            self.tree.heading(c, text=text, anchor="w")
            self.tree.column(c, width=w, stretch=stretch, anchor="e" if c == "size" else "w")
        sb = ctk.CTkScrollbar(wrap, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, 6), pady=10)
        self.tree.pack(fill="both", expand=True, padx=(10, 0), pady=10)
        foot = ctk.CTkFrame(page, fg_color="transparent")
        foot.pack(fill="x", pady=(10, 0))
        self.page_lbl = ctk.CTkLabel(foot, text="", font=(FONT, 12), text_color=MUTED)
        self.page_lbl.pack(side="left")
        self.next_btn = self._button(foot, "Next", lambda: self._review_move(1), width=80)
        self.next_btn.pack(side="right")
        self.prev_btn = self._button(foot, "Prev", lambda: self._review_move(-1), width=80)
        self.prev_btn.pack(side="right", padx=8)
        return page

    def _current_run(self) -> int | None:
        try:
            conn = db.connect(db_path())
            try:
                return report.latest_dry_run(conn)
            finally:
                conn.close()
        except Exception:
            return None

    def _review_refresh(self, reset: bool = False, keep_projects: bool = False) -> None:
        if reset:
            self.review_page = 0
        try:
            conn = db.connect(db_path())
        except Exception:
            return
        try:
            rid = report.latest_dry_run(conn)
            self.tree.delete(*self.tree.get_children())
            if rid is None:
                self.page_lbl.configure(text="No analysis yet.")
                return
            dest = conn.execute("SELECT dest FROM runs WHERE id=?", (rid,)).fetchone()[0]
            if rid != self.review_run or not keep_projects:
                names = ["All projects", "(no project)"] + report.project_names(conn, rid)
                self.proj_menu.configure(values=names)
                if self.proj_var.get() not in names:
                    self.proj_var.set("All projects")
            self.review_run = rid
            proj = self.proj_var.get()
            rows, total = report.query_rows(conn, rid, dest, self.q_var.get().strip(),
                                            "" if proj == "All projects" else proj, PAGE_ROWS,
                                            self.review_page * PAGE_ROWS)
        finally:
            conn.close()
        self.review_total = total
        for src, dst, p, role, size in rows:
            self.tree.insert("", "end", values=(src, dst, p, role, human_size(size)))
        a = self.review_page * PAGE_ROWS
        self.page_lbl.configure(text=f"{a + 1 if total else 0:,} to {min(total, a + PAGE_ROWS):,} of {total:,}")
        self.prev_btn.configure(state="normal" if self.review_page else "disabled")
        self.next_btn.configure(state="normal" if a + PAGE_ROWS < total else "disabled")

    def _review_move(self, step: int) -> None:
        self.review_page = max(0, self.review_page + step)
        self._review_refresh(keep_projects=True)

    # ------------------------------------------------------------------------------ page: rules
    def _page_rules(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self.body, fg_color="transparent")
        self._title(page, "Rules", "Your own if-then rules. They win over automatic sorting. The first match is used.")
        form = self._card(page)
        form.pack(fill="x")
        self.rule_fields: dict[str, ctk.CTkEntry] = {}
        spec = (("name", "Rule name", "Tax papers", 160), ("name_contains", "File name contains", "tax, vat", 180),
                ("ext", "File type", "pdf, docx", 120), ("larger_than_mb", "Larger than (MB)", "0", 110),
                ("to", "Move to folder", "Finance/Tax/{year}", 230))
        grid = ctk.CTkFrame(form, fg_color="transparent")
        grid.pack(fill="x", padx=18, pady=(16, 6))
        for i, (key, label, ph, w) in enumerate(spec):
            ctk.CTkLabel(grid, text=label, font=(FONT, 11), text_color=MUTED).grid(row=0, column=i, sticky="w", padx=(0, 10))
            e = ctk.CTkEntry(grid, placeholder_text=ph, width=w, height=36, corner_radius=10, fg_color=PANEL,
                             border_color=LINE, font=(FONT, 12))
            e.grid(row=1, column=i, padx=(0, 10), sticky="w")
            self.rule_fields[key] = e
        foot = ctk.CTkFrame(form, fg_color="transparent", height=1)
        foot.pack(fill="x", padx=18, pady=(4, 16))
        ctk.CTkLabel(foot, text="Words and types are comma separated. In the folder you can use {year} {month} {ext}.",
                     font=(FONT, 11), text_color=MUTED).pack(side="left")
        self._button(foot, "Add rule", self._rule_add, "primary", width=110).pack(side="right")
        self.rule_list = ctk.CTkScrollableFrame(page, fg_color="transparent", corner_radius=0)
        self.rule_list.pack(fill="both", expand=True, pady=(12, 0))
        self._rules_draw()
        return page

    def _rules_draw(self) -> None:
        for w in self.rule_list.winfo_children():
            w.destroy()
        rules = userrules.load()
        if not rules:
            ctk.CTkLabel(self.rule_list, text="No rules yet. Example: file name contains 'tax' and type pdf, "
                         "move to Finance/Tax/{year}.", font=(FONT, 12), text_color=MUTED).pack(anchor="w", pady=10)
        for i, r in enumerate(rules):
            row = self._card(self.rule_list)
            row.pack(fill="x", pady=4)
            conds = []
            if r.get("name_contains"):
                conds.append("name has " + " or ".join(r["name_contains"]))
            if r.get("ext"):
                conds.append("type " + " or ".join(r["ext"]))
            if r.get("larger_than_mb"):
                conds.append(f"over {r['larger_than_mb']} MB")
            ctk.CTkLabel(row, text=r.get("name") or r["to"], font=(FONT, 13, "bold"), text_color=TEXT).pack(
                side="left", padx=(16, 12), pady=14)
            ctk.CTkLabel(row, text="  and  ".join(conds) + "   \u2192   " + r["to"], font=(FONT, 12),
                         text_color=MUTED).pack(side="left")
            self._button(row, "Delete", lambda n=i: self._rule_delete(n), "danger", width=80, height=30).pack(
                side="right", padx=14)

    def _rule_add(self) -> None:
        f = {k: e.get().strip() for k, e in self.rule_fields.items()}
        if not f["to"]:
            messagebox.showinfo("Rule needs a folder", "Type the folder files should go to.")
            return
        rule = {"name": f["name"] or f["to"], "to": f["to"],
                "name_contains": [x.strip() for x in f["name_contains"].split(",") if x.strip()],
                "ext": [x.strip() for x in f["ext"].split(",") if x.strip()]}
        try:
            mb = float(f["larger_than_mb"] or 0)
        except ValueError:
            messagebox.showinfo("Size", "Larger than must be a number of MB.")
            return
        if mb:
            rule["larger_than_mb"] = mb
        if not (rule["name_contains"] or rule["ext"] or mb):
            messagebox.showinfo("Rule is empty", "Add a file name word, a file type or a size, or the rule would match everything.")
            return
        userrules.save(userrules.load() + [rule])
        for e in self.rule_fields.values():
            e.delete(0, "end")
        self._rules_draw()

    def _rule_delete(self, n: int) -> None:
        rules = userrules.load()
        if 0 <= n < len(rules):
            userrules.save(rules[:n] + rules[n + 1:])
        self._rules_draw()

    # ---------------------------------------------------------------------------- page: settings
    def _page_settings(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self.body, fg_color="transparent")
        self._title(page, "Settings", "Saved automatically. Applied the next time you press Analyze.")
        scroll = ctk.CTkScrollableFrame(page, fg_color="transparent", corner_radius=0)
        scroll.pack(fill="both", expand=True)
        sw = dict(progress_color=ACCENT, button_color="#ffffff", font=(FONT, 13), text_color=TEXT)

        def section(title: str) -> ctk.CTkFrame:
            ctk.CTkLabel(scroll, text=title.upper(), font=(FONT, 11, "bold"), text_color=MUTED).pack(
                anchor="w", pady=(14, 6))
            card = self._card(scroll)
            card.pack(fill="x")
            return card

        card = section("Smart sorting")
        for text, var in (("Group related files into project folders", self.group_projects),
                          ("Smart photo folders (trips, events, dates)", self.smart_photos),
                          ("Read inside documents for keywords", self.read_content),
                          ("Rename bills and receipts to date, vendor, amount (shown in the preview first)",
                           self.smart_rename)):
            ctk.CTkSwitch(card, text=text, variable=var, **sw).pack(anchor="w", padx=20, pady=8)
        card = section("About you")
        for r, (label, var, ph, w) in enumerate((
                ("My name(s)", self.my_names, "John Smith, Ioannis", 360),
                ("Home country", self.home, "GB", 80))):
            line = ctk.CTkFrame(card, fg_color="transparent")
            line.pack(fill="x", padx=20, pady=8)
            ctk.CTkLabel(line, text=label, width=120, anchor="w", font=(FONT, 12), text_color=MUTED).pack(side="left")
            ctk.CTkEntry(line, textvariable=var, placeholder_text=ph, width=w, height=36, corner_radius=10,
                         fg_color=PANEL, border_color=LINE, font=(FONT, 12)).pack(side="left")
        ctk.CTkLabel(card, text="Names listed here never become a project folder (comma separated). Photos taken in your "
                                "home country (two-letter code, e.g. GB, CY, GR) become normal events instead of trips.",
                     wraplength=760, justify="left",
                     font=(FONT, 11), text_color=MUTED).pack(anchor="w", padx=20, pady=(0, 12))
        card = section("Duplicates and safety")
        for text, var in (("Detect duplicate files", self.detect_dups),
                          ("Put extra copies in a _Duplicates folder", self.segregate),
                          ("Verify every copy by hash (slow)", self.verify),
                          ("Also sort executables and system files (not recommended)", self.include_exec)):
            ctk.CTkSwitch(card, text=text, variable=var, **sw).pack(anchor="w", padx=20, pady=8)
        card = section("Performance")
        line = ctk.CTkFrame(card, fg_color="transparent")
        line.pack(fill="x", padx=20, pady=12)
        ctk.CTkLabel(line, text="Hash", width=120, anchor="w", font=(FONT, 12), text_color=MUTED).pack(side="left")
        ctk.CTkOptionMenu(line, variable=self.algo, values=["sha256", "md5"], width=110, fg_color=PANEL,
                          button_color="#1d2547", button_hover_color="#27315a").pack(side="left")
        ctk.CTkLabel(line, text="Threads", width=90, anchor="e", font=(FONT, 12), text_color=MUTED).pack(side="left")
        ctk.CTkOptionMenu(line, variable=tk.StringVar(value=str(self.workers.get())), values=[str(i) for i in (1, 2, 4, 8, 12, 16)],
                          width=80, fg_color=PANEL, button_color="#1d2547", button_hover_color="#27315a",
                          command=lambda v: self.workers.set(int(v))).pack(side="left", padx=8)
        return page

    # ---------------------------------------------------------------------------- page: activity
    def _page_activity(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self.body, fg_color="transparent")
        self._title(page, "Activity", "Live log of everything the engine does.")
        self.log = ctk.CTkTextbox(page, fg_color=CARD, border_width=1, border_color=LINE, corner_radius=18,
                                  font=("Consolas", 11), text_color="#cdd5ea", wrap="none")
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")
        return page

    # -------------------------------------------------------------------------------- helpers
    def _browse(self, var: tk.StringVar) -> None:
        d = filedialog.askdirectory(mustexist=False)
        if d:
            var.set(os.path.normpath(d))

    def _find_latest_report(self) -> None:
        try:
            names = sorted(f for f in os.listdir(report_dir()) if f.endswith(".html"))
        except OSError:
            return
        if names:
            self.last_html = os.path.join(report_dir(), names[-1])
            self.last_csv = self.last_html[:-5] + ".csv"

    def _open_html(self) -> None:
        if self.last_html and os.path.exists(self.last_html):
            webbrowser.open(pathlib.Path(self.last_html).as_uri())
        else:
            messagebox.showinfo("No report yet", "Run Analyze first. The report is saved in:\n" + report_dir())

    def _open_csv(self) -> None:
        if self.last_csv and os.path.exists(self.last_csv):
            try:
                os.startfile(self.last_csv)  # type: ignore[attr-defined]  # Windows only
            except AttributeError:
                messagebox.showinfo("Report", self.last_csv)
        else:
            messagebox.showinfo("No report yet", "Run Analyze first.")

    def _opts(self) -> dict:
        return {
            "include_exec": self.include_exec.get(), "segregate_dups": self.segregate.get(),
            "detect_dups": self.detect_dups.get(), "read_content": self.read_content.get(),
            "group_projects": self.group_projects.get(), "smart_photos": self.smart_photos.get(),
            "smart_rename": self.smart_rename.get(),
            "my_names": self.my_names.get().strip(), "home_country": self.home.get().strip().upper(),
            "hash_algo": self.algo.get(), "verify": self.verify.get(),
            "workers": max(1, int(self.workers.get() or 1)),
        }

    def _log(self, text: str) -> None:
        self._append_log([text])

    def _append_log(self, lines: list[str]) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", "\n".join(lines) + "\n")
        n = len(self.log.get("1.0", "end"))
        if n > MAX_LOG_CHARS:
            self.log.delete("1.0", "1000.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _refresh_buttons(self) -> None:
        busy = self._busy()
        ready = False
        if not busy and self.src.get() and self.dst.get():
            try:
                ready = engine.plan_overview(db_path(), self.src.get(), self.dst.get()) is not None
            except Exception:
                ready = False
        self.btn_dry.configure(state="disabled" if busy else "normal")
        self.btn_exec.configure(state="normal" if ready else "disabled")
        self.btn_undo.configure(state="disabled" if busy else "normal")
        self.btn_cancel.configure(state="normal" if busy else "disabled")

    def _load_insights(self) -> None:
        """Build the numbers on a helper thread so a huge run never freezes the window."""
        def run() -> None:
            try:
                conn = db.connect(db_path())
                try:
                    rid = report.latest_dry_run(conn)
                    data = report.build_report(conn, rid) if rid else None
                finally:
                    conn.close()
            except Exception:
                data = None
            self.q.put(("insights", data))
        threading.Thread(target=run, daemon=True).start()

    # ---------------------------------------------------------------------------------- actions
    def _start(self, name: str, fn) -> None:
        self.cancel.clear()
        self._phase = None
        self._job_t0 = time.monotonic()
        self.result_lbl.configure(text="")
        rep = Reporter(self.q.put, self.cancel)

        def run() -> None:
            try:
                self.q.put(("done", name, fn(rep)))
            except Cancelled:
                self.q.put(("cancelled", name))
            except Exception as e:  # shown to the user, never crashes the UI
                self.q.put(("error", name, str(e)))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()
        self.chip.configure(text=f"Running: {name}", text_color=MINT)
        self._refresh_buttons()

    def on_dry_run(self, quiet: bool = False) -> None:
        err = engine.validate_paths(self.src.get(), self.dst.get())
        if err and quiet:
            return
        if err:
            messagebox.showerror("Cannot start", err)
            return
        self._save_settings()
        s, d, o = self.src.get(), self.dst.get(), self._opts()
        self._start("analysis", lambda rep: engine.dry_run(db_path(), s, d, o, rep))

    def on_execute(self) -> None:
        s, d = self.src.get(), self.dst.get()
        err = engine.validate_paths(s, d)
        if err:
            messagebox.showerror("Cannot start", err)
            return
        ov = engine.plan_overview(db_path(), s, d)
        if ov is None:
            messagebox.showerror("Analysis required", "Run Analyze for this source and destination first.")
            return
        mode = MODES[self.mode.get()]
        msg = (f"Mode: {mode.upper()}\nFiles: {ov['files']:,}\nSize: {human_size(ov['bytes'])}\n\n"
               "Every action is journaled and can be reverted with 'Undo last'.\n\nContinue?")
        if not messagebox.askyesno("Execute sorting", msg):
            return
        if mode == "move" and not messagebox.askyesno(
                "Move mode",
                "MOVE relocates your files and flattens their original folder structure.\n"
                "Programs and projects that expect files in specific places can break.\n"
                "Copy is safer for a first run. Move anyway?", icon="warning"):
            return
        self._save_settings()
        o = self._opts()
        self._start("execution", lambda rep: engine.execute(db_path(), s, d, mode, o, rep))

    def on_undo(self) -> None:
        if not messagebox.askyesno("Undo last run", "Revert the most recent execution?"):
            return
        self._start("undo", lambda rep: engine.undo(db_path(), rep))

    # ------------------------------------------------------------------------------ queue pump
    def _poll(self) -> None:
        logs: list[str] = []
        progress = None
        finished: list[tuple] = []
        try:
            for _ in range(2000):
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    logs.append(msg[1])
                elif kind == "progress":
                    progress = msg
                else:
                    finished.append(msg)
        except queue.Empty:
            pass
        if logs:
            self._append_log(logs)
        if progress:
            self._show_progress(*progress[1:])
        for msg in finished:
            if msg[0] == "insights":
                self._fill_insights(msg[1])
            elif msg[0] == "fingerprint":
                self._on_fingerprint(msg[1], msg[2])
            else:
                self._on_finished(msg)
        if self.closing and not self._busy():
            self._save_settings()
            self.destroy()
            return
        self.after(100, self._poll)

    def _watch_tick(self) -> None:
        self.after(30000, self._watch_tick)
        src = self.src.get()
        if (not self.watch.get() or self._busy() or not src or not os.path.isdir(src)
                or time.monotonic() - self._watch_last < max(1, self.watch_min.get()) * 60):
            return
        self._watch_last = time.monotonic()

        def run() -> None:
            self.q.put(("fingerprint", src, report.fingerprint(src)))
        threading.Thread(target=run, daemon=True).start()

    def _on_fingerprint(self, src: str, fp: tuple) -> None:
        old, self._watch_fp = self._watch_fp, (src, fp)
        if old is not None and old != self._watch_fp and not self._busy() and not engine.validate_paths(src, self.dst.get()):
            self._log("Watch: the source folder changed, analyzing again.")
            self.on_dry_run(quiet=True)

    def _show_progress(self, phase: str, done: int, total: int | None) -> None:
        now = time.monotonic()
        if phase != self._phase:
            self._phase, self._phase_t0, self._phase_n0 = phase, now, 0
        rate = (done - self._phase_n0) / max(now - self._phase_t0, 0.001)
        self.phase_lbl.configure(text=phase.capitalize())
        if total:
            self.detail_lbl.configure(text=f"{done:,} of {total:,} files   |   {rate:,.0f} files/s")
        else:
            self.detail_lbl.configure(text=f"{done:,} files so far   |   {rate:,.0f} files/s")
        if now - self._ring_at < 0.15:
            return
        self._ring_at = now
        if total:
            pic = charts.ring(done / total, 170, 14, f"{int(done * 100 / total)}%", f"{rate:,.0f}/s")
        else:
            pic = charts.ring((now * 0.6) % 1.0, 170, 14, "...", f"{done:,}")
        self._img(self.ring_lbl, "ring", pic, (170, 170))

    def _on_finished(self, msg: tuple) -> None:
        kind, name = msg[0], msg[1]
        took = time.monotonic() - self._job_t0
        if kind == "cancelled":
            self.phase_lbl.configure(text=f"{name.capitalize()} cancelled")
            self._log(f"{name} cancelled.")
            self._img(self.ring_lbl, "ring", charts.ring(0, 170, 14, "0%", "stopped"), (170, 170))
        elif kind == "error":
            self.phase_lbl.configure(text=f"{name.capitalize()} failed")
            self.detail_lbl.configure(text=msg[2][:200])
            self._log(f"ERROR: {msg[2]}")
            self._img(self.ring_lbl, "ring", charts.ring(0, 170, 14, "!", "failed"), (170, 170))
            messagebox.showerror(f"{name} failed", msg[2])
        else:
            self._img(self.ring_lbl, "ring", charts.ring(1, 170, 14, "100%", f"{took:.0f}s"), (170, 170))
            self.phase_lbl.configure(text=f"{name.capitalize()} finished")
            res = msg[2] or {}
            if name == "analysis":
                self.last_html, self.last_csv = res.get("report_html"), res.get("report_csv")
                self.detail_lbl.configure(text="Plan ready. Check Insights and Review, then Execute.")
                self.result_lbl.configure(text=(f"{res.get('files', 0):,} files   |   {res.get('projects', 0)} project "
                                                f"folders   |   {res.get('duplicate_files', 0):,} duplicates"))
                self._load_insights()
                self.review_run = None
            else:
                self.detail_lbl.configure(text="Done. Use 'Undo last' to revert. Audit log: " + (res.get("audit_csv") or "-"))
        self.worker = None
        self.chip.configure(text="Idle", text_color=MUTED)
        self._refresh_buttons()

    def _on_close(self) -> None:
        if self._busy():
            if not messagebox.askyesno("Job running", "Cancel the running job and exit?"):
                return
            self.cancel.set()
            self.closing = True
            self.chip.configure(text="Cancelling...", text_color=WARN)
        else:
            self._save_settings()
            self.destroy()


def main() -> None:
    App().mainloop()
