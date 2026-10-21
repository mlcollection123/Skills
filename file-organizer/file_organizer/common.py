"""Shared helpers: cancellation, progress reporting, Windows long-path support."""
from __future__ import annotations

import os
import threading
import time


class Cancelled(Exception):
    """Raised inside worker code when the user pressed Cancel."""


class Reporter:
    """Thread-safe bridge between worker code and whatever shows progress.

    `sink` is any callable taking one tuple:
        ("log", text) or ("progress", phase, done, total_or_None)
    """

    def __init__(self, sink, cancel: threading.Event | None = None, min_interval: float = 0.2):
        self._sink = sink
        self.cancel = cancel or threading.Event()
        self._min = min_interval
        self._last = 0.0

    def log(self, msg) -> None:
        self._sink(("log", str(msg)))

    def progress(self, phase: str, done: int, total: int | None = None, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last >= self._min:
            self._last = now
            self._sink(("progress", phase, done, total))

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()


def win_path(p: str) -> str:
    """Return a path usable beyond the 260-character Windows limit (no-op elsewhere)."""
    if os.name != "nt":
        return p
    p = os.path.abspath(p)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def norm(p: str) -> str:
    """Canonical form of a path for comparisons."""
    return os.path.normcase(os.path.abspath(p))


def is_inside(child: str, parent: str) -> bool:
    c, p = norm(child), norm(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def human_size(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"
