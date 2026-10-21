"""Smart photo folders: capture date + GPS (EXIF) -> trips like 'Germany 2025', all offline.

Needs the optional packages Pillow (EXIF) and geonamescache (offline city/country table).
Without them the rest of the program still works and photos fall back to the normal folders.
"""
from __future__ import annotations

import calendar
import math
import os
import re
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .common import Reporter, win_path
from .config import (
    CATEGORY_GRAPHICS, MAX_GEOCODE_KM, MIN_TRIP_PHOTOS, PHOTO_EXTS, TRIP_GAP_DAYS,
)
from .db import iter_batches

try:
    from PIL import Image
    warnings.simplefilter("ignore", Image.DecompressionBombWarning)
    try:  # optional: lets Pillow read iPhone .heic photos
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
except Exception:  # pragma: no cover
    Image = None

try:
    import geonamescache
except Exception:  # pragma: no cover
    geonamescache = None

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_FILENAME_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)")
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
# Folder names that say nothing about the content of the photos inside.
_GENERIC_FOLDER = re.compile(
    r"^(?:dcim|camera(?: roll)?|photos?|pictures?|images?|img[_\- ]?\d*|dsc[_\- ]?\d*|pxl[_\- ]?\d*|"
    r"\d+|\d{3}[a-z_]*|download(?:s)?|new folder.*|untitled.*|misc|temp|tmp|backup|old|"
    r"screenshots?|whatsapp.*|desktop|documents|my pictures|sorted|unsorted|all|stuff|export(?:ed)?|\.+)$",
    re.IGNORECASE)


def available() -> tuple[bool, bool]:
    """(EXIF reading possible, offline place lookup possible)"""
    return Image is not None, geonamescache is not None


# ------------------------------------------------------------------------ offline geocoder
class Geo:
    """Nearest-city country lookup from a bundled table of ~34,000 towns. No network."""

    def __init__(self) -> None:
        gc = geonamescache.GeonamesCache(min_city_population=15000)
        self.countries = {code: v["name"] for code, v in gc.get_countries().items()}
        self.grid: dict[tuple[int, int], list[tuple[float, float, str]]] = {}
        for c in gc.get_cities().values():
            lat, lon = c["latitude"], c["longitude"]
            self.grid.setdefault((math.floor(lat), math.floor(lon)), []).append((lat, lon, c["countrycode"]))

    def country(self, lat: float, lon: float) -> str | None:
        """ISO country code of the closest town within MAX_GEOCODE_KM, else None (e.g. at sea)."""
        la, lo = math.floor(lat), math.floor(lon)
        lon_span = min(8, max(2, math.ceil(2 / max(math.cos(math.radians(lat)), 0.25))))
        best, best_d = None, 1e9
        for i in range(la - 2, la + 3):
            for j in range(lo - lon_span, lo + lon_span + 1):
                for clat, clon, cc in self.grid.get((i, (j + 180) % 360 - 180), ()):
                    d = _haversine(lat, lon, clat, clon)
                    if d < best_d:
                        best, best_d = cc, d
        return best if best_d <= MAX_GEOCODE_KM else None

    def name(self, code: str) -> str:
        return self.countries.get(code, code)


def _haversine(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


# ------------------------------------------------------------------------------ EXIF
def _parse_dt(value) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.strptime(str(value).strip("\x00 ")[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    return _ok_time(calendar.timegm(dt.timetuple()))


def _ok_time(ts: float) -> float | None:
    return ts if 631152000 <= ts <= time.time() + 366 * 86400 else None   # 1990 .. next year


def _dms(values, ref) -> float | None:
    try:
        d, m, s = (float(v) for v in values)
    except (TypeError, ValueError):
        return None
    deg = d + m / 60 + s / 3600
    return -deg if str(ref).upper() in ("S", "W") else deg


def read_exif(path: str) -> tuple[float | None, float | None, float | None]:
    """(taken_epoch, lat, lon) from the photo's metadata; any part may be None."""
    taken = lat = lon = None
    if Image is not None:
        try:
            with Image.open(win_path(path)) as im:     # reads the header only, not the pixels
                ex = im.getexif()
                sub = ex.get_ifd(0x8769) or {}
                taken = _parse_dt(sub.get(36867)) or _parse_dt(sub.get(36868)) or _parse_dt(ex.get(306))
                gps = ex.get_ifd(0x8825) or {}
                if 2 in gps and 4 in gps:
                    lat, lon = _dms(gps[2], gps.get(1)), _dms(gps[4], gps.get(3))
                    if lat is None or lon is None or (lat == 0 and lon == 0) \
                            or abs(lat) > 90 or abs(lon) > 180:
                        lat = lon = None
        except Exception:
            pass
    if taken is None:   # WhatsApp/phone names such as IMG-20250714-WA0003.jpg or PXL_20250714_101500.jpg
        m = _FILENAME_DATE.search(os.path.basename(path))
        if m:
            taken = _ok_time(calendar.timegm(datetime(int(m[1]), int(m[2]), int(m[3])).timetuple()))
    return taken, lat, lon


def analyze(conn, run_id: int, rep: Reporter, workers: int, home: str) -> dict:
    """Fill taken/lat/lon/country for photos, then group them into trips."""
    exif_ok, geo_ok = available()
    if not exif_ok:
        rep.log("Pillow is not installed, so photo dates and GPS are not read. "
                "Run: pip install Pillow geonamescache")
        return {"photos": 0, "with_date": 0, "with_gps": 0, "trips": []}
    geo = Geo() if geo_ok else None
    if geo is None:
        rep.log("geonamescache is not installed: photos are sorted by date, without place names. "
                "Run: pip install geonamescache")
    exts = sorted(PHOTO_EXTS)
    marks = ",".join("?" * len(exts))
    total = conn.execute(f"SELECT COUNT(*) FROM files WHERE run_id=? AND ext IN ({marks})",
                         (run_id, *exts)).fetchone()[0]
    sql = (f"SELECT id, path FROM files WHERE run_id=? AND ext IN ({marks}) AND id>? "
           "ORDER BY id LIMIT ?")

    def work(row):
        fid, path = row
        taken, lat, lon = read_exif(path)
        country = geo.country(lat, lon) if (geo and lat is not None) else None
        return taken, lat, lon, country, fid

    done = with_date = with_gps = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for rows in iter_batches(conn, sql, (run_id, *exts), 256):
            rep.check()
            res = list(ex.map(work, rows))
            conn.executemany("UPDATE files SET taken=?, lat=?, lon=?, country=? WHERE id=?", res)
            conn.commit()
            done += len(rows)
            with_date += sum(1 for r in res if r[0] is not None)
            with_gps += sum(1 for r in res if r[1] is not None)
            rep.progress("Reading photo metadata", done, total)
    rep.progress("Reading photo metadata", done, total, force=True)
    trips = cluster_trips(conn, run_id, geo, home)
    return {"photos": done, "with_date": with_date, "with_gps": with_gps, "trips": trips}


# -------------------------------------------------------------------------------- trips
def cluster_trips(conn, run_id: int, geo: Geo | None, home: str) -> list[tuple[str, int]]:
    """Group GPS photos taken abroad into trips: same country, no gap longer than TRIP_GAP_DAYS."""
    home = (home or "").strip().upper()
    gap = TRIP_GAP_DAYS * 86400
    trips: list[list] = []   # [country, start, end, count]
    cur = conn.execute("SELECT country, taken FROM files WHERE run_id=? AND country IS NOT NULL "
                       "AND taken IS NOT NULL AND country != ? ORDER BY country, taken", (run_id, home))
    for country, t in cur:
        if trips and trips[-1][0] == country and t - trips[-1][2] <= gap:
            trips[-1][2] = t
            trips[-1][3] += 1
        else:
            trips.append([country, t, t, 1])
    trips = [t for t in trips if t[3] >= MIN_TRIP_PHOTOS]   # a single layover photo is not a trip

    per_year: dict[tuple[str, int], int] = {}
    for c, start, _, _ in trips:
        per_year[(c, time.gmtime(start).tm_year)] = per_year.get((c, time.gmtime(start).tm_year), 0) + 1
    used: dict[str, int] = {}
    summary = []
    for c, start, end, n in trips:
        y = time.gmtime(start)
        name = geo.name(c) if geo else c
        label = f"{name} {y.tm_year}"
        if per_year[(c, y.tm_year)] > 1:
            label = f"{name} {y.tm_year}-{y.tm_mon:02d}"       # several trips that year: add the month
        used[label] = used.get(label, 0) + 1
        if used[label] > 1:
            label = f"{label} ({used[label]})"
        label = _BAD.sub("_", label).strip(" .")
        conn.execute("UPDATE files SET trip=? WHERE run_id=? AND country=? AND taken BETWEEN ? AND ?",
                     (label, run_id, c, start, end))
        summary.append((label, n))
    conn.commit()
    return summary


# ------------------------------------------------------------------------------- folders
def folder_hint(path: str) -> str | None:
    """A meaningful name from the photo's current folder (e.g. 'Birthday Party'), else None."""
    name = os.path.basename(os.path.dirname(path)).strip()
    if sum(ch.isalpha() for ch in name) < 3 or _GENERIC_FOLDER.match(name):
        return None
    return _BAD.sub("_", name).strip(" .")[:60] or None


def photo_subdir(path: str, taken: float, trip: str | None) -> str:
    """Relative destination folder for a photo that has a capture date."""
    t = time.gmtime(taken)
    if trip:
        return os.path.join("Photos", "Travel", trip)
    hint = folder_hint(path)
    if hint:
        label = hint if _YEAR.search(hint) else f"{hint} {t.tm_year}"
        return os.path.join("Photos", "Events", label)
    return os.path.join("Photos", str(t.tm_year), f"{t.tm_year}-{t.tm_mon:02d}")


def is_photo(category: str, ext: str) -> bool:
    return ext in PHOTO_EXTS and category in (CATEGORY_GRAPHICS, "Other")
