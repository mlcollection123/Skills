"""File-system helpers: hashing, MIME guess, cross-device detection."""
from __future__ import annotations

import errno
import hashlib
import mimetypes
import os

from .common import Cancelled, win_path
from .config import PARTIAL_BYTES

mimetypes.init()
_mime_cache: dict[str, str] = {}


def guess_mime(ext: str) -> str:
    """MIME type from the extension (fast; does not open the file)."""
    m = _mime_cache.get(ext)
    if m is None:
        m = mimetypes.guess_type("x" + ext)[0] or "application/octet-stream"
        _mime_cache[ext] = m
    return m


def partial_hash(path: str, size: int, algo: str) -> tuple[str, str | None]:
    """Quick fingerprint of the first and last PARTIAL_BYTES.

    Returns (partial, full). `full` is set only when the file is small enough that the
    fingerprint already covers every byte.
    """
    h = hashlib.new(algo)
    h.update(str(size).encode())
    with open(win_path(path), "rb") as f:
        if size <= 2 * PARTIAL_BYTES:
            h.update(f.read())
            d = h.hexdigest()
            return d, d
        h.update(f.read(PARTIAL_BYTES))
        f.seek(max(0, size - PARTIAL_BYTES))
        h.update(f.read(PARTIAL_BYTES))
    return h.hexdigest(), None


def full_hash(path: str, algo: str, cancel=None, bufsize: int = 1 << 20) -> str:
    h = hashlib.new(algo)
    with open(win_path(path), "rb", buffering=0) as f:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            chunk = f.read(bufsize)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def is_cross_device(e: OSError) -> bool:
    return e.errno == errno.EXDEV or getattr(e, "winerror", None) == 17
