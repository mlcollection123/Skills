"""Your own if-then sorting rules, stored in sort_rules.json next to the database.

Each rule: {"name": "Tax papers", "name_contains": ["tax", "vat"], "ext": [".pdf"], "folder_contains": [],
            "larger_than_mb": 0, "older_than_days": 0, "to": "Finance/Tax/{year}"}
All filled-in conditions must match (AND); inside a list any entry may match (OR). The first matching rule wins and
overrides automatic sorting. Destination placeholders: {year} {month} {ext} {category}."""
from __future__ import annotations

import json
import os
import re
import time

from .config import app_dir

_BAD = re.compile(r'[<>:"|?*\x00-\x1f]')


def rules_file() -> str:
    return os.path.join(app_dir(), "sort_rules.json")


def load() -> list[dict]:
    try:
        with open(rules_file(), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return []
    return [r for r in raw if isinstance(r, dict) and r.get("to")] if isinstance(raw, list) else []


def save(rules: list[dict]) -> None:
    os.makedirs(app_dir(), exist_ok=True)
    with open(rules_file(), "w", encoding="utf-8") as fh:
        json.dump(rules, fh, indent=1, ensure_ascii=False)


def _lower_list(v) -> list[str]:
    return [str(x).strip().lower() for x in (v or []) if str(x).strip()]


def compile_rules(rules: list[dict]) -> list[dict]:
    out = []
    for r in rules:
        out.append({"name": r.get("name") or r["to"], "to": r["to"],
                    "names": _lower_list(r.get("name_contains")),
                    "exts": [e if e.startswith(".") else "." + e for e in _lower_list(r.get("ext"))],
                    "folders": _lower_list(r.get("folder_contains")),
                    "big": float(r.get("larger_than_mb") or 0) * 1024 * 1024,
                    "old": float(r.get("older_than_days") or 0)})
    return out


def match(rules: list[dict], path: str, ext: str, size: int, mtime: float, category: str) -> tuple[str, str] | None:
    """Return (rule name, destination sub-folder) for the first rule that fits, else None."""
    if not rules:
        return None
    base = os.path.basename(path).lower()
    folder = os.path.dirname(path).lower()
    for r in rules:
        if r["names"] and not any(n in base for n in r["names"]):
            continue
        if r["exts"] and ext.lower() not in r["exts"]:
            continue
        if r["folders"] and not any(f in folder for f in r["folders"]):
            continue
        if r["big"] and (size or 0) < r["big"]:
            continue
        if r["old"] and (time.time() - (mtime or 0)) < r["old"] * 86400:
            continue
        if not (r["names"] or r["exts"] or r["folders"] or r["big"] or r["old"]):
            continue                       # an empty rule would grab everything
        t = time.localtime(mtime or 0)
        dest = (r["to"].replace("{year}", f"{t.tm_year}").replace("{month}", f"{t.tm_mon:02d}")
                .replace("{ext}", ext[1:].upper() if ext else "NO_EXTENSION").replace("{category}", category))
        parts = [_BAD.sub("_", p).strip(" .") for p in re.split(r"[\\/]+", dest) if p.strip(" .")]
        if parts and all(p != ".." for p in parts):
            return r["name"], os.path.join(*parts)
    return None
