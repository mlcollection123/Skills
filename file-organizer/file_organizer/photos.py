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
    MAX_GEOCODE_KM, MEDIA_EXTS, MIN_TRIP_PHOTOS, PHOTO_EXTS, RAW_EXTS, SIDECAR_EXTS,
    TRIP_GAP_DAYS, TRIP_MEDIA_MARGIN_DAYS, VIDEO_EXTS,
)
from .db import iter_batches
from .lexicon import is_generic_folder

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
_FILENAME_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?=$|\D|\d{4,6}(?!\d))")   # also DJI_20260927113000
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


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
    """Date and place every photo, RAW file and video, then group them into trips.

    1. EXIF date and GPS from photos and RAW files (Pillow).
    2. A RAW/sidecar file takes the date and place of its JPEG twin (same folder, same name).
    3. Videos and any still-undated RAW file are dated from the file name (DJI_20260927...) or the file date.
    4. GPS photos taken abroad are grouped into trips; undated-GPS media inside a trip's dates join it.
    """
    exif_ok, geo_ok = available()
    if not exif_ok:
        rep.log("Pillow is not installed, so photo dates and GPS are not read. "
                "Run: pip install Pillow geonamescache")
    geo = Geo() if (exif_ok and geo_ok) else None
    if exif_ok and geo is None:
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
        taken, lat, lon = read_exif(path) if exif_ok else (None, None, None)
        country = geo.country(lat, lon) if (geo and lat is not None) else None
        return taken, lat, lon, country, fid

    done = with_date = with_gps = 0
    if exif_ok:
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

    _inherit_from_twins(conn, run_id, rep)           # RAW <- JPEG (date and GPS)
    dated = _date_media_without_exif(conn, run_id, rep)   # videos and unpaired RAW: file name, else file date
    _inherit_from_twins(conn, run_id, rep)           # subtitle/sidecar files <- their video
    trips = cluster_trips(conn, run_id, geo, home)
    joined = _media_into_trips(conn, run_id, trips)
    return {"photos": total, "with_date": with_date, "with_gps": with_gps, "dated_by_name_or_file": dated,
            "joined_trips": joined, "trips": [(t["label"], t["n"]) for t in trips]}


def _inherit_from_twins(conn, run_id: int, rep: Reporter) -> None:
    """DJI_0166.DNG takes the date and GPS of DJI_0166.JPG, DJI_0166.SRT those of its video, and so on."""
    exts = sorted(MEDIA_EXTS)
    marks = ",".join("?" * len(exts))
    sql = f"SELECT id, path FROM files WHERE run_id=? AND ext IN ({marks}) AND id>? ORDER BY id LIMIT ?"
    for rows in iter_batches(conn, sql, (run_id, *exts), 5000):
        rep.check()
        conn.executemany("UPDATE files SET sib=? WHERE id=?", [
            (os.path.join(os.path.dirname(path), os.path.splitext(os.path.basename(path))[0]).lower(), fid)
            for fid, path in rows])
        conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_sib ON files(run_id, sib)")
    conn.execute(
        "UPDATE files SET taken=(SELECT s.taken FROM files s WHERE s.run_id=files.run_id AND s.sib=files.sib "
        "AND s.taken IS NOT NULL AND s.id != files.id LIMIT 1), "
        "lat=(SELECT s.lat FROM files s WHERE s.run_id=files.run_id AND s.sib=files.sib "
        "AND s.taken IS NOT NULL AND s.id != files.id LIMIT 1), "
        "lon=(SELECT s.lon FROM files s WHERE s.run_id=files.run_id AND s.sib=files.sib "
        "AND s.taken IS NOT NULL AND s.id != files.id LIMIT 1), "
        "country=(SELECT s.country FROM files s WHERE s.run_id=files.run_id AND s.sib=files.sib "
        "AND s.taken IS NOT NULL AND s.id != files.id LIMIT 1) "
        "WHERE run_id=? AND taken IS NULL AND sib IS NOT NULL AND EXISTS "
        "(SELECT 1 FROM files s WHERE s.run_id=files.run_id AND s.sib=files.sib AND s.taken IS NOT NULL "
        "AND s.id != files.id)", (run_id,))
    conn.commit()


def _date_media_without_exif(conn, run_id: int, rep: Reporter) -> int:
    """Videos and RAW files with no EXIF: use the date in the file name, else the file's own date."""
    exts = sorted(VIDEO_EXTS | RAW_EXTS)
    marks = ",".join("?" * len(exts))
    sql = (f"SELECT id, path, mtime FROM files WHERE run_id=? AND taken IS NULL AND ext IN ({marks}) "
           "AND id>? ORDER BY id LIMIT ?")
    n = 0
    for rows in iter_batches(conn, sql, (run_id, *exts), 5000):
        rep.check()
        upd = []
        for fid, path, mtime in rows:
            m = _FILENAME_DATE.search(os.path.basename(path))
            t = _ok_time(calendar.timegm(datetime(int(m[1]), int(m[2]), int(m[3])).timetuple())) if m else None
            t = t or _ok_time(mtime)
            if t:
                upd.append((t, fid))
        conn.executemany("UPDATE files SET taken=? WHERE id=?", upd)
        conn.commit()
        n += len(upd)
    return n


# -------------------------------------------------------------------------------- trips
def cluster_trips(conn, run_id: int, geo: Geo | None, home: str) -> list[dict]:
    """Group GPS photos taken abroad into trips: same country, no gap longer than TRIP_GAP_DAYS."""
    home = (home or "").strip().upper()
    gap = TRIP_GAP_DAYS * 86400
    trips: list[list] = []   # [country, start, end, count]
    cur = conn.execute("SELECT country, taken FROM files WHERE run_id=? AND country IS NOT NULL "
                       "AND lat IS NOT NULL AND taken IS NOT NULL AND country != ? ORDER BY country, taken",
                       (run_id, home))
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
    out = []
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
        conn.execute("UPDATE files SET trip=? WHERE run_id=? AND country=? AND lat IS NOT NULL "
                     "AND taken BETWEEN ? AND ?", (label, run_id, c, start, end))
        out.append({"label": label, "country": c, "start": start, "end": end, "n": n})
    conn.commit()
    return out


def _media_into_trips(conn, run_id: int, trips: list[dict]) -> int:
    """Videos, RAW files and sidecars without GPS that were shot during a trip belong to it."""
    margin = TRIP_MEDIA_MARGIN_DAYS * 86400
    exts = sorted(MEDIA_EXTS)
    marks = ",".join("?" * len(exts))
    n = 0
    for t in trips:
        cur = conn.execute(
            f"UPDATE files SET trip=?, country=? WHERE run_id=? AND trip IS NULL AND lat IS NULL "
            f"AND taken BETWEEN ? AND ? AND ext IN ({marks})",
            (t["label"], t["country"], run_id, t["start"] - margin, t["end"] + margin, *exts))
        n += cur.rowcount
    conn.commit()
    return n


# ------------------------------------------------------------------------------- folders
def folder_hint(path: str, root: str | None = None) -> str | None:
    """The nearest meaningful folder name above a photo (e.g. 'Birthday Party'), else None.

    Camera folders such as DCIM, 100APPLE or DJI_001 are skipped, so 'Drone 27.09.2026/DJI_001/x.jpg'
    still yields 'Drone 27.09.2026'. Only folders below the scan root are considered.
    """
    folder = os.path.dirname(path)
    if root:
        rel = os.path.relpath(folder, root)
        names = [] if rel in (".", "") else list(reversed(rel.split(os.sep)))[:4]
    else:
        names = [os.path.basename(folder)]
    for name in names:
        name = name.strip()
        if name and not is_generic_folder(name):
            return _BAD.sub("_", name).strip(" .")[:60] or None
    return None


def media_kind(ext: str) -> str | None:
    """RAW files and videos get their own sub-folder next to the JPEGs."""
    if ext in RAW_EXTS:
        return "RAW"
    if ext in VIDEO_EXTS or ext in (".srt", ".lrf", ".lrv", ".thm"):
        return "Videos"
    return None


def photo_subdir(path: str, taken: float, trip: str | None, ext: str = "", root: str | None = None) -> str:
    """Relative destination folder for a photo, RAW file or video that has a date."""
    t = time.gmtime(taken)
    hint = folder_hint(path, root)
    if trip:
        parts = ["Photos", "Travel", trip]
        if hint:
            parts.append(hint)           # keep the event folder you already made: 'Drone 27.09.2026'
    elif hint:
        label = hint if _YEAR.search(hint) else f"{hint} {t.tm_year}"
        parts = ["Photos", "Events", label]
    else:
        parts = ["Photos", str(t.tm_year), f"{t.tm_year}-{t.tm_mon:02d}"]
    kind = media_kind(ext)
    if kind:
        parts.append(kind)
    return os.path.join(*parts)


def is_media(category: str, ext: str) -> bool:
    return ext in MEDIA_EXTS and category != "Executables-System"
