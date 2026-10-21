"""Tkinter desktop UI. All heavy work runs on one background thread; the UI only polls a queue."""
from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import engine
from .common import Cancelled, Reporter, human_size
from .config import HASH_WORKERS, db_path, report_dir

MODE_LABELS = {
    "Copy (keep originals)": "copy",
    "Move (relocate files)": "move",
    "Symlink (virtual folders)": "symlink",
}
MAX_LOG_LINES = 5000


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("File Organizer - local, private 2 TB sorter")
        self.geometry("1000x740")
        self.minsize(820, 600)

        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.closing = False
        self._phase = None
        self._phase_t0 = 0.0
        self._phase_n0 = 0

        self.src = tk.StringVar()
        self.dst = tk.StringVar()
        self.mode = tk.StringVar(value="Copy (keep originals)")
        self.include_exec = tk.BooleanVar(value=False)
        self.segregate = tk.BooleanVar(value=True)
        self.detect_dups = tk.BooleanVar(value=True)
        self.read_content = tk.BooleanVar(value=True)
        self.smart_photos = tk.BooleanVar(value=True)
        self.home = tk.StringVar()
        self.verify = tk.BooleanVar(value=False)
        self.algo = tk.StringVar(value="sha256")
        self.workers = tk.IntVar(value=HASH_WORKERS)

        self._build()
        for v in (self.src, self.dst):
            v.trace_add("write", lambda *_: self._refresh_buttons())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        self._refresh_buttons()
        self._log("Ready. Pick a source and destination, then press 'Run Dry Run'. "
                  "Nothing is changed until you press 'Execute Sorting'.")

    # ------------------------------------------------------------------------------- layout
    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(6, weight=1)

        ttk.Label(frm, text="Source Directory").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.src).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Browse...", command=lambda: self._browse(self.src)).grid(row=0, column=2, **pad)

        ttk.Label(frm, text="Destination Directory").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.dst).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Browse...", command=lambda: self._browse(self.dst)).grid(row=1, column=2, **pad)

        opt = ttk.LabelFrame(frm, text="Options")
        opt.grid(row=2, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Label(opt, text="Action when executing:").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.Combobox(opt, textvariable=self.mode, values=list(MODE_LABELS), state="readonly",
                     width=26).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(opt, text="Hash:").grid(row=0, column=2, sticky="e", padx=6)
        ttk.Combobox(opt, textvariable=self.algo, values=["sha256", "md5"], state="readonly",
                     width=8).grid(row=0, column=3, sticky="w")
        ttk.Label(opt, text="Threads:").grid(row=0, column=4, sticky="e", padx=6)
        ttk.Spinbox(opt, from_=1, to=16, textvariable=self.workers, width=4).grid(row=0, column=5, sticky="w")
        checks = [
            ("Detect duplicates", self.detect_dups),
            ("Put duplicate copies in _Duplicates", self.segregate),
            ("Read inside documents for keywords", self.read_content),
            ("Smart photo folders (trips, events, dates)", self.smart_photos),
            ("Also sort executables/system files (not recommended)", self.include_exec),
            ("Verify copies by hash (slow)", self.verify),
        ]
        for i, (text, var) in enumerate(checks):
            ttk.Checkbutton(opt, text=text, variable=var).grid(
                row=1 + i // 2, column=(i % 2) * 3, columnspan=3, sticky="w", padx=6, pady=1)

        ttk.Label(opt, text="Home country code (optional, e.g. GB, DE, US):").grid(
            row=4, column=0, columnspan=2, sticky="w", padx=6, pady=3)
        ttk.Entry(opt, textvariable=self.home, width=6).grid(row=4, column=2, sticky="w")

        btns = ttk.Frame(frm)
        btns.grid(row=3, column=0, columnspan=3, sticky="ew", **pad)
        self.btn_dry = ttk.Button(btns, text="Run Dry Run", command=self.on_dry_run)
        self.btn_dry.pack(side="left", padx=(0, 6))
        self.btn_exec = ttk.Button(btns, text="Execute Sorting", command=self.on_execute)
        self.btn_exec.pack(side="left", padx=6)
        self.btn_undo = ttk.Button(btns, text="Undo Last Run", command=self.on_undo)
        self.btn_undo.pack(side="left", padx=6)
        self.btn_cancel = ttk.Button(btns, text="Cancel", command=self.cancel.set, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        ttk.Button(btns, text="Open Reports Folder", command=self._open_reports).pack(side="right")

        self.bar = ttk.Progressbar(frm, mode="determinate", maximum=100)
        self.bar.grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
        self.status = ttk.Label(frm, text="Idle")
        self.status.grid(row=5, column=0, columnspan=3, sticky="w", **pad)

        box = ttk.Frame(frm)
        box.grid(row=6, column=0, columnspan=3, sticky="nsew", **pad)
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self.log = tk.Text(box, wrap="none", state="disabled", font=("Consolas", 9),
                           background="#111", foreground="#ddd")
        self.log.grid(row=0, column=0, sticky="nsew")
        ys = ttk.Scrollbar(box, command=self.log.yview)
        ys.grid(row=0, column=1, sticky="ns")
        xs = ttk.Scrollbar(box, orient="horizontal", command=self.log.xview)
        xs.grid(row=1, column=0, sticky="ew")
        self.log.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)

    # ----------------------------------------------------------------------------- helpers
    def _browse(self, var: tk.StringVar) -> None:
        d = filedialog.askdirectory(mustexist=False)
        if d:
            var.set(os.path.normpath(d))

    def _open_reports(self) -> None:
        try:
            os.startfile(report_dir())  # type: ignore[attr-defined]  # Windows only
        except AttributeError:
            messagebox.showinfo("Reports folder", report_dir())

    def _opts(self) -> dict:
        return {
            "include_exec": self.include_exec.get(), "segregate_dups": self.segregate.get(),
            "detect_dups": self.detect_dups.get(), "read_content": self.read_content.get(),
            "smart_photos": self.smart_photos.get(), "home_country": self.home.get().strip().upper(),
            "hash_algo": self.algo.get(), "verify": self.verify.get(),
            "workers": max(1, int(self.workers.get() or 1)),
        }

    def _log(self, text: str) -> None:
        self._append_log([text])

    def _append_log(self, lines: list[str]) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", "\n".join(lines) + "\n")
        n = int(self.log.index("end-1c").split(".")[0])
        if n > MAX_LOG_LINES:
            self.log.delete("1.0", f"{n - MAX_LOG_LINES}.0")
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

    # ---------------------------------------------------------------------------- actions
    def _start(self, name: str, fn) -> None:
        self.cancel.clear()
        self._phase = None
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
        self._refresh_buttons()

    def on_dry_run(self) -> None:
        err = engine.validate_paths(self.src.get(), self.dst.get())
        if err:
            messagebox.showerror("Cannot start", err)
            return
        s, d, o = self.src.get(), self.dst.get(), self._opts()
        self._start("dry run", lambda rep: engine.dry_run(db_path(), s, d, o, rep))

    def on_execute(self) -> None:
        s, d = self.src.get(), self.dst.get()
        err = engine.validate_paths(s, d)
        if err:
            messagebox.showerror("Cannot start", err)
            return
        ov = engine.plan_overview(db_path(), s, d)
        if ov is None:
            messagebox.showerror("Dry run required", "Run a dry run for this source and destination first.")
            return
        mode = MODE_LABELS[self.mode.get()]
        msg = (f"Mode: {mode.upper()}\nFiles: {ov['files']:,}\nSize: {human_size(ov['bytes'])}\n\n"
               "Every action is journaled and can be reverted with 'Undo Last Run'.\n\nContinue?")
        if not messagebox.askyesno("Execute sorting", msg):
            return
        if mode == "move" and not messagebox.askyesno(
                "Move mode",
                "MOVE relocates your files and flattens their original folder structure.\n"
                "Programs and projects that expect files in specific places can break.\n"
                "Copy is safer for a first run. Move anyway?", icon="warning"):
            return
        o = self._opts()
        self._start("execute", lambda rep: engine.execute(db_path(), s, d, mode, o, rep))

    def on_undo(self) -> None:
        if not messagebox.askyesno("Undo last run", "Revert the most recent execution?"):
            return
        self._start("undo", lambda rep: engine.undo(db_path(), rep))

    # -------------------------------------------------------------------------- queue pump
    def _poll(self) -> None:
        logs: list[str] = []
        progress = None
        finished = None
        try:
            for _ in range(2000):
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    logs.append(msg[1])
                elif kind == "progress":
                    progress = msg
                else:
                    finished = msg
        except queue.Empty:
            pass
        if logs:
            self._append_log(logs)
        if progress:
            self._show_progress(*progress[1:])
        if finished:
            self._on_finished(finished)
        if self.closing and not self._busy():
            self.destroy()
            return
        self.after(100, self._poll)

    def _show_progress(self, phase: str, done: int, total: int | None) -> None:
        now = time.monotonic()
        if phase != self._phase:
            self._phase, self._phase_t0, self._phase_n0 = phase, now, 0
        rate = (done - self._phase_n0) / max(now - self._phase_t0, 0.001)
        if total:
            if str(self.bar["mode"]) != "determinate":
                self.bar.stop()
                self.bar.configure(mode="determinate")
            self.bar["value"] = min(100.0, done * 100.0 / total)
            self.status.configure(text=f"{phase}: {done:,} / {total:,} files  |  {rate:,.0f} files/s")
        else:
            if str(self.bar["mode"]) != "indeterminate":
                self.bar.configure(mode="indeterminate")
                self.bar.start(15)
            self.status.configure(text=f"{phase}: {done:,} files  |  {rate:,.0f} files/s")

    def _on_finished(self, msg: tuple) -> None:
        self.bar.stop()
        self.bar.configure(mode="determinate")
        kind, name = msg[0], msg[1]
        if kind == "cancelled":
            self.bar["value"] = 0
            self.status.configure(text=f"{name} cancelled")
            self._log(f"{name} cancelled.")
        elif kind == "error":
            self.bar["value"] = 0
            self.status.configure(text=f"{name} failed")
            self._log(f"ERROR: {msg[2]}")
            messagebox.showerror(f"{name} failed", msg[2])
        else:
            self.bar["value"] = 100
            self.status.configure(text=f"{name} finished")
        self.worker = None
        self._refresh_buttons()

    def _on_close(self) -> None:
        if self._busy():
            if not messagebox.askyesno("Job running", "Cancel the running job and exit?"):
                return
            self.cancel.set()
            self.closing = True
            self.status.configure(text="Cancelling, then exiting...")
        else:
            self.destroy()


def main() -> None:
    App().mainloop()
